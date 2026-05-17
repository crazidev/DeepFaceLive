import multiprocessing
import os
import platform
import struct
import subprocess
import threading
import time
from datetime import datetime
from enum import IntEnum
from multiprocessing import shared_memory
from typing import List, Tuple, Union

import cv2
import numpy as np
from xlib import os as lib_os
from xlib.image import ImageProcessor
from xlib.mp import csw as lib_csw

from .BackendBase import (BackendConnection, BackendConnectionData, BackendDB,
                          BackendHost, BackendWeakHeap, BackendWorker,
                          BackendWorkerState)


class _SourceType(IntEnum):
    CAMERA_DEVICE = 0
    NETWORK_STREAM = 1
    WEBRTC_STREAM = 2

_SourceType_names = { _SourceType.CAMERA_DEVICE : 'Camera device',
                      _SourceType.NETWORK_STREAM : 'Network stream',
                      _SourceType.WEBRTC_STREAM : 'WebRTC (Browser)',
                    }


class CameraSource(BackendHost):
    SOURCE_TYPE_CAMERA = _SourceType.CAMERA_DEVICE
    SOURCE_TYPE_NETWORK = _SourceType.NETWORK_STREAM
    SOURCE_TYPE_WEBRTC = _SourceType.WEBRTC_STREAM

    def __init__(self, weak_heap :  BackendWeakHeap, bc_out : BackendConnection, backend_db : BackendDB = None):
        super().__init__(backend_db=backend_db,
                         sheet_cls=Sheet,
                         worker_cls=CameraSourceWorker,
                         worker_state_cls=WorkerState,
                         worker_start_args=[weak_heap, bc_out] )

    def get_control_sheet(self) -> 'Sheet.Host': return super().get_control_sheet()


class _StreamProtocol(IntEnum):
    UDP = 0
    SRT = 1
    RTMP = 2
    RTSP = 3


_StreamProtocol_names = {
    _StreamProtocol.UDP: '@QCameraSource.protocol_udp',
    _StreamProtocol.SRT: '@QCameraSource.protocol_srt',
    _StreamProtocol.RTMP: '@QCameraSource.protocol_rtmp',
    _StreamProtocol.RTSP: '@QCameraSource.protocol_rtsp',
}


class StreamEnv:
    __slots__ = ('bind_host', 'client_host', 'ports', 'default_protocol')

    def __init__(self, bind_host : str, client_host : Union[str, None], ports : dict, default_protocol : _StreamProtocol):
        self.bind_host = bind_host
        self.client_host = client_host
        self.ports = ports
        self.default_protocol = default_protocol


def _parse_int_env(name : str, default : int) -> int:
    v = os.environ.get(name)
    if v is None or len(str(v).strip()) == 0:
        return default
    try:
        return int(v.strip(), 10)
    except ValueError:
        return default


def read_stream_env() -> StreamEnv:
    bind_host = (os.environ.get('DFL_STREAM_BIND_HOST') or '0.0.0.0').strip() or '0.0.0.0'
    ch = (os.environ.get('DFL_STREAM_CLIENT_HOST') or '').strip()
    client_host = ch if ch else None
    ports = {
        # Default 18766: 1238/1234 often collide with other services or a second listener on RunPod.
        _StreamProtocol.UDP: _parse_int_env('DFL_STREAM_PORT_UDP', 18766),
        # Default 8890: 8888 often conflicts with Jupyter on RunPod / cloud notebooks.
        _StreamProtocol.SRT: _parse_int_env('DFL_STREAM_PORT_SRT', 8890),
        _StreamProtocol.RTMP: _parse_int_env('DFL_STREAM_PORT_RTMP', 1935),
        _StreamProtocol.RTSP: _parse_int_env('DFL_STREAM_PORT_RTSP', 8554),
    }
    key = (os.environ.get('DFL_STREAM_PROTOCOL') or 'udp').strip().lower()
    pmap = {'udp': _StreamProtocol.UDP, 'srt': _StreamProtocol.SRT, 'rtmp': _StreamProtocol.RTMP, 'rtsp': _StreamProtocol.RTSP}
    default_protocol = pmap.get(key, _StreamProtocol.UDP)
    return StreamEnv(bind_host, client_host, ports, default_protocol)


def compose_network_stream_urls(protocol : _StreamProtocol, env : StreamEnv) -> Tuple[str, str]:
    port = env.ports[protocol]
    bh = env.bind_host or '0.0.0.0'
    ch = env.client_host
    if protocol == _StreamProtocol.UDP:
        listen = f'udp://{bh}:{port}'
        client = (f'udp://{ch}:{port}?pkt_size=1316' if ch else '')
    elif protocol == _StreamProtocol.SRT:
        listen = f'srt://{bh}:{port}?mode=listener'
        client = (f'srt://{ch}:{port}' if ch else '')
    elif protocol == _StreamProtocol.RTMP:
        listen = f'rtmp://{bh}:{port}/live/stream?listen=1'
        client = (f'rtmp://{ch}:{port}/live/stream' if ch else '')
    elif protocol == _StreamProtocol.RTSP:
        listen = f'rtsp://{bh}:{port}/live?listen=1'
        client = (f'rtsp://{ch}:{port}/live' if ch else '')
    else:
        listen, client = '', ''
    return listen, client

