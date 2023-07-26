#!/usr/bin/env python3
"""
TITLE:  mumblestream
AUTHOR: Ranomier (ranomier@fragomat.net), F4EXB (f4exb06@gmail.com)
DESC:   A bot that streams host audio to/from a Mumble server.
"""

import argparse
import sys
import os
import subprocess
from threading import Thread
import time
import logging
import json
import collections

from ctypes import *
import time
#import threading
import select
import socket
import errno
import struct
import ctypes
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import audioop

import pymumble_py3 as pymumble
from pymumble_py3.callbacks import PYMUMBLE_CLBK_SOUNDRECEIVED as CLBK_SOUNDRECEIVED
import pyaudio
import numpy as np

from pulseaudio import PulseAudioHandler

__version__ = "0.1.0"

logging.basicConfig(format="%(asctime)s %(levelname).1s [%(threadName)s] %(funcName)s: %(message)s", level=logging.INFO)
LOG = logging.getLogger("Mumblestream")

global_mumble = None

class Status(collections.UserList):
    """Thread status handler"""

    def __init__(self, runner_obj):
        self.__runner_obj = runner_obj
        self.scheme = collections.namedtuple("thread_info", ("name", "alive"))
        super().__init__(self.__gather_status())

    def __gather_status(self):
        """Gather status"""
        result = []
        for meta in self.__runner_obj.values():
            result.append(self.scheme(meta["process"].name, meta["process"].is_alive()))
        return result

    def __repr__(self):
        repr_str = ""
        for status in self:
            repr_str += f"[{status.name}] alive: {status.alive} "
        return repr_str


class Runner(collections.UserDict):
    """Runs a list of threads"""

    def __init__(self, run_dict, args_dict=None):
        self.is_ready = False
        if run_dict is not None:
            super().__init__(run_dict)
            self.change_args(args_dict)
            self.run()

    def change_args(self, args_dict):
        """Copy arguments"""
        for name, value in self.items():
            if name in args_dict:
                value["args"] = args_dict[name]["args"]
                value["kwargs"] = args_dict[name]["kwargs"]
            else:
                value["args"] = None
                value["kwargs"] = None

    def run(self):
        """Spawns threads"""
        for name, cdict in self.items():
            LOG.info("generating process")
            # fmt: off
            cdict["process"] = Thread(
                name=name,
                target=cdict["func"],
                args=cdict["args"],
                kwargs=cdict["kwargs"]
            )
            # fmt: on
            LOG.info("starting process")
            cdict["process"].daemon = True
            cdict["process"].start()
            LOG.info("%s started", name)
        LOG.info("all done")
        self.is_ready = True

    def status(self):
        """Return a status"""
        if self.is_ready:
            return Status(self)
        return []

    def stop(self, name=""):
        """Stop and exit"""
        raise NotImplementedError("Sorry")


class MumbleRunner(Runner):
    """A threads runner for Mumble"""

    def __init__(self, mumble_object, config, args_dict):
        self.mumble = mumble_object
        self.config = config
        super().__init__(self._config(), args_dict)

    def _config(self):
        """Initial configuration"""
        raise NotImplementedError("please inherit and implement")


