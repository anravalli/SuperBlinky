#!/usr/bin/env python3

import gpiod
import paho.mqtt.client as mqtt
import select
import time

LED_CH = 18
BUTTON_CH = 17

BROKER = "192.168.1.20"
PORT = 1883
STATUS_TOPIC = "iot/status/led"
COMMAND_TOPIC = "iot/command/led"

led_status = "OFF"

chip = None
led_line = None
button_line = None


# --- LOGICA LED ---
def set_led(state, mqtt_client=None):
    global led_status

    if state == "ON":
        led_line.set_value(1)
    else:
        led_line.set_value(0)

    led_status = state
    print(f"LED -> {state}")

    if mqtt_client:
        mqtt_client.publish(STATUS_TOPIC, led_status)


# --- MQTT CALLBACK ---
def on_message(mqtt_client, userdata, msg):
    payload = msg.payload.decode("utf-8").upper()

    print(f"message on topic: {msg.topic}")

    if msg.topic == COMMAND_TOPIC:
        print(f"received LED command: {payload}")
        set_led(payload, mqtt_client)
    else:
        print("Unsupported topic")


# --- GPIO SETUP ---
def gpioSetUp():
    global chip, led_line, button_line

    print("Setting up GPIO (event + poll)")

    chip = gpiod.Chip("gpiochip0")

    led_line = chip.get_line(LED_CH)
    button_line = chip.get_line(BUTTON_CH)

    led_line.request(consumer="iot-led", type=gpiod.LINE_REQ_DIR_OUT)

    button_line.request(
        consumer="iot-button",
        type=gpiod.LINE_REQ_EV_RISING_EDGE,
        flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_DOWN
    )


# --- MQTT SETUP ---
def setupMqttClient():
    client = mqtt.Client()

    def on_connect(mqtt_client, userdata, flags, rc):
        if rc == 0:
            print("Connected to Broker!")
            mqtt_client.subscribe(COMMAND_TOPIC)
            mqtt_client.publish(STATUS_TOPIC, led_status)
        else:
            print(f"Connection error, code: {rc}")

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting to {BROKER}...")
    client.connect(BROKER, PORT, 60)

    return client


# --- MAIN LOOP ---
def main_loop(client):
    poller = select.poll()

    # fd GPIO
    gpio_fd = button_line.event_get_fd()
    poller.register(gpio_fd, select.POLLIN)

    # fd MQTT
    sock = client.socket()
    poller.register(sock, select.POLLIN)

    print("Entering unified event loop...")

    last_press_time = 0
    debounce_ms = 200

    while True:
        events = poller.poll(1000)  # timeout 1s

        for fd, flag in events:

            # --- GPIO EVENT ---
            if fd == gpio_fd:
                for evt in button_line.event_read():
                    if evt.type == gpiod.LineEvent.RISING_EDGE:

                        now = int(time.time() * 1000)
                        if now - last_press_time < debounce_ms:
                            continue

                        last_press_time = now

                        print("Button pressed")

                        new_state = "OFF" if led_status == "ON" else "ON"
                        set_led(new_state, client)

            # --- MQTT EVENT ---
            elif fd == sock.fileno():
                client.loop_read()

        # MQTT housekeeping (keepalive, retry, ecc.)
        client.loop_misc()


# --- MAIN ---
try:
    print("Remote Pi Control Switch (poll-based)")

    gpioSetUp()

    set_led("OFF")

    client = setupMqttClient()

    main_loop(client)

except KeyboardInterrupt:
    print("\n...loop stopped...")

finally:
    if led_line:
        led_line.release()
    if button_line:
        button_line.release()

    print("GPIO cleanup and exit.")