class _ResolutionType(IntEnum):
    RES_320x240 = 0
    RES_640x480 = 1
    RES_720x480 = 2
    RES_1280x720 = 3
    RES_1280x960 = 4
    RES_1366x768 = 5
    RES_1920x1080 = 6

_ResolutionType_names = {_ResolutionType.RES_320x240 : '320x240',
                         _ResolutionType.RES_640x480 : '640x480',
                         _ResolutionType.RES_720x480 : '720x480',
                         _ResolutionType.RES_1280x720 : '1280x720',
                         _ResolutionType.RES_1280x960 : '1280x960',
                         _ResolutionType.RES_1366x768 : '1366x768',
                         _ResolutionType.RES_1920x1080 : '1920x1080',
                        }

_ResolutionType_wh = {_ResolutionType.RES_320x240: (320,240),
                      _ResolutionType.RES_640x480: (640,480),
                      _ResolutionType.RES_720x480: (720,480),
                      _ResolutionType.RES_1280x720: (1280,720),
                      _ResolutionType.RES_1280x960: (1280,960),
                      _ResolutionType.RES_1366x768: (1366,768),
                      _ResolutionType.RES_1920x1080: (1920,1080),
                      }
class _DriverType(IntEnum):
    COMPATIBLE = 0
    DSHOW = 1
    MSMF = 2
    GSTREAMER = 3
    AVFOUNDATION = 4

_DriverType_names = { _DriverType.COMPATIBLE : 'Compatible',
                      _DriverType.DSHOW : 'DirectShow',
                      _DriverType.MSMF : 'Microsoft Media Foundation',
                      _DriverType.GSTREAMER : 'GStreamer',
                      _DriverType.AVFOUNDATION : 'AVFoundation (macOS)',
                    }

class _RotationType(IntEnum):
    ROTATION_0 = 0
    ROTATION_90 = 1
    ROTATION_180 = 2
    ROTATION_270 = 3

_RotationType_names = ['0 degrees', '90 degrees', '180 degrees', '270 degrees']


