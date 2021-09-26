# Simple CircuitPython Pi Pico webserver using ESP32 WiFi
#
# For detailed description, see https://iosoft.blog
#
# Copyright (c) 2021 Jeremy P Bentham
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# v0.02 JPB 25/9/21 Tidied up for release

import os, time, board, busio
from digitalio import DigitalInOut
from analogio import AnalogIn
from adafruit_esp32spi import adafruit_esp32spi
import adafruit_esp32spi.adafruit_esp32spi_socket as socket

WIFI_SSID = "testnet"
WIFI_PASS = "testpass"

ADC_PINS = board.A0, board.A1, board.A2
ADC_SCALE = 3.3 / 65536
esp32_cs = DigitalInOut(board.GP7)
esp32_ready = DigitalInOut(board.GP10)
esp32_reset = DigitalInOut(board.GP11)

DIRECTORY     = "/"
INDEX_FNAME   = "index.html"
TEST_FNAME    = "test.html"
DATA_FNAME    = "data.csv"
ICON_FNAME    = "favicon.ico"

HTTP_PORT = 80
MAX_SPI_DLEN = 128
NO_SOCKET = 255

HTTP_OK = "HTTP/1.1 200 OK\r\n"
CONTENT_LEN = "Content-Length: %u\r\n"
CONTENT_TYPE = "Content-type %s\r\n"
TEST_PAGE = '''<!DOCTYPE html><html>
    <head><style>table, th, td {border: 1px solid black; margin: 5px;}</style></head>
    <body><h2>Pi Pico web server</h2>%s</body></html>'''
NOT_FOUND = 'HTTP/1.1 404 Not found\r\nContent-Length: 13\r\n\r\n404 Not Found'
DISABLE_CACHE = "Cache-Control: no-cache, no-store, must-revalidate\r\n"
DISABLE_CACHE += "Pragma: no-cache\r\nExpires: 0\r\n"
HEAD_END = "\r\n"

# Wifi status (firmware getConnStatus): see nina-fw/arduino/libraries/WiFi/src/Wifi.h
status_strs = {0:"idle", 1:"connecting", 2:"scan complete", 3:"connected", 4:"connect failed",
               5:"connection lost", 6:"disconnected", 255:"no WiFi"}
STATUS_OK, STATUS_FAIL = 3, 4
sock_strs = ("closed", "listen", "syn_sent", "syn_rcvd", "established", "fin_wait_1",
             "fin_wait_2", "close_wait", "closing", "last_ack", "time_wait")

def mstimeout(start, tout):
    return utime.ticks_ms()-start > mstout

