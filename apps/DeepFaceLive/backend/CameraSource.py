import os
import platform
import subprocess
import threading
import time
import atexit
from datetime import datetime
from enum import IntEnum
from typing import List, Tuple, Union

import cv2
import numpy as np
from xlib import os as lib_os
from xlib.image import ImageProcessor
from xlib.mp import csw as lib_csw

from .BackendBase import (BackendConnection, BackendConnectionData, BackendDB,
                          BackendHost, BackendWeakHeap, BackendWorker,
                          BackendWorkerState)


class CameraSource(BackendHost):
    def __init__(self, weak_heap :  BackendWeakHeap, bc_out : BackendConnection, backend_db : BackendDB = None):
        super().__init__(backend_db=backend_db,
                         sheet_cls=Sheet,
                         worker_cls=CameraSourceWorker,
                         worker_state_cls=WorkerState,
                         worker_start_args=[weak_heap, bc_out] )

    def get_control_sheet(self) -> 'Sheet.Host': return super().get_control_sheet()

class _SourceType(IntEnum):
    CAMERA_DEVICE = 0
    NETWORK_STREAM = 1

_SourceType_names = { _SourceType.CAMERA_DEVICE : 'Camera device',
                      _SourceType.NETWORK_STREAM : 'Network stream',
                    }

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
        lib_os.set_timer_resolution(4)

        state, cs = self.get_state(), self.get_control_sheet()

        cs.source_type.call_on_selected(self.on_cs_source_type_selected)
        cs.stream_url.call_on_text(self.on_cs_stream_url)
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
           (state.source_type == _SourceType.NETWORK_STREAM and state.stream_url is not None and len(state.stream_url) != 0):

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
            else:
                print(f"\033[94mOpening stream {state.stream_url}\033[0m")
                vcap = cv2.VideoCapture(state.stream_url, cv2.CAP_FFMPEG)
                if vcap.isOpened():
                    print("\033[92mStream opened successfully\033[0m")
                    self.vcap = vcap
                else:
                    print("\033[93mFailed to open stream\033[0m")

            if vcap.isOpened():
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

    def on_cs_source_type_selected(self, idx, source_type):
        cs, state = self.get_control_sheet(), self.get_state()
        if state.source_type != source_type:
            state.source_type = source_type
            self.save_state()
            if self.is_started():
                self.stop_ffmpeg()
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

    def _ffmpeg_reader(self, url):
        self.ffmpeg_starting = True
        try:
            if url.startswith('udp://') and 'reuse=1' not in url:
                if '?' in url: url += '&reuse=1'
                else: url += '?reuse=1'
            
            is_file = os.path.exists(url)
            is_listener = 'listen=1' in url or 'mode=listener' in url or url.startswith('udp://')

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
                print(f"\033[93mListener mode detected, waiting for connection on {url}...\033[0m")

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
            
            def cleanup_ffmpeg(proc):
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except:
                        pass
            atexit.register(cleanup_ffmpeg, self.ffmpeg_proc)

            self.ffmpeg_starting = False
            frame_size = w * h * 3
            stream_connected = False
            while self.ffmpeg_proc is not None:
                raw_frame = self.ffmpeg_proc.stdout.read(frame_size)
                if len(raw_frame) != frame_size:
                    break
                if not stream_connected:
                    stream_connected = True
                    print(f"\033[92m[✓] Stream connected and receiving frames: {url}\033[0m")
                self.last_frame = np.frombuffer(raw_frame, dtype=np.uint8).reshape((h, w, 3))
        except Exception as e:
            print(f"\033[91mFFmpeg reader error: {e}\033[0m")
        finally:
            self.ffmpeg_starting = False
            if self.ffmpeg_proc:
                self.ffmpeg_proc.terminate()
                self.ffmpeg_proc = None
            print(f"\033[91m[X] FFmpeg reader stopped/disconnected: {url}\033[0m")

    def stop_ffmpeg(self):
        self.ffmpeg_starting = False
        if self.ffmpeg_proc:
            self.ffmpeg_proc.terminate()
            try:
                self.ffmpeg_proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.ffmpeg_proc.kill()
            self.ffmpeg_proc = None
        if self.ffmpeg_thread:
            # We don't join because it might block, let it die as daemon
            self.ffmpeg_thread = None
        self.last_frame = None

    def on_stop(self):
        self.stop_ffmpeg()
        super().on_stop()

    def on_tick(self):
        state, cs = self.get_state(), self.get_control_sheet()

        if state.source_type == _SourceType.CAMERA_DEVICE:
            if self.vcap is not None and not self.vcap.isOpened():
                self.set_vcap(None)

            if self.vcap is not None:
                self.start_profile_timing()
                ret, img = self.vcap.read()
                if ret:
                    self._process_frame(img)
        else:
            if self.ffmpeg_proc is None and not self.ffmpeg_starting:
                if state.stream_url:
                    now = time.time()
                    if now - self.last_ffmpeg_start_time > 5.0: # Retry delay
                        self.last_ffmpeg_start_time = now
                        self.ffmpeg_thread = threading.Thread(target=self._ffmpeg_reader, args=(state.stream_url,), daemon=True)
                        self.ffmpeg_thread.start()
            
            if self.last_frame is not None:
                self.start_profile_timing()
                img = self.last_frame
                self.last_frame = None
                self._process_frame(img)

    def _process_frame(self, img):
        state, cs = self.get_state(), self.get_control_sheet()
        self.stop_profile_timing()
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
        self.device_idx : int = None
        self.driver : _DriverType = None
        self.resolution : _ResolutionType = None
        self.fps : float = None
        self.rotation : _RotationType = None
        self.flip_horizontal : bool = None
        self.settings_by_idx = {}

class Sheet:
    class Host(lib_csw.Sheet.Host):
        def __init__(self):
            super().__init__()
            self.source_type = lib_csw.DynamicSingleSwitch.Client()
            self.stream_url = lib_csw.Text.Client()
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
            self.device_idx = lib_csw.DynamicSingleSwitch.Host()
            self.driver = lib_csw.DynamicSingleSwitch.Host()
            self.resolution = lib_csw.DynamicSingleSwitch.Host()
            self.fps = lib_csw.Number.Host()
            self.rotation = lib_csw.DynamicSingleSwitch.Host()
            self.flip_horizontal = lib_csw.Flag.Host()
            self.open_settings = lib_csw.Signal.Host()
            self.save_settings = lib_csw.Signal.Host()
            self.load_settings = lib_csw.Signal.Host()
