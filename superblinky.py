#!/usr/bin/env python3

import typing

import gpiod
from gpiod.edge_event import EdgeEvent
from gpiod.line import Edge, Direction, Value

import paho.mqtt.client as mqtt
import select
import time
from datetime import timedelta


def edge_type_str(event: EdgeEvent) -> str:
    if event.event_type is event.Type.RISING_EDGE:
        return "Rising"
    if event.event_type is event.Type.FALLING_EDGE:
        return "Falling"
    return "Unknown"

class GpioController:
    __request: gpiod.LineRequest = None
    __gpio_dev: str = "/dev/gpiochip0"
    __led_offset: int = 18
    __button_offset: int = 17
    __button_callback: callable = None

    def __init__(self):
        self.__request = self._setup_gpio()
        self.set_led("OFF")

    def _setup_gpio(self):
        print("Setting up GPIO")

        chip = gpiod.Chip(self.__gpio_dev)

        request = chip.request_lines(
                consumer="iot-ctl", 
                config={
                    self.__led_offset: gpiod.LineSettings(direction=Direction.OUTPUT, 
                                                output_value=Value.INACTIVE),
                    self.__button_offset: gpiod.LineSettings(direction=Direction.INPUT, 
                                                edge_detection=Edge.RISING,
                                                debounce_period=timedelta(milliseconds=10)) 
                    }
            )
        
        return request

    def set_button_callback(self, cbk: callable):
        self.__button_callback = cbk

    def set_led(self, state: str):
        val = Value.INACTIVE
        #self.__led_status = state
        if state == "ON":
            val = Value.ACTIVE
        self.__request.set_value(self.__led_offset, val)

    def read_button(self):
        for event in self.__request.read_edge_events():
            print(
                f"offset: {event.line_offset}"
                f"  type: {edge_type_str(event):<7}"
                f"  event #{event.global_seqno}"
                f"  line event #{event.line_seqno}"
            )
            if event.event_type == event.Type.RISING_EDGE:
                self.__button_callback()
            else:
                print("Not a rising edge event")
    
    def fd(self) -> int:
        return self.__request.fd

    def release(self):
        self.__request.release()


class MqttClient:
    __client: mqtt.Client = None
    __broker = "192.168.1.20"
    __port = 1883
    __status_topic = "iot/status/led"
    __command_topic = "iot/command/led"
    __socket_fd: int = None
    
    def __init__(self):
        self.__client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.__client.on_connect = self.on_connect
        self.__client.on_disconnect = self.on_disconnect

    def on_connect(self, mqtt_client, userdata, flags, rc, props):
        if rc == 0:
            print("Connected to Broker!")
            self.__client.subscribe(self.__command_topic)
            print("...subscribed to command topic")
        else:
            print(f"Connection error, code: {rc}")

    def on_disconnect(self, mqtt_client, userdata, disconnect_flags, rc, props):
        print(f"Disconnected from broker (rc={rc})")
        self.reconnect()

    def reconnect(self):
        try:
            print("Attempting MQTT reconnect...")
            self.__client.reconnect()
            return True
        except Exception as ex:
            print(f"Reconnect failed: {ex}")
            return False

    def publish_state(self, state: str):
        print("Publishing state")
        self.__client.publish(self.__status_topic, state)

    def connect(self):
        while True:
            try:
                print(f"Connecting to {self.__broker}...")
                self.__client.connect(self.__broker, self.__port, 60)
                print("MQTT connect initiated")
                return

            except Exception as ex:
                print(f"MQTT connection failed: {ex}")
                print("Retry in 5 seconds...")
                time.sleep(5)

    def set_message_callback(self, message_cbk, userdata):
        self.__client.user_data_set(userdata)
        self.__client.on_message = message_cbk

    def read(self):
        self.__client.loop_read()
    
    def misc(self):
        self.__client.loop_misc()

    def socket(self) -> int:
        return self.__client.socket()

    def fileno(self):
        sock = self.__client.socket()
        if sock is None:
            return None
        return sock.fileno()

    def disconnect(self):
        if self.ready():
            self.publish_state("---")
            self.__client.disconnect()

    def ready(self) -> bool:
        return self.__client.is_connected()
    
    def write(self):
        if self.__client.want_write():
            self.__client.loop_write()

    def unregister_fd(self, poller: select.poll):
        # unregister old fd
        if self.__socket_fd is not None:
            print(f"Unregistering MQTT fd: {self.__socket_fd}")
            try:
                poller.unregister(self.__socket_fd)
                self.__socket_fd = None
            except Exception:
                pass

    def register_fd(self, poller: select.poll):
        #self.__socket_fd = self.fileno()
        # register new fd
        if self.__socket_fd is not None:
            print(f"Registering MQTT fd: {self.__socket_fd}")
            poller.register(self.__socket_fd, select.POLLIN)

    def check_fd(self, poller):
        # if not mqtt_client.ready():
        #     print(f"MqttClient: not ready")
        #     return
        
        if self.__socket_fd != self.fileno():
            self.unregister_fd(poller)
            self.__socket_fd = self.fileno()
            self.register_fd(poller)

    def loop(self):
        self.__client.loop()