# Class for ESP32 WiFi interface
class ESP32:
    def __init__(self):
        self.last_status = self.server_sock = None
        self.txcount = 0
        spi = busio.SPI(board.GP18, board.GP19, board.GP16)
        self.esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)
        self.set_power_mode(False)

    # Reset WiFi adaptor
    def reset(self):
        self.esp.reset()

    # Return status of WiFi connection
    def get_wifi_status(self):
        return self.esp.status

    # Return status of socket connection
    def get_server_status(self):
        return False if self.server_sock is None else self.esp.server_state(self.server_sock.socknum)

    # Get my IP address
    def get_ip_address(self):
        return self.esp.ip_address

    # Display status of WiFi connection
    def disp_wifi_status(self, status):
        s = status_strs[status] if status in status_strs else str(status)
        print("WiFi status: %s" % s)

    # Check WiFi status, display a change
    def check_wifi_status(self):
        status = self.get_wifi_status()
        if status != self.last_status:
            self.disp_wifi_status(status)
            self.last_status = status
        return status

    # Connect to WiFi network, given SSID and password
    def connect(self, ssid, passwd):
        self.reset()
        self.esp.wifi_set_passphrase(bytes(ssid, "utf-8"), bytes(passwd, "utf-8"))

    # Enable or disable ESP32 power-saving
    def set_power_mode(self, enabled=False):
        self.esp._send_command_get_response(0x17, ((bool(enabled),),))

    # Get client socket, given server socket
    def get_client_sock(self, server_sock):
        num = self.esp.socket_available(server_sock.socknum)
        return socket.socket(socknum=num) if num!=NO_SOCKET else None

    # Start TCP server, given port number
    def start_server(self, port):
        socket.set_interface(self.esp)
        self.server_sock = socket.socket(socknum=self.esp.get_socket())
        self.client_sock = None
        self.esp.start_server(port, self.server_sock.socknum)

    # Return length of data received by server
    def recv_length(self, sock):
        return self.esp.socket_available(sock.socknum)

    # Return data received by server
    def recv_data(self, sock, dlen):
        return self.esp.socket_read(sock.socknum, self.recv_length(sock))

    # Send block of data to client
    def send_data(self, sock, data):
        self.esp.socket_write(sock.socknum, data)

    # Mark end of data sent to client
    def send_end(self, sock):
        self.esp.socket_close(sock.socknum)

    # Get HTTP request from client
    def get_http_request(self, mstout=1000):
        self.client_sock = self.get_client_sock(self.server_sock)
        if self.client_sock:
            client_dlen = self.recv_length(self.client_sock)
            print("Client socket %d len %d: " % (self.client_sock.socknum, client_dlen), end="")
            if client_dlen > 0:
                req = self.recv_data(self.client_sock, client_dlen)
                return req.decode("utf-8")
        return None

    # Split data into blocks, send to client
    def put_data(self, data):
        n = 0
        while n < len(data):
            d = data[n: n+MAX_SPI_DLEN]
            self.send_data(self.client_sock, d)
            n += len(d)

    # Send 'not found' error to client
    def put_http_404(self):
        self.send_data(self.client_sock, NOT_FOUND)
        self.send_end(self.client_sock)

    # Send text response to client
    def put_http_text(self, text):
        resp = HTTP_OK + CONTENT_LEN%len(text)
        resp += CONTENT_TYPE%"text/html" + HEAD_END + text
        self.put_data(resp)
        self.send_end(self.client_sock)
        self.txcount += 1

    # Send file from filesystem to client
    def put_http_file(self, fname, content="text/html; charset=utf-8", hdr=""):
        try:
            f = open(fname)
        except:
            f = None
        if not f:
            self.put_http_404()
        else:
            flen = os.stat(fname)[6]
            resp = HTTP_OK + CONTENT_LEN%flen + CONTENT_TYPE%content + hdr + HEAD_END
            self.send_data(self.client_sock, resp)
            n = 0
            while n < flen:
                data = f.read(MAX_SPI_DLEN)
                self.send_data(self.client_sock, data)
                n += len(data)
            self.send_end(self.client_sock)

# Initialise TCP server, return class instance
def server_init():
    print("Connecting to %s..." % WIFI_SSID)
    e = ESP32()
    status = 0
    while status != STATUS_OK:
        if status==0 or status==STATUS_FAIL:
            e.connect(WIFI_SSID, WIFI_PASS)
            time.sleep(0.2)
        status = e.check_wifi_status()
    e.start_server(HTTP_PORT)
    while not e.get_server_status():
        pass
    ip_addr = e.get_ip_address()
    print("Server socket {0}, {2}.{3}.{4}.{5}:{1}".format(e.server_sock.socknum, HTTP_PORT, *ip_addr))
    return e

if __name__ == "__main__":
    esp = server_init()
    adcs = [AnalogIn(pin) for pin in ADC_PINS]
    while True:
        req = esp.get_http_request()
        if req:
            r = req.split("\r")[0]
            print(r, end="")
            if ICON_FNAME in r:
                print(" [not found]")
                esp.put_http_404()
            elif TEST_FNAME in r:
                print(" [test page]")
                heads = [str(pin)[-2:] for pin in ADC_PINS]
                vals = [("%1.3f" % (adc.value * ADC_SCALE)) for adc in adcs]
                th = "<tr><th>" + "</th><th>".join(heads) + "</th></tr>"
                tr = "<tr><td>" + "</td><td>".join(vals) + "</td></tr>"
                table = "<table><caption>ADC voltages</caption>%s</table>" % (th+tr)
                esp.put_http_text(TEST_PAGE % table)
            elif DATA_FNAME in r:
                print(" [data/csv]")
                esp.put_http_file(DIRECTORY+DATA_FNAME, "text/csv", DISABLE_CACHE)
            else:
                print(" [index]")
                esp.put_http_file(DIRECTORY+INDEX_FNAME)
        time.sleep(0.01)

#EOF