class CameraSourceWorker(BackendWorker):
    def get_state(self) -> 'WorkerState': return super().get_state()
    def get_control_sheet(self) -> 'Sheet.Worker': return super().get_control_sheet()

    def on_start(self, weak_heap : BackendWeakHeap, bc_out : BackendConnection):
        self.weak_heap = weak_heap
        self.bc_out = bc_out
        self.bcd_uid = 0
        self.pending_bcd = None
        self.vcap = None
        self.ffmpeg_proc = None
        self.ffmpeg_thread = None
        self.ffmpeg_starting = False
        self.last_ffmpeg_start_time = 0
        self.last_frame = None
        self.last_timestamp = 0
        self._stream_env = read_stream_env()
        self._stream_feed_connected_event = threading.Event()
        self._last_stream_wait_sent = None
        self._ffmpeg_consecutive_listen_failures = 0
        # WebRTC state
        self._webrtc_shm = None
        self._webrtc_proc = None
        self._webrtc_stop_event = None
        self._webrtc_last_seq = 0
        self._webrtc_port = _parse_int_env('DFL_WEBRTC_PORT', 9090)
        lib_os.set_timer_resolution(4)
        self.start_profile_timing()

        state, cs = self.get_state(), self.get_control_sheet()

        cs.source_type.call_on_selected(self.on_cs_source_type_selected)
        cs.stream_url.call_on_text(self.on_cs_stream_url)
        cs.stream_protocol.call_on_selected(self.on_cs_stream_protocol_selected)
        cs.driver.call_on_selected(self.on_cs_driver_selected)
        cs.device_idx.call_on_selected(self.on_cs_device_idx_selected)
        cs.resolution.call_on_selected(self.on_cs_resolution_selected)
        cs.fps.call_on_number(self.on_cs_fps)
        cs.rotation.call_on_selected(self.on_cs_rotation_selected)
        cs.flip_horizontal.call_on_flag(self.on_cs_flip_horizontal)
        cs.open_settings.call_on_signal(self.on_cs_open_settings)
        cs.load_settings.call_on_signal(self.on_cs_load_settings)
        cs.save_settings.call_on_signal(self.on_cs_save_settings)

        cs.source_type.enable()
        cs.source_type.set_choices(_SourceType, _SourceType_names, none_choice_name=None)
        cs.source_type.select(state.source_type if state.source_type is not None else _SourceType.CAMERA_DEVICE)

        cs.stream_url.enable()
        cs.stream_url.set_text(state.stream_url if state.stream_url is not None else '')

        cs.stream_protocol.enable()
        cs.stream_protocol.set_choices(_StreamProtocol, _StreamProtocol_names, none_choice_name=None)
        # Non-empty DFL_STREAM_PROTOCOL overrides saved userdata (servers often set this per deploy).
        raw_proto = os.environ.get('DFL_STREAM_PROTOCOL')
        if raw_proto is not None and len(str(raw_proto).strip()) > 0 and state.source_type == _SourceType.NETWORK_STREAM:
            pmap = {'udp': _StreamProtocol.UDP, 'srt': _StreamProtocol.SRT, 'rtmp': _StreamProtocol.RTMP, 'rtsp': _StreamProtocol.RTSP}
            pu = pmap.get(str(raw_proto).strip().lower())
            if pu is not None:
                if state.stream_protocol != pu:
                    print(f"\033[96mNetwork stream: DFL_STREAM_PROTOCOL={raw_proto.strip()!r} overrides saved protocol\033[0m")
                state.stream_protocol = pu
        if state.stream_protocol is None:
            state.stream_protocol = self._stream_env.default_protocol
        cs.stream_protocol.select(state.stream_protocol)

        cs.stream_waiting.enable()
        cs.stream_waiting.set_flag(False)

        # WebRTC widgets
        cs.webrtc_url.enable()
        cs.webrtc_url.set_text('')
        cs.webrtc_waiting.enable()
        cs.webrtc_waiting.set_flag(False)

        if state.source_type == _SourceType.NETWORK_STREAM:
            self._apply_network_stream_from_state()
            cs.webrtc_url.disable()
            cs.webrtc_waiting.disable()
        elif state.source_type == _SourceType.WEBRTC_STREAM:
            cs.stream_protocol.disable()
            cs.stream_client_hint.disable()
            cs.stream_waiting.disable()
            self._start_webrtc_server()
        else:
            cs.stream_protocol.disable()
            cs.stream_client_hint.disable()
            cs.stream_waiting.disable()
            cs.webrtc_url.disable()
            cs.webrtc_waiting.disable()

        if state.source_type == _SourceType.CAMERA_DEVICE:
            cs.driver.enable()
        cs.driver.set_choices(_DriverType, _DriverType_names, none_choice_name='@misc.menu_select')
        
        default_driver = _DriverType.COMPATIBLE
        if platform.system() == 'Windows':
            default_driver = _DriverType.DSHOW
        elif platform.system() == 'Darwin':
            default_driver = _DriverType.AVFOUNDATION
            
        cs.driver.select(state.driver if state.driver is not None else default_driver)

        if state.source_type == _SourceType.CAMERA_DEVICE:
            if platform.system() == 'Windows':
                from xlib.api.win32 import ole32
                from xlib.api.win32 import dshow
                ole32.CoInitializeEx(0,0)
                choices = [ f'{idx} : {name}' for idx, name in enumerate(dshow.get_video_input_devices_names()) ]
                choices += [ f'{idx}' for idx in range(len(choices), 16) ]
                ole32.CoUninitialize()
            else:
                choices = [ f'{idx}' for idx in range(16) ]

            cs.device_idx.enable()
            cs.device_idx.set_choices(choices, none_choice_name='@misc.menu_select')
            cs.device_idx.select(state.device_idx)

            cs.resolution.enable()
            cs.resolution.set_choices(_ResolutionType, _ResolutionType_names, none_choice_name=None)
            cs.resolution.select(state.resolution if state.resolution is not None else _ResolutionType.RES_640x480)
        else:
            cs.driver.disable()
            cs.device_idx.disable()
            cs.resolution.disable()

        if (state.source_type == _SourceType.CAMERA_DEVICE and state.device_idx is not None and state.driver is not None) or \
           (state.source_type == _SourceType.NETWORK_STREAM and state.stream_url is not None and len(state.stream_url) != 0) or \
           (state.source_type == _SourceType.WEBRTC_STREAM):

            if state.source_type == _SourceType.CAMERA_DEVICE:
                cv_api = {_DriverType.COMPATIBLE: cv2.CAP_ANY,
                          _DriverType.DSHOW: cv2.CAP_DSHOW,
                          _DriverType.MSMF: cv2.CAP_MSMF,
                          _DriverType.GSTREAMER: cv2.CAP_GSTREAMER,
                          _DriverType.AVFOUNDATION: getattr(cv2, 'CAP_AVFOUNDATION', cv2.CAP_ANY),
                          }[state.driver]

                print(f"\033[94mOpening camera {state.device_idx} with api {cv_api}\033[0m")
                vcap = cv2.VideoCapture(state.device_idx, cv_api)
                if vcap.isOpened():
                    print("\033[92mCamera opened successfully\033[0m")
                    self.vcap = vcap
                    w, h = _ResolutionType_wh[state.resolution]

                    vcap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                    vcap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                else:
                    print("\033[91mFailed to open camera\033[0m")
            elif state.source_type == _SourceType.NETWORK_STREAM:
                print(f"\033[94mOpening stream {state.stream_url}\033[0m")
                vcap = None
                # OpenCV + in-worker FFmpeg would both try to bind listener ports (SRT/RTMP/RTSP/UDP).
                if self._is_network_listener_url(state.stream_url):
                    print("\033[93mListener URL: skipping OpenCV; FFmpeg binds in the worker loop\033[0m")
                else:
                    vcap = cv2.VideoCapture(state.stream_url, cv2.CAP_FFMPEG)
                    if vcap.isOpened():
                        print("\033[92mStream opened successfully\033[0m")
                        self.vcap = vcap
                    else:
                        print("\033[93mFailed to open stream (using FFmpeg for listener / push URLs)\033[0m")
            else:
                # WEBRTC_STREAM — no vcap needed; frames come via shared memory
                vcap = None
                print(f"\033[94mWebRTC stream mode — waiting for browser on port {self._webrtc_port}\033[0m")

            network_has_url = state.source_type == _SourceType.NETWORK_STREAM and state.stream_url
            is_webrtc = state.source_type == _SourceType.WEBRTC_STREAM
            if (vcap is not None and vcap.isOpened()) or network_has_url or is_webrtc:
                if state.source_type == _SourceType.NETWORK_STREAM and vcap is not None and not vcap.isOpened():
                    vcap.release()
                cs.fps.enable()
                cs.fps.set_config(lib_csw.Number.Config(min=0, max=240, step=1.0, decimals=2, zero_is_auto=True, allow_instant_update=False))
                cs.fps.set_number(state.fps if state.fps is not None else 0)

                cs.rotation.enable()
                cs.rotation.set_choices(_RotationType, _RotationType_names, none_choice_name=None)
                cs.rotation.select(state.rotation if state.rotation is not None else _RotationType.ROTATION_0)

                cs.flip_horizontal.enable()
                cs.flip_horizontal.set_flag(state.flip_horizontal if state.flip_horizontal is not None else False)

                if platform.system() == 'Windows':
                    cs.open_settings.enable()

                cs.load_settings.enable()
                cs.save_settings.enable()
            else:
                if state.source_type == _SourceType.CAMERA_DEVICE:
                    cs.device_idx.unselect()

    def _apply_network_stream_from_state(self):
        state, cs = self.get_state(), self.get_control_sheet()
        if state.stream_protocol is None:
            state.stream_protocol = self._stream_env.default_protocol
        listen, client = compose_network_stream_urls(state.stream_protocol, self._stream_env)
        state.stream_url = listen
        cs.stream_url.set_text(listen)
        if client:
            cs.stream_client_hint.set_text(client)
            cs.stream_client_hint.enable()
        else:
            cs.stream_client_hint.set_text('')
            cs.stream_client_hint.disable()
        self.save_state()

    def _is_network_listener_url(self, url : str) -> bool:
        if not url:
            return False
        u = url.lower()
        return 'listen=1' in u or 'mode=listener' in u or u.startswith('udp://')

    def _update_stream_waiting_ui(self):
        state, cs = self.get_state(), self.get_control_sheet()
        if not cs.stream_waiting.is_enabled():
            return
        url = state.stream_url or ''
        waiting = (
            state.source_type == _SourceType.NETWORK_STREAM
            and self._is_network_listener_url(url)
            and self.is_started()
            and (
                self.ffmpeg_starting
                or (
                    self.ffmpeg_proc is not None
                    and self.ffmpeg_proc.poll() is None
                    and not self._stream_feed_connected_event.is_set()
                )
            )
        )
        if waiting != self._last_stream_wait_sent:
            self._last_stream_wait_sent = waiting
            cs.stream_waiting.set_flag(waiting)

    def on_cs_stream_protocol_selected(self, idx, protocol : _StreamProtocol):
        state, cs = self.get_state(), self.get_control_sheet()
        if state.source_type != _SourceType.NETWORK_STREAM:
            return
        if state.stream_protocol == protocol:
            return
        state.stream_protocol = protocol
        self._apply_network_stream_from_state()
        if self.is_started():
            self.stop_ffmpeg()
            self.set_vcap(None)
            self.restart()

    def on_cs_source_type_selected(self, idx, source_type):
        cs, state = self.get_control_sheet(), self.get_state()
        if state.source_type != source_type:
            state.source_type = source_type
            if source_type == _SourceType.NETWORK_STREAM:
                cs.stream_protocol.enable()
                cs.stream_waiting.enable()
                self._apply_network_stream_from_state()
                cs.webrtc_url.disable()
                cs.webrtc_waiting.disable()
                cs.webrtc_waiting.set_flag(False)
            elif source_type == _SourceType.WEBRTC_STREAM:
                cs.stream_protocol.disable()
                cs.stream_client_hint.disable()
                cs.stream_waiting.disable()
                cs.stream_waiting.set_flag(False)
                self._last_stream_wait_sent = None
                cs.webrtc_url.enable()
                cs.webrtc_waiting.enable()
            else:
                cs.stream_protocol.disable()
                cs.stream_client_hint.disable()
                cs.stream_waiting.disable()
                cs.stream_waiting.set_flag(False)
                self._last_stream_wait_sent = None
                cs.webrtc_url.disable()
                cs.webrtc_waiting.disable()
                cs.webrtc_waiting.set_flag(False)
            self.save_state()
            if self.is_started():
                self.stop_ffmpeg()
                self.stop_webrtc()
                self.set_vcap(None)
                self.restart()

    def on_cs_stream_url(self, stream_url):
        cs, state = self.get_control_sheet(), self.get_state()
        if state.stream_url != stream_url:
            state.stream_url = stream_url
            self.save_state()
            if self.is_started():
                self.stop_ffmpeg()
                self.restart()

    def on_cs_driver_selected(self, idx, driver):
        cs, state = self.get_control_sheet(), self.get_state()
        if state.driver != driver:
            state.driver = driver
            self.save_state()
            if self.is_started():
                self.restart()

    def on_cs_device_idx_selected(self, device_idx, device_name):
        cs, state = self.get_control_sheet(), self.get_state()
        if state.device_idx != device_idx:
            state.device_idx = device_idx
            self.save_state()
            if self.is_started():
                self.restart()

    def on_cs_resolution_selected(self, idx, resolution : _ResolutionType):
        state, cs = self.get_state(), self.get_control_sheet()
        if state.resolution != resolution:
            state.resolution = resolution
            self.save_state()
            if self.is_started():
                self.restart()

    def on_cs_fps(self, fps):
        state, cs = self.get_state(), self.get_control_sheet()
        cfg = cs.fps.get_config()
        fps = state.fps = np.clip(fps, cfg.min, cfg.max)
        cs.fps.set_number(fps)
        self.save_state()

    def on_cs_rotation_selected(self, idx, _rot_type : _RotationType):
        cs, state = self.get_control_sheet(), self.get_state()
        state.rotation = _rot_type
        self.save_state()

    def on_cs_flip_horizontal(self, flip_horizontal):
        state, cs = self.get_state(), self.get_control_sheet()
        state.flip_horizontal = flip_horizontal
        self.save_state()

    def on_cs_open_settings(self):
        cs, state = self.get_control_sheet(), self.get_state()
        if self.vcap is not None and self.vcap.isOpened():
            self.vcap.set(cv2.CAP_PROP_SETTINGS, 0)

    def on_cs_load_settings(self):
        cs, state = self.get_control_sheet(), self.get_state()

        vcap = self.vcap
        if vcap is not None:
            settings = state.settings_by_idx.get(state.device_idx, None)
            if settings is not None:
                for setting_name, value in settings.items():
                    setting_id = getattr(cv2, setting_name, None)
                    if setting_id is not None:
                        vcap.set(setting_id, value)

    def on_cs_save_settings(self):
        cs, state = self.get_control_sheet(), self.get_state()

        vcap = self.vcap
        if vcap is not None:
            settings = {}
            for setting_name in self._get_vcap_setting_name_list():
                setting_id = getattr(cv2, setting_name, None)
                if setting_id is not None:
                    settings[setting_name] = vcap.get(setting_id)
            state.settings_by_idx[state.device_idx] = settings
            self.save_state()

    def set_vcap(self, vcap):
        if self.vcap is not None:
            self.vcap.release()
        self.vcap = vcap

    def _parse_url_port_and_proto(self, url: str) -> Tuple[Union[int, None], Union[str, None]]:
        if not url:
            return None, None
        try:
            lower_url = url.lower()
            if lower_url.startswith("udp://"):
                proto = "udp"
            elif lower_url.startswith("srt://"):
                proto = "udp"  # SRT uses UDP under the hood
            elif lower_url.startswith("rtmp://"):
                proto = "tcp"
            elif lower_url.startswith("rtsp://"):
                proto = "tcp"
            else:
                proto = "tcp"
                
            parts = url.split("://", 1)
            if len(parts) < 2:
                return None, None
            
            host_port_path = parts[1]
            host_port = host_port_path.split("/", 1)[0].split("?", 1)[0]
            if ":" in host_port:
                port_str = host_port.split(":")[-1]
                port_digits = "".join(c for c in port_str if c.isdigit())
                if port_digits:
                    return int(port_digits), proto
        except Exception as e:
            print(f"\033[93mError parsing port/proto from url {url}: {e}\033[0m")
        return None, None

    def _ensure_port_is_free(self, port: int, proto: str):
        sys_platform = platform.system().lower()
        if sys_platform not in ['linux', 'darwin']:
            return

        print(f"\033[93m[Port Guard] Checking if {proto} port {port} is available...\033[0m")
        
        pids = []
        
        # 1. Try lsof
        try:
            out = subprocess.check_output(["lsof", "-t", f"-i{proto}:{port}"], stderr=subprocess.DEVNULL)
            pids = [p.strip() for p in out.decode().splitlines() if p.strip()]
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        # 2. Try fuser if no PIDs found with lsof
        if not pids:
            try:
                out = subprocess.check_output(["fuser", f"{port}/{proto}"], stderr=subprocess.DEVNULL)
                for word in out.decode().replace("/", " ").replace(":", " ").split():
                    if word.isdigit():
                        pids.append(word)
            except (subprocess.SubprocessError, FileNotFoundError):
                pass

        if pids:
            pids = list(set(pids)) # deduplicate
            # Filter out our own PID
            my_pid = str(os.getpid())
            pids = [p for p in pids if p != my_pid]

            if pids:
                print(f"\033[91m[Port Guard] Port {port}/{proto} is in use by PID(s): {', '.join(pids)}. Terminating them...\033[0m")
                for pid in pids:
                    try:
                        subprocess.run(["kill", "-15", pid], stderr=subprocess.DEVNULL)
                    except (subprocess.SubprocessError, FileNotFoundError):
                        pass
                
                # Wait up to 1 second for processes to exit
                for _ in range(10):
                    time.sleep(0.1)
                    still_alive = []
                    for pid in pids:
                        if sys_platform == 'linux':
                            if os.path.exists(f"/proc/{pid}"):
                                still_alive.append(pid)
                        else:
                            try:
                                os.kill(int(pid), 0)
                                still_alive.append(pid)
                            except OSError:
                                pass
                    if not still_alive:
                        break
                    pids = still_alive
                
                # If still alive, force kill
                if pids:
                    print(f"\033[91m[Port Guard] Force killing stubborn PID(s): {', '.join(pids)}...\033[0m")
                    for pid in pids:
                        try:
                            subprocess.run(["kill", "-9", pid], stderr=subprocess.DEVNULL)
                        except (subprocess.SubprocessError, FileNotFoundError):
                            pass
                    time.sleep(0.5)
            else:
                print(f"\033[92m[Port Guard] Port {port}/{proto} only bound by our own process.\033[0m")
        else:
            # Blanket kill with fuser just in case
            try:
                subprocess.run(["fuser", "-k", "-9", f"{port}/{proto}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
            print(f"\033[92m[Port Guard] Port {port}/{proto} is free.\033[0m")

    def _ffmpeg_reader(self, url):
        self.ffmpeg_starting = True
        stream_connected = False
        try:
            if url.startswith('udp://'):
                extra = []
                if 'reuse=1' not in url:
                    extra.append('reuse=1')
                if 'overrun_nonfatal=1' not in url:
                    extra.append('overrun_nonfatal=1')
                if extra:
                    url += ('&' if '?' in url else '?') + '&'.join(extra)

            is_file = os.path.exists(url)
            is_listener = 'listen=1' in url or 'mode=listener' in url or url.startswith('udp://')

            if is_listener:
                port, proto = self._parse_url_port_and_proto(url)
                if port is not None and proto is not None:
                    self._ensure_port_is_free(port, proto)

            # We try to probe the stream first to get resolution, unless it's a listener
            w, h = 1280, 720 # Default
            if not is_listener and not is_file:
                # If it's a file, we should probe it
                pass
            
            if is_file or (not is_listener):
                print(f"\033[94mProbing {'file' if is_file else 'stream'} {url}...\033[0m")
                probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', url]
                try:
                    res = subprocess.check_output(probe_cmd).decode().strip()
                    if 'x' in res:
                        w, h = map(int, res.split('x'))
                        print(f"\033[92mResolution: {w}x{h}\033[0m")
                except:
                    print("\033[93mFailed to probe, using default 1280x720\033[0m")
            else:
                print(f"\033[93mListener mode detected, waiting for connection on {url}\033[0m")

            cmd = ['ffmpeg']
            if is_listener:
                if url.startswith('rtsp://'):
                    cmd += ['-rtsp_flags', 'listen', '-listen_timeout', '-1']
                elif url.startswith('srt://') or url.startswith('udp://'):
                    # SRT and UDP listeners are handled via URL parameters or implied
                    pass
                else:
                    cmd += ['-listen', '1']
            
            # Low latency flags
            cmd += ['-fflags', 'nobuffer', '-flags', 'low_delay']

            if is_file:
                cmd += ['-re', '-stream_loop', '-1']
            cmd += ['-i', url, '-vf', 'scale=1280:720,format=bgr24', '-f', 'image2pipe', '-vcodec', 'rawvideo', '-loglevel', 'error', '-']
            
            # Force resolution to match our scale filter
            w, h = 1280, 720
            
            self.ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=w*h*3*10)

            self.ffmpeg_starting = False
            frame_size = w * h * 3
            while self.ffmpeg_proc is not None:
                raw_frame = self.ffmpeg_proc.stdout.read(frame_size)
                if len(raw_frame) != frame_size:
                    break
                if not stream_connected:
                    stream_connected = True
                    self._stream_feed_connected_event.set()
                    print(f"\033[92m[✓] Stream connected and receiving frames: {url}\033[0m")
                self.last_frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((h, w, 3))
        except Exception as e:
            print(f"\033[91mFFmpeg reader error: {e}\033[0m")
        finally:
            self.ffmpeg_starting = False
            if self.ffmpeg_proc:
                self.ffmpeg_proc.terminate()
                self.ffmpeg_proc = None
            if stream_connected:
                self._ffmpeg_consecutive_listen_failures = 0
            else:
                self._ffmpeg_consecutive_listen_failures = min(
                    self._ffmpeg_consecutive_listen_failures + 1, 10)
            print(f"\033[91m[X] FFmpeg reader stopped/disconnected: {url}\033[0m")

    def stop_ffmpeg(self):
        self.ffmpeg_starting = False
        self._stream_feed_connected_event.clear()
        if self.ffmpeg_proc:
            self.ffmpeg_proc.terminate()
            try:
                self.ffmpeg_proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.ffmpeg_proc.kill()
            self.ffmpeg_proc = None
        thr = self.ffmpeg_thread
        self.ffmpeg_thread = None
        if thr is not None and thr.is_alive():
            thr.join(timeout=2.0)
        self.last_frame = None
        cs = self.get_control_sheet()
        if cs.stream_waiting.is_enabled():
            cs.stream_waiting.set_flag(False)
        self._last_stream_wait_sent = None
        self._ffmpeg_consecutive_listen_failures = 0


    def _kill_port_process(self, port):
        import subprocess
        import platform
        import time
        print(f"\033[93m[WebRTC] Checking if port {port} is already in use...\033[0m")
        try:
            res = subprocess.run(['lsof', '-t', '-i', f'tcp:{port}'], capture_output=True, text=True)
            pids = [p.strip() for p in res.stdout.strip().split('\n') if p.strip().isdigit()]
            if pids:
                print(f"\033[91m[WebRTC] Port {port} is held by PIDs {pids}. Killing them...\033[0m")
                if platform.system() == 'Linux':
                    subprocess.run(['fuser', '-k', f'{port}/tcp'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    for pid in pids:
                        subprocess.run(['kill', '-9', pid])
                time.sleep(1.0) # give socket some time to release
        except Exception as e:
            print(f"[WebRTC] Warning: Error checking/killing port process: {e}")


    # ── WebRTC server management ──────────────────────────────────────

    def _start_webrtc_server(self):
        """Spawn the WebRTC server subprocess and set up shared memory."""
        from .webrtc_server import SHM_SIZE, run_webrtc_server

        cs = self.get_control_sheet()
        self.stop_webrtc()  # clean up any previous instance

        # Robust port release/cleanup before binding
        self._kill_port_process(self._webrtc_port)

        # Create shared memory
        self._webrtc_shm = shared_memory.SharedMemory(create=True, size=SHM_SIZE)
        self._webrtc_stop_event = multiprocessing.Event()
        self._webrtc_last_seq = 0

        port = self._webrtc_port
        self._webrtc_proc = multiprocessing.Process(
            target=run_webrtc_server,
            args=(self._webrtc_shm.name, port, self._webrtc_stop_event),
            daemon=True,
        )
        # Python's multiprocessing does not allow daemon processes to have children.
        # We temporarily disable the daemon flag of the current process to start the child, and then restore it.
        current_proc = multiprocessing.current_process()
        was_daemon = current_proc.daemon
        current_proc.daemon = False
        try:
            self._webrtc_proc.start()
        finally:
            current_proc.daemon = was_daemon


        # Determine the best friendly URL to display to the user
        public_url = os.environ.get('DFL_WEBRTC_PUBLIC_URL')
        if not public_url:
            public_host = os.environ.get('DFL_TURN_PUBLIC_HOST')
            if public_host:
                public_url = f'https://{public_host}' if port in (443, 80) else f'https://{public_host}:{port}'
        
        if not public_url:
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
            except Exception:
                local_ip = "localhost"
            public_url = f'https://{local_ip}:{port}'

        cs.webrtc_url.set_text(public_url)
        cs.webrtc_waiting.set_flag(True)
        print(f"\033[92m[WebRTC] Server started — open {public_url} in your browser\033[0m")

    def stop_webrtc(self):
        """Gracefully stop the WebRTC server and release shared memory."""
        cs = self.get_control_sheet()
        if self._webrtc_stop_event is not None:
            self._webrtc_stop_event.set()
        if self._webrtc_proc is not None:
            self._webrtc_proc.join(timeout=3.0)
            if self._webrtc_proc.is_alive():
                self._webrtc_proc.terminate()
                self._webrtc_proc.join(timeout=1.0)
            self._webrtc_proc = None
        self._webrtc_stop_event = None
        if self._webrtc_shm is not None:
            try:
                self._webrtc_shm.close()
                self._webrtc_shm.unlink()
            except Exception:
                pass
            self._webrtc_shm = None
        self._webrtc_last_seq = 0
        if cs.webrtc_waiting.is_enabled():
            cs.webrtc_waiting.set_flag(False)
        if cs.webrtc_url.is_enabled():
            cs.webrtc_url.set_text('')
        print("\033[93m[WebRTC] Server stopped\033[0m")

    def _read_webrtc_frame(self):
        """Read a frame from WebRTC shared memory if a new one is available."""
        from .webrtc_server import read_frame_from_shm
        if self._webrtc_shm is None:
            return None
        result = read_frame_from_shm(self._webrtc_shm)
        if result is None:
            return None
        w, h, seq, frame = result
        if seq == self._webrtc_last_seq:
            return None  # no new frame
        self._webrtc_last_seq = seq
        return frame

    def on_stop(self):
        self.stop_ffmpeg()
        self.stop_webrtc()
        super().on_stop()

    def on_tick(self):
        state, cs = self.get_state(), self.get_control_sheet()

        if state.source_type == _SourceType.CAMERA_DEVICE:
            if self.vcap is not None and not self.vcap.isOpened():
                self.set_vcap(None)

            if self.vcap is not None:
                ret, img = self.vcap.read()
                if ret:
                    self._process_frame(img)
        elif state.source_type == _SourceType.NETWORK_STREAM:
            self._update_stream_waiting_ui()
            if self.ffmpeg_proc is None and not self.ffmpeg_starting:
                if state.stream_url:
                    now = time.time()
                    backoff = min(
                        60.0,
                        5.0 * (2 ** min(self._ffmpeg_consecutive_listen_failures, 4)),
                    )
                    if now - self.last_ffmpeg_start_time > backoff:
                        self.last_ffmpeg_start_time = now
                        self._stream_feed_connected_event.clear()
                        self.ffmpeg_thread = threading.Thread(target=self._ffmpeg_reader, args=(state.stream_url,), daemon=True)
                        self.ffmpeg_thread.start()
            
            if self.last_frame is not None:
                img = self.last_frame
                self.last_frame = None
                self._process_frame(img)
        elif state.source_type == _SourceType.WEBRTC_STREAM:
            # Read frames from WebRTC shared memory
            frame = self._read_webrtc_frame()
            if frame is not None:
                # First frame received — update waiting indicator
                if cs.webrtc_waiting.is_enabled():
                    cs.webrtc_waiting.set_flag(False)
                self._process_frame(frame)
            else:
                time.sleep(0.001)

    def _process_frame(self, img):
        state, cs = self.get_state(), self.get_control_sheet()
        self.stop_profile_timing()
        self.start_profile_timing()
        timestamp = datetime.now().timestamp()
        fps = state.fps
        if fps is None:
            fps = 0
            
        if fps == 0 or ((timestamp - self.last_timestamp) > 1.0 / fps):
            if fps != 0:
                if timestamp - self.last_timestamp >= 1.0:
                    self.last_timestamp = timestamp
                else:
                    self.last_timestamp += 1.0 / fps

            ip = ImageProcessor(img)
            ip.ch(3).to_uint8()

            if state.source_type == _SourceType.CAMERA_DEVICE:
                w, h = _ResolutionType_wh[state.resolution]
                ip.fit_in(TW=w)

            rotation = state.rotation
            if rotation == _RotationType.ROTATION_90: ip.rotate90()
            elif rotation == _RotationType.ROTATION_180: ip.rotate180()
            elif rotation == _RotationType.ROTATION_270: ip.rotate270()

            if state.flip_horizontal:
                ip.flip_horizontal()

            img = ip.get_image('HWC')

            bcd_uid = self.bcd_uid = self.bcd_uid + 1
            bcd = BackendConnectionData(uid=bcd_uid)
            bcd.assign_weak_heap(self.weak_heap)
            
            if state.source_type == _SourceType.CAMERA_DEVICE:
                frame_name = f'Camera_{state.device_idx}_{bcd_uid:06}'
            elif state.source_type == _SourceType.WEBRTC_STREAM:
                frame_name = f'WebRTC_{bcd_uid:06}'
            else:
                frame_name = f'Stream_{bcd_uid:06}'
            
            bcd.set_frame_image_name(frame_name)
            bcd.set_frame_num(bcd_uid)
            bcd.set_frame_timestamp(timestamp)
            bcd.set_image(frame_name, img)
            self.pending_bcd = bcd

        if self.pending_bcd is not None:
            if self.bc_out.is_full_read(1):
                self.bc_out.write(self.pending_bcd)
                self.pending_bcd = None

        time.sleep(0.001)

    def _get_vcap_setting_name_list(self) -> List[str]:
        return ['CAP_PROP_BRIGHTNESS',
                'CAP_PROP_CONTRAST',
                'CAP_PROP_SATURATION',
                'CAP_PROP_HUE',
                'CAP_PROP_SHARPNESS',
                'CAP_PROP_GAMMA',
                'CAP_PROP_AUTO_WB',
                'CAP_PROP_XI_AUTO_WB',
                'CAP_PROP_XI_MANUAL_WB',
                'CAP_PROP_WB_TEMPERATURE',
                'CAP_PROP_BACKLIGHT',
                'CAP_PROP_GAIN',
                'CAP_PROP_AUTO_EXPOSURE',
                'CAP_PROP_EXPOSURE']

class WorkerState(BackendWorkerState):
    def __init__(self):
        self.source_type : _SourceType = None
        self.stream_url : str = None
        self.stream_protocol : _StreamProtocol = None
        self.device_idx : int = None
        self.driver : _DriverType = None
        self.resolution : _ResolutionType = None
        self.fps : float = None
        self.rotation : _RotationType = None
        self.flip_horizontal : bool = None
        self.settings_by_idx = {}
        self.webrtc_port : int = None

class Sheet:
    class Host(lib_csw.Sheet.Host):
        def __init__(self):
            super().__init__()
            self.source_type = lib_csw.DynamicSingleSwitch.Client()
            self.stream_url = lib_csw.Text.Client()
            self.stream_protocol = lib_csw.DynamicSingleSwitch.Client()
            self.stream_client_hint = lib_csw.Text.Client()
            self.stream_waiting = lib_csw.Flag.Client()
            self.webrtc_url = lib_csw.Text.Client()
            self.webrtc_waiting = lib_csw.Flag.Client()
            self.device_idx = lib_csw.DynamicSingleSwitch.Client()
            self.driver = lib_csw.DynamicSingleSwitch.Client()
            self.resolution = lib_csw.DynamicSingleSwitch.Client()
            self.fps = lib_csw.Number.Client()
            self.rotation = lib_csw.DynamicSingleSwitch.Client()
            self.flip_horizontal = lib_csw.Flag.Client()
            self.open_settings = lib_csw.Signal.Client()
            self.save_settings = lib_csw.Signal.Client()
            self.load_settings = lib_csw.Signal.Client()

    class Worker(lib_csw.Sheet.Worker):
        def __init__(self):
            super().__init__()
            self.source_type = lib_csw.DynamicSingleSwitch.Host()
            self.stream_url = lib_csw.Text.Host()
            self.stream_protocol = lib_csw.DynamicSingleSwitch.Host()
            self.stream_client_hint = lib_csw.Text.Host()
            self.stream_waiting = lib_csw.Flag.Host()
            self.webrtc_url = lib_csw.Text.Host()
            self.webrtc_waiting = lib_csw.Flag.Host()
            self.device_idx = lib_csw.DynamicSingleSwitch.Host()
            self.driver = lib_csw.DynamicSingleSwitch.Host()
            self.resolution = lib_csw.DynamicSingleSwitch.Host()
            self.fps = lib_csw.Number.Host()
            self.rotation = lib_csw.DynamicSingleSwitch.Host()
            self.flip_horizontal = lib_csw.Flag.Host()
            self.open_settings = lib_csw.Signal.Host()
            self.save_settings = lib_csw.Signal.Host()
            self.load_settings = lib_csw.Signal.Host()