class LedManager:

    __mqtt_client: MqttClient = None
    __gpio_controller: GpioController = None
    __led_status: str = "OFF"
    __command_topic = "iot/command/led"

    def __init__(self, mqtt_client, gpio_ctrl):
        self.__gpio_controller = gpio_ctrl
        self.__mqtt_client = mqtt_client
        mqtt_client.set_message_callback(self.mqtt_message_callback, self.__gpio_controller)
        gpio_ctrl.set_button_callback(self.gpio_button_callback)
        

    def gpio_button_callback(self):
        print("Button pressed")
        self.__led_status = "OFF" if self.__led_status == "ON" else "ON"
        self.update_led()

    def mqtt_message_callback(self, mqtt_client: mqtt.Client, gpio_ctrl: GpioController, msg):
        if msg.topic == self.__command_topic:
            print(f"message on topic: {msg.topic}")
            new_status = msg.payload.decode("utf-8").upper()

            if new_status == "ON" or new_status == "OFF":
                print(f"Received valid LED command: {new_status}")
                if new_status != self.__led_status:
                    print(f"Status changed: updating")
                    self.__led_status = new_status
                    self.update_led()
            else:
                print(f"Invalid LED command: {new_status}")
        else:
            print("Unsupported topic")

    def update_led(self):
        self.__gpio_controller.set_led(self.__led_status)
        self.__mqtt_client.publish_state(self.__led_status)
    

# --- MAIN LOOP ---
def main_loop(gpio_ctrl: GpioController, mqtt_client: MqttClient):
    
    poller = select.poll()
    poller.register(gpio_ctrl.fd(), select.POLLIN)

    while not mqtt_client.ready():
        mqtt_client.loop()

    if mqtt_client.ready():
        mqtt_client.register_fd(poller)
        print("Publishing initial state")
        mqtt_client.publish_state("XXX") 

    print("Entering event loop...")
    while True:
        # MQTT SOCKET MANAGEMENT
        current_fd = mqtt_client.fileno()

        # check if socket changed after reconnect
        mqtt_client.check_fd(poller)
        
        events = poller.poll(1000)

        for fd, flag in events:
            # --- GPIO EVENT ---
            if fd == gpio_ctrl.fd():
                new_state = gpio_ctrl.read_button()
            # --- MQTT EVENT ---
            elif fd == mqtt_client.fileno():
                mqtt_client.read()
                mqtt_client.write()
            else:
                print(f'unexpected file descriptor: {fd} ({flag})')

        # MQTT housekeeping (keepalive, retry, ecc.)
        mqtt_client.misc()

# --- MAIN ---
try:
    print("Remote Pi Control Switch")

    gpioctl = None
    mqtt_client = None
    
    gpioctl = GpioController()

    mqtt_client = MqttClient()
    mqtt_client.connect()

    led_manager = LedManager(mqtt_client, gpioctl)

    main_loop(gpioctl, mqtt_client)

except KeyboardInterrupt:
    print("\n...loop stopped...")

finally:
    if gpioctl:
        print("GPIO cleanup")
        gpioctl.set_led("OFF")
        gpioctl.release()
    if mqtt_client:
        print("Disconnecting MQTT client")
        mqtt_client.disconnect()
    print("Exit")