class Audio(MumbleRunner):
    """Audio input/output"""

    def _config(self):
        self.stream_in = None
        self.stream_out = None
        self.in_user = None
        self.receive_ts = None
        self.in_running = None
        self.out_running = None
        self.out_volume = 1
        self.ptt_on_command = None
        """Initial configuration"""
        if not self.__init_audio():
            return None
        # fmt: off
        return {
            "input": {
                "func": self.__input_loop,
                "process": None
            },
            "output": {
                "func": self.__output_loop,
                "process": None
            }
        }
        # fmt: on

    def __init_audio(self):
        pa = pyaudio.PyAudio()
        pulse = None
        input_device_names, output_device_names = self.__scan_devices(pa)
        chunk_size = int(pymumble.constants.PYMUMBLE_SAMPLERATE * self.config["args"]["packet_length"])
        # Input audio
        if not self.config["input_disable"]:
            if pulse is None and self.config["input_pulse_name"]:
                pulse = PulseAudioHandler("mumblestream")
            pyaudio_input_index = self.__get_pyaudio_input_index(input_device_names)
            if pyaudio_input_index is None:
                LOG.error("cannot find PyAudio input device")
                return False
            self.stream_in = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=pymumble.constants.PYMUMBLE_SAMPLERATE,
                input=True,
                frames_per_buffer=chunk_size,
                input_device_index=pyaudio_input_index,
            )
            LOG.debug("input stream opened")
            if self.config["input_pulse_name"] is not None:  # redirect input to mumblestream with pulseaudio
                self.__move_input_pulseaudio(pulse, self.config["input_pulse_name"])
        # Output audio
        if not self.config["output_disable"]:
            if pulse is None and self.config["output_pulse_name"]:
                pulse = PulseAudioHandler("mumblestream")
            pyaudio_output_index = self.__get_pyaudio_output_index(output_device_names)
            if pyaudio_output_index is None:
                LOG.error("cannot find PyAudio output device")
                return False
            self.stream_out = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=pymumble.constants.PYMUMBLE_SAMPLERATE,
                output=True,
                frames_per_buffer=chunk_size,
                output_device_index=pyaudio_output_index,
            )
            LOG.debug("output stream opened")
            if self.config["output_pulse_name"] is not None:  # redirect output from mumblestream with pulseaudio
                self.__move_output_pulseaudio(pulse, self.config["output_pulse_name"])
        # All OK
        return True

    @staticmethod
    def __scan_devices(pa):
        """Scan audio devices handled by PyAudio"""
        info = pa.get_host_api_info_by_index(0)
        numdevices = info.get("deviceCount")
        input_device_names = {}
        output_device_names = {}
        for i in range(0, numdevices):
            if pa.get_device_info_by_host_api_device_index(0, i).get("maxInputChannels") > 0:
                device_info = pa.get_device_info_by_host_api_device_index(0, i)
                input_device_names[device_info["name"]] = device_info["index"]
            if pa.get_device_info_by_host_api_device_index(0, i).get("maxOutputChannels") > 0:
                device_info = pa.get_device_info_by_host_api_device_index(0, i)
                output_device_names[device_info["name"]] = device_info["index"]
        LOG.debug("input: %s", input_device_names)
        LOG.debug("output: %s", output_device_names)
        return input_device_names, output_device_names

    def __get_pyaudio_input_index(self, input_device_names):
        """Returns the PyAudio index of input device or None if no default"""
        if self.config["input_pulse_name"] is not None:
            pyaudio_name = "pulse"
        else:
            pyaudio_name = self.config.get("input_pyaudio_name", "default")
        return input_device_names.get(pyaudio_name)

    def __get_pyaudio_output_index(self, output_device_names):
        """Returns the PyAudio index of output device or None if no default"""
        if self.config["output_pulse_name"] is not None:
            pyaudio_name = "pulse"
        else:
            pyaudio_name = self.config.get("output_pyaudio_name", "default")
        return output_device_names.get(pyaudio_name)

    def __move_input_pulseaudio(self, pulse, input_pulse_name):
        """Moves the input to the given pulseaudio device"""
        pulse_source_index = pulse.get_source_index(input_pulse_name)
        pulse_source_output_index = pulse.get_own_source_output_index()
        if pulse_source_index is None or pulse_source_output_index is None:
            LOG.warning("cannot move source output %d to source %d", pulse_source_output_index, pulse_source_index)
        else:
            try:
                pulse.move_source_output(pulse_source_output_index, pulse_source_index)
                LOG.debug("moved pulseaudio source output %d to source %d", pulse_source_output_index, pulse_source_index)
            except Exception as ex:
                LOG.error("exception assigning pulseaudio source: %s", ex)

    def __move_output_pulseaudio(self, pulse, output_pulse_name):
        """Moves the output to the given pulseaudio device"""
        pulse_sink_index = pulse.get_sink_index(output_pulse_name)
        pulse_sink_input_index = pulse.get_own_sink_input_index()
        if pulse_sink_index is None or pulse_sink_input_index is None:
            LOG.warning("cannot move pulseaudio sink input %d to sink %d", pulse_sink_input_index, pulse_sink_index)
        else:
            try:
                pulse.move_sink_input(pulse_sink_input_index, pulse_sink_index)
                LOG.debug("moved pulseaudio sink input %d to sink %d", pulse_sink_input_index, pulse_sink_index)
            except Exception as ex:
                LOG.error("exception assigning pulseaudio sink: %s", ex)

    def __mute_output_pulseaudio(self, pulse):
        pulse_sink_input_index = pulse.get_own_sink_input_index()
        if pulse_sink_input_index is None:
            LOG.warning("cannot mute pulseaudio sink input %d", pulse_sink_input_index)
        else:
            try:
                pulse.mute_sink_input(pulse_sink_input_index, True)
                LOG.debug("muted pulseaudio sink input %d", pulse_sink_input_index)
            except Exception as ex:
                LOG.error("exception muting pulseaudio sink input %d: %s", pulse_sink_input_index, ex)

    @staticmethod
    def __level(audio_bytes):
        """Return maximum signal chunk magnitude"""
        alldata = bytearray()
        alldata.extend(audio_bytes)
        data = np.frombuffer(alldata, dtype=np.short)
        return max(abs(data))

    def __sound_received_handler(self, user, soundchunk):
        """Pymumble sound received callback"""
        if self.in_user is None:
            LOG.debug("start receiving from %s", user["name"])
            self.in_user = user["name"]
            if self.ptt_on_command is not None:  # PTT on
                run_ptt_on_command = subprocess.run(
                    self.ptt_on_command, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                LOG.debug("PTT on exited with code %d", run_ptt_on_command.returncode)
        if self.stream_out is not None and user["name"] == self.in_user:
            self.receive_ts = time.time()
            np_audio = np.frombuffer(soundchunk.pcm, dtype=np.short)
            np_audio = (np_audio * self.out_volume).astype(np.short)
            self.stream_out.write(np_audio.tobytes())

    def __output_loop(self):
        """Output process"""
        if self.config["output_disable"]:
            LOG.info("output disabled")
            return None
        self.out_volume = self.config["audio_output_volume"]
        self.ptt_on_command = " ".join(self.config["ptt_on_command"]) if self.config["ptt_command_support"] else None
        self.out_running = True
        try:
            self.mumble.callbacks.set_callback(CLBK_SOUNDRECEIVED, self.__sound_received_handler)
            while self.out_running:
                if self.receive_ts is not None and time.time() > self.receive_ts + 1:
                    LOG.debug("stop receiving from %s", self.in_user)
                    if self.config["ptt_command_support"]:  # PTT off
                        ptt_off_command = " ".join(self.config["ptt_off_command"])
                        run_ptt_off_command = subprocess.run(
                            ptt_off_command, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
                        LOG.debug("PTT off exited with code %d", run_ptt_off_command.returncode)
                    self.receive_ts = None
                    self.in_user = None
                time.sleep(0.1)
        finally:
            LOG.debug("terminating")
            self.mumble.callbacks.remove_callback(CLBK_SOUNDRECEIVED, self.__sound_received_handler)
            self.stream_out.close()
            LOG.debug("output stream closed")
        return True

    def __input_loop(self):
        """Input process"""
        if self.config["input_disable"]:
            LOG.info("input disabled")
            return None
        chunk_size = int(pymumble.constants.PYMUMBLE_SAMPLERATE * self.config["args"]["packet_length"])
        self.in_running = True
        try:
            while self.in_running:
                data = self.stream_in.read(chunk_size, exception_on_overflow = False)
                if self.__level(data) > self.config["audio_threshold"]:
                    LOG.debug("audio on")
                    quiet_samples = 0
                    while quiet_samples < (self.config["vox_silence_time"] * (1 / self.config["args"]["packet_length"])):
                        self.mumble.sound_output.add_sound(data)
                        data = self.stream_in.read(chunk_size, exception_on_overflow = False)
                        if self.__level(data) < self.config["audio_threshold"]:
                            quiet_samples = quiet_samples + 1
                        else:
                            quiet_samples = 0
                    LOG.debug("audio off")
        finally:
            LOG.debug("terminating")
            self.stream_in.close()
            LOG.debug("input stream closed")
        return True

    def stop(self, name=""):
        """Stop the runnin threads"""
        self.in_running = False
        self.out_running = False


class AudioPipe(MumbleRunner):
    """Audio pipe"""

    def _config(self):
        """Initial configuration"""
        # fmt: off
        return {
            "PipeInput": {
                "func": self.__input_loop,
                "process": None
            },
            "PipeOutput": {
                "func": self.__output_loop,
                "process": None
            }
        }
        # fmt: on

    def __output_loop(self, _):
        """Output process"""
        return None

    def __input_loop(self, packet_length, path):
        """Input process"""
        ckunk_size = int(pymumble.constants.PYMUMBLE_SAMPLERATE * packet_length)
        while True:
            with open(path) as fifo_fd:
                while True:
                    data = fifo_fd.read(ckunk_size)
                    #self.mumble.sound_output.add_sound(data)
                    if self.__level(data) > self.config["audio_threshold"]:
                        LOG.debug("audio on")
                        quiet_samples = 0
                        while quiet_samples < (self.config["vox_silence_time"] * (1 / self.config["args"]["packet_length"])):
                            self.mumble.sound_output.add_sound(data)
                            data = fifo_fd.read(ckunk_size)
                            if self.__level(data) < self.config["audio_threshold"]:
                                quiet_samples = quiet_samples + 1
                            else:
                                quiet_samples = 0
                        LOG.debug("audio off")


    def stop(self, name=""):
        """Stop the runnin threads"""

MAX_SUPERFRAME_SIZE = 320   # maximum size of incoming UDP audio buffer

# A lot of this code is from op25's audio.py and sockaudio.py
class AudioUdp(MumbleRunner):
    """Audio UDP stream"""

    def _config(self):
        """Initial configuration"""
        # fmt: off
        return {
            "input": {
                "func": self.__input_loop,
                "process": None
            },
            "output": {
                "func": self.__output_loop,
                "process": None
            }
        }
        # fmt: on

    def __output_loop(self):
        """Output process"""
        return None

    def __input_loop(self):
        """Input process"""
        chunk_size = int(pymumble.constants.PYMUMBLE_SAMPLERATE * self.config["args"]["packet_length"])

        self.ratecv_state = None
        self.audio_buffer = bytearray()
        self.two_channels = self.config["input_udp_two_channels"]

        self.__setup_sockets(self.config["input_udp_host"], self.config["input_udp_port"], self.config["input_udp_multicast"])

        while True:
            data = self.__read(chunk_size)
            #self.mumble.sound_output.add_sound(data)
            if self.__level(data) > self.config["audio_threshold"]:
                LOG.debug("audio on")
                quiet_samples = 0
                while quiet_samples < (self.config["vox_silence_time"] * (1 / self.config["args"]["packet_length"])):
                    self.mumble.sound_output.add_sound(data)
                    data = self.__read(chunk_size)
                    if self.__level(data) < self.config["audio_threshold"]:
                        quiet_samples = quiet_samples + 1
                    else:
                        quiet_samples = 0
                LOG.debug("audio off")

    def __read_test(self, read_size):
        data = bytearray()

        while len(data) < read_size:
            data_chunk = self.__read_one()
            if len(data_chunk) > 0:
                data = data + data_chunk

        return data

    def __read_two(self, read_size):
        data, address = self.sock_a.recvfrom(read_size) #MAX_SUPERFRAME_SIZE)
        #return self.__interleave(data, data)
        #return bytearray(data)
        return data

    def __read(self, read_size):
        data = self.__read_one()
        #data = bytearray()

        #LOG.debug("read_size %d" % read_size)

        #while len(data) < read_size:
        #    LOG.debug("len(data) %d" % len(data))
        #    data_chunk = self.__read_one()
        #    if len(data_chunk) > 0:
        #        data.extend(data_chunk)
        ##        data = data + data_chunk

        if len(data) > 0:
            channels = 2 # if self.two_channels else 1
            new_data, self.ratecv_state = audioop.ratecv(data, 2, channels, 16000, 48000, self.ratecv_state)
        else:
            new_data = data

        return new_data

    def __read_one(self):
        out_data = bytearray()
        readable, writable, exceptional = select.select( [self.sock_a, self.sock_b], [], [self.sock_a, self.sock_b], 5.0)
        in_a = None
        in_b = None
        data_a = bytearray()
        data_b = bytearray()
        flag_a = -1
        flag_b = -1

        # Check for select() polling timeout
        if (not readable) and (not writable) and (not exceptional):
            return out_data

        # Data received on the udp port is 320 bytes for an audio frame or 2 bytes for a flag
        if self.sock_a in readable:
            in_a = self.sock_a.recvfrom(MAX_SUPERFRAME_SIZE)

        if self.sock_b in readable:
            in_b = self.sock_b.recvfrom(MAX_SUPERFRAME_SIZE)

        if in_a is not None:
            len_a = len(in_a[0])
            if len_a == 2:
                flag_a = np.frombuffer(in_a[0], dtype=np.int16)[0]
            elif len_a > 0:
                data_a = in_a[0]

        if in_b is not None:
            len_b = len(in_b[0])
            if len_b == 2:
                flag_b = np.frombuffer(in_b[0], dtype=np.int16)[0]
            elif len_b > 0:
                data_b = in_b[0]

        if (flag_a == 0) or (flag_b == 0):
            return out_data

        if (((flag_a == 1) and (flag_b == 1)) or
            ((flag_a == 1) and (in_b is None)) or
            ((flag_b == 1) and (in_a is None))):
            return out_data

        gain = float(self.config["input_udp_audio_gain"])
        if not self.two_channels:
            data_a = self.__scale(gain, data_a)
            out_data = self.__interleave(data_a, data_a)
        else:
            data_a = self.__scale(gain, data_a)
            data_b = self.__scale(gain, data_b)
            out_data = self.__interleave(data_a, data_b)

        return out_data

    def __setup_sockets(self, udp_host, udp_port, multicast):
        LOG.debug("Listening on %s:%d\n" % (udp_host, udp_port))
        self.sock_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock_a.setblocking(0)
        self.sock_b.setblocking(0)

        if multicast:
            ##self.sock_a.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ##self.sock_b.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock_a.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            self.sock_b.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)

            membership = struct.pack("4sl", socket.inet_aton(udp_host), socket.INADDR_ANY)
            self.sock_a.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            self.sock_b.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)

        self.sock_a.bind((udp_host, udp_port))
        self.sock_b.bind((udp_host, udp_port + 2))

    def __close_sockets(self):
        if self.sock_a is not None:
            self.sock_a.close()
        if self.sock_b is not None:
            self.sock_b.close()
        return

    @staticmethod
    def __level(audio_bytes):
        """Return maximum signal chunk magnitude"""
        if len(audio_bytes) == 0:
            return 0

        alldata = bytearray()
        alldata.extend(audio_bytes)
        data = np.frombuffer(alldata, dtype=np.short)
        return max(abs(data))

    def __scale(self, gain, data):  # crude amplitude scaler (volume) for S16_LE samples
        arr = np.array(np.frombuffer(data, dtype=np.int16), dtype=np.float32)
        result = np.zeros(len(arr), dtype=np.int16)
        arr = np.clip(arr*gain, -32767, 32766, out=result)
        return result.tobytes('C')

    def __interleave(self, data_a, data_b):
        arr_a = np.frombuffer(data_a, dtype=np.int16)
        arr_b = np.frombuffer(data_b, dtype=np.int16)
        d_len = max(len(arr_a), len(arr_b))
        result = np.zeros(d_len*2, dtype=np.int16)
        if len(arr_a):
            # copy arr_a to result[0,2,4, ...]
            result[ range(0, len(arr_a)*2, 2) ] = arr_a
        if len(arr_b):
            # copy arr_b to result[1,3,5, ...]
            result[ range(1, len(arr_b)*2, 2) ] = arr_b
        return result.tobytes('C')

    def stop(self, name=""):
        """Stop the runnin threads"""
        self.__close_sockets()

class HttpHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type','text/html')
        self.end_headers()
        self.wfile.write(bytes("", "utf8"))

        LOG.info("Metadata update: %s" % self.path)

class IcecastMetadataReceiver(MumbleRunner):
    """Icecast Metadata Receiver"""

    def _config(self):
        """Initial configuration"""
        # fmt: off
        return {
            "icecast_input": {
                "func": self.__input_loop,
                "process": None
            }
            #,
            #"output": {
            #    "func": self.__output_loop,
            #    "process": None
            #}
        }
        # fmt: on

    def __output_loop(self):
        """Output process"""
        return None

    def __input_loop(self):
        """Input process"""
        # Bind to bind_ip:bind_port
        # Receive input
        # Update Mumble user comment
        # Forward to icecast_metadata_fwd_url_base if receive data

        self.meta_server = ThreadingHTTPServer((self.config["icecast_metadata_bind_ip"], self.config["icecast_metadata_bind_port"]), HttpHandler)
        self.meta_server.serve_forever()

    def stop(self, name=""):
        """Stop the runnin threads"""

        self.meta_server.server_close()


def prepare_mumble(host, port, user, password="", certfile=None, keyfile=None, codec_profile="audio", bandwidth=96000, channel=None, debug=False):
    """Will configure the pymumble object and return it"""

    try:
        mumble = pymumble.Mumble(host, user, port=port, certfile=certfile, keyfile=keyfile, password=password, debug=debug)
    except Exception as ex:
        LOG.error("cannot commect to %s: %s", host, ex)
        return None

    mumble.set_application_string(f"mumblestream ({__version__})")
    mumble.set_codec_profile(codec_profile)
    mumble.set_receive_sound(1)  # Enable receiving sound from mumble server
    mumble.start()
    mumble.is_ready()
    mumble.set_bandwidth(bandwidth)
    if channel:
        try:
            mumble.channels.find_by_name(channel).move_in()
        except pymumble.channels.UnknownChannelError as ex:
            LOG.warning("tried to connect to channel: '%s' exception %s", channel, ex)
            LOG.info("Available Channels:")
            LOG.info(mumble.channels)
            return None
    return mumble


def get_config(args):
    """Get parameters from the optional config file"""
    config = {}

    if args.config_path is not None and os.path.exists(args.config_path):
        with open(args.config_path) as f:
            configdata = json.load(f)
    else:
        configdata = {}

    config["args"] = {}
    config["args"]["host"] = configdata.get("host", None) # Mandatory
    config["args"]["port"] = configdata.get("port", 64738)
    config["args"]["user"] = configdata.get("user", None) # Mandatory
    config["args"]["password"] = configdata.get("password", "")
    config["args"]["packet_length"] = configdata.get("packet_length", pymumble.constants.PYMUMBLE_AUDIO_PER_PACKET)
    config["args"]["bandwidth"] = configdata.get("bandwidth", 48000)
    config["args"]["certfile"] = configdata.get("certfile", None)
    config["args"]["keyfile"] = configdata.get("keyfile", None)
    config["args"]["channel"] = configdata.get("channel", None)
    config["args"]["fifo_path"] = configdata.get("fifo_path", None)


    config["vox_silence_time"] = configdata.get("vox_silence_time", 3)
    config["audio_threshold"] = configdata.get("audio_threshold", 1000)
    config["audio_output_volume"] = configdata.get("audio_output_volume", 1)
    config["input_pyaudio_name"] = configdata.get("input_pyaudio_name", "default")
    config["input_pulse_name"] = configdata.get("input_pulse_name")
    config["input_disable"] = configdata.get("input_disable", 0) != 0
    config["input_udp"] = configdata.get("input_udp", False)
    config["input_udp_host"] = configdata.get("input_udp_host", "0.0.0.0")
    config["input_udp_port"] = configdata.get("input_udp_port", 10000)
    config["input_udp_multicast"] = configdata.get("input_udp_multicast", False)
    config["input_udp_two_channels"] = configdata.get("input_udp_two_channels", False)
    config["input_udp_audio_gain"] = configdata.get("input_udp_audio_gain", 1.0)

    config["output_pyaudio_name"] = configdata.get("output_pyaudio_name", "default")
    config["output_pulse_name"] = configdata.get("output_pulse_name")
    config["output_disable"] = configdata.get("output_disable", 0) != 0
    config["ptt_on_command"] = configdata.get("ptt_on_command")
    config["ptt_off_command"] = configdata.get("ptt_off_command")
    config["ptt_command_support"] = not (config["ptt_on_command"] is None or config["ptt_off_command"] is None)
    config["icecast_metadata_support"] = configdata.get("icecast_metadata_support", False)
    config["icecast_metadata_bind_ip"] = configdata.get("icecast_metadata_bind_ip", "127.0.0.1")
    config["icecast_metadata_bind_port"] = configdata.get("icecast_metadata_bind_port", 8888)
    config["icecast_metadata_password"] = configdata.get("icecast_metadata_password", None)
    config["icecast_metadata_fwd_url_base"] = configdata.get("icecast_metadata_fwd_url_base", None)
    config["logging_level"] = configdata.get("logging_level", "warning")
    config["debug_mumble"] = configdata.get("debug_mumble", False)

    args_dict = vars(args)
    for key in args_dict:
        if args_dict[key] is not None:
            config["args"][key] = args_dict[key]

    if config["logging_level"] == "debug":
        print("Configuration: %s" % config)

    return config

def validate_config(config):
    is_valid = True
    errors = []

    if "args" in config and config["args"] is not None:
        args = config["args"]

        if "host" not in args or args["host"] is None:
            errors.append("No host parameter")
            is_valid = False

        if "user" not in args or args["user"] is None:
            errors.append("No user parameter")
            is_valid = False
    else:
        errors.append("No args object")
        is_valid = False

    return (is_valid, errors)


def main(preserve_thread=True):
    """swallows parameter. TODO: move functionality away"""
    parser = argparse.ArgumentParser(description="Alsa input to mumble")
    # fmt: off
    parser.add_argument("-H", "--host", dest="host", type=str,
                        help="A hostame of a mumble server")
    parser.add_argument("-P", "--port", dest="port", type=int,
                        help="A port of a mumble server")
    parser.add_argument("-u", "--user", dest="user", type=str,
                        help="Username you wish, Default=mumble")
    parser.add_argument("-p", "--password", dest="password", type=str,
                        help="Password if server requires one")
    parser.add_argument("-s", "--setpacketlength", dest="packet_length", type=int,
                        help="Length of audio packet in seconds. Lower values mean less delay. Default 0.02 WARNING:Lower values could be unstable")
    parser.add_argument("-b", "--bandwidth", dest="bandwidth", type=int,
                        help="Bandwith of the bot (in bytes/s). Default=48000")
    parser.add_argument("-c", "--certificate", dest="certfile", type=str,
                        help="Path to an optional openssl certificate file")
    parser.add_argument("-k", "--key", dest="keyfile", type=str,
                        help="Path to an optional openssl key file")
    parser.add_argument("-C", "--channel", dest="channel", type=str,
                        help="Channel name as string")
    parser.add_argument("-f", "--fifo", dest="fifo_path", type=str,
                        help="Read from FIFO (EXPERMENTAL)")
    parser.add_argument("--config", dest="config_path", type=str, default="config.json",
                        help="Configuration file")
    # fmt: on
    args = parser.parse_args()
    config = get_config(args)
    #config["args"] = args

    config_is_valid, config_errors = validate_config(config)
    if not config_is_valid:
        print("Configuration errors:")
        for error in config_errors:
            print("    * %s" % error)
        return

    log_level = logging.getLevelName(config["logging_level"].upper())
    LOG.setLevel(log_level)

    config_args = config["args"]
    # fmt: off
    mumble = prepare_mumble(
            config_args["host"], config_args["port"],
            config_args["user"], config_args["password"],
            config_args["certfile"], config_args["keyfile"],
            "audio", config_args["bandwidth"], config_args["channel"], config["debug_mumble"])
    # fmt: on

    if mumble is None:
        LOG.critical("cannot connect to Mumble server or channel")
        return 1

    global_mumble = mumble

    # fmt: off
    if config["icecast_metadata_support"]:
        icecast_receiver = IcecastMetadataReceiver(
            mumble,
            config,
            {
                "icecast_input": {
                    "args": (),
                    "kwargs": None
                }
                #,
                #"icecast_output": {
                #    "args": (),
                #    "kwargs": None
                #}
            },
        )


    # fmt: on

    # fmt: off
    if config["args"]["fifo_path"]:
        audio = AudioPipe(
            mumble,
            config,
            {
                "output": {
                    "args": (config["args"]["packet_length"],),
                    "kwargs": None
                },
                "input": {
                    "args": (config["args"]["packet_length"], config["args"]["fifo_path"]),
                    "kwargs": None
                },
            },
        )
    elif config["input_udp"]:
        audio = AudioUdp(
            mumble,
            config,
            {
                "output": {
                    "args": [],
                    "kwargs": None
                },
                "input": {
                    "args": [],
                    "kwargs": None
                },
            },
        )

    else:
        audio = Audio(
            mumble,
            config,
            {
                "output": {
                    "args": [],
                    "kwargs": None
                },
                "input": {
                    "args": [],
                    "kwargs": None
                }
            }
        )
    # fmt: on
    if preserve_thread:
        while True:
            try:
                LOG.info(audio.status())
                if config["icecast_metadata_support"]:
                    LOG.info(icecast_receiver.status())
                time.sleep(60)
            except KeyboardInterrupt:
                LOG.info("terminating")
                audio.stop()
                time.sleep(1)
                return 0
            except Exception as ex:
                LOG.error("exception %s", ex)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
