import os
from enum import IntEnum
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np
from xlib import cv as lib_cv
from xlib import logic as lib_logic
from xlib import os as lib_os
from xlib import time as lib_time
from xlib.image import ImageProcessor
from xlib.mp import csw as lib_csw
from xlib.streamer import FFMPEGStreamer

from .BackendBase import (BackendConnection, BackendDB, BackendHost,
                          BackendSignal, BackendWeakHeap, BackendWorker,
                          BackendWorkerState)


class InputRoute(IntEnum):
    """Where StreamOutput reads BackendConnectionData from."""
    PIPELINE = 0
    SOURCE_DIRECT = 1


_InputRoute_names = {
    InputRoute.PIPELINE: '@StreamOutput.InputRoute.PIPELINE',
    InputRoute.SOURCE_DIRECT: '@StreamOutput.InputRoute.SOURCE_DIRECT',
}


def _default_output_stream_udp_port() -> int:
    v = os.environ.get('DFL_OUTPUT_STREAM_UDP_PORT')
    if v is None or len(str(v).strip()) == 0:
        return 1234
    try:
        p = int(str(v).strip(), 10)
        return p if 1 <= p <= 65535 else 1234
    except ValueError:
        return 1234


class StreamOutput(BackendHost):
    """
    Bufferizes and shows the stream in separated window.
    """
    def __init__(self, weak_heap : BackendWeakHeap,
                       reemit_frame_signal : BackendSignal,
                       bc_in_pipeline : BackendConnection,
                       bc_in_direct : Optional[BackendConnection] = None,
                       face_pipeline_hosts : Optional[Sequence[BackendHost]] = None,
                       save_default_path : Path = None,
                       backend_db : BackendDB = None):

        self._face_pipeline_hosts = tuple(face_pipeline_hosts) if face_pipeline_hosts else tuple()
        self._face_pipeline_snapshot : Optional[List[Tuple[BackendHost, bool]]] = None

        super().__init__(backend_db=backend_db,
                         sheet_cls=Sheet,
                         worker_cls=StreamOutputWorker,
                         worker_state_cls=WorkerState,
                         worker_start_args=[weak_heap, reemit_frame_signal, bc_in_pipeline, bc_in_direct, save_default_path] )

        self.call_on_msg('_dfl_suspend_face_pipeline', self._host_suspend_face_pipeline)
        self.call_on_msg('_dfl_resume_face_pipeline', self._host_resume_face_pipeline)

    def _host_suspend_face_pipeline(self):
        if self._face_pipeline_snapshot is not None or not self._face_pipeline_hosts:
            self.send_msg('_direct_route_ack')
            return
        snap = []
        for h in self._face_pipeline_hosts:
            was = h.is_started()
            snap.append((h, was))
            if was:
                h.stop()
        self._face_pipeline_snapshot = snap
        for _ in range(5000):
            busy = False
            for h, _was in snap:
                h.process_messages()
                if h.is_starting() or h.is_stopping():
                    busy = True
            if not busy:
                break
        self.send_msg('_direct_route_ack')

    def _host_resume_face_pipeline(self):
        if self._face_pipeline_snapshot is None:
            self.send_msg('_pipeline_route_ack')
            return
        snap = self._face_pipeline_snapshot
        self._face_pipeline_snapshot = None
        for h, was_started in snap:
            if was_started:
                h.start()
        for _ in range(5000):
            busy = False
            for h, _ in snap:
                h.process_messages()
                if h.is_starting():
                    busy = True
            if not busy:
                break
        self.send_msg('_pipeline_route_ack')

    def get_control_sheet(self) -> 'Sheet.Host': return super().get_control_sheet()

class SourceType(IntEnum):
    SOURCE_FRAME = 0
    ALIGNED_FACE = 1
    SWAPPED_FACE = 2
    MERGED_FRAME = 3
    MERGED_FRAME_OR_SOURCE_FRAME = 4
    SOURCE_N_MERGED_FRAME = 5
    SOURCE_N_MERGED_FRAME_OR_SOURCE_FRAME = 6
    ALIGNED_N_SWAPPED_FACE = 7

ViewModeNames = ['@StreamOutput.SourceType.SOURCE_FRAME',
                 '@StreamOutput.SourceType.ALIGNED_FACE',
                 '@StreamOutput.SourceType.SWAPPED_FACE',
                 '@StreamOutput.SourceType.MERGED_FRAME',
                 '@StreamOutput.SourceType.MERGED_FRAME_OR_SOURCE_FRAME',
                 '@StreamOutput.SourceType.SOURCE_N_MERGED_FRAME',
                 '@StreamOutput.SourceType.SOURCE_N_MERGED_FRAME_OR_SOURCE_FRAME',
                 '@StreamOutput.SourceType.ALIGNED_N_SWAPPED_FACE',
                 ]



class StreamOutputWorker(BackendWorker):
    def get_state(self) -> 'WorkerState': return super().get_state()
    def get_control_sheet(self) -> 'Sheet.Worker': return super().get_control_sheet()

    def _on_direct_route_ack(self):
        self._pending_direct_ack = False

    def _on_pipeline_route_ack(self):
        self._pending_pipeline_ack = False

    def _wait_direct_ack(self):
        self._pending_direct_ack = True
        self.send_msg('_dfl_suspend_face_pipeline')
        while self._pending_direct_ack:
            self._pmpi.process_messages(0.02)

    def _wait_pipeline_ack(self):
        self._pending_pipeline_ack = True
        self.send_msg('_dfl_resume_face_pipeline')
        while self._pending_pipeline_ack:
            self._pmpi.process_messages(0.02)

    def _apply_input_route_controls(self, sync_input_route_widget=False):
        """
        sync_input_route_widget: True only from on_start. set_choices() on input_route calls
        unselect() internally, which re-enters on_cs_input_route with route=None and would
        recurse if we set_choices again from that handler.
        """
        state, cs = self.get_state(), self.get_control_sheet()
        if self.bc_in_direct is None:
            cs.input_route.disable()
            cs.source_type.enable()
            cs.source_type.set_choices(SourceType, ViewModeNames, none_choice_name='@misc.menu_select')
            st = state.source_type if state.source_type is not None else SourceType.SOURCE_FRAME
            cs.source_type.select(st)
            self.on_cs_source_type(0, st)
            return
        cs.input_route.enable()
        if sync_input_route_widget:
            cs.input_route.set_choices(InputRoute, _InputRoute_names, none_choice_name=None)
            cs.input_route.select(state.input_route if state.input_route is not None else InputRoute.PIPELINE)
        if state.input_route == InputRoute.SOURCE_DIRECT:
            cs.source_type.disable()
            state.source_type = SourceType.SOURCE_FRAME
            cs.source_type.set_choices(SourceType, ViewModeNames, none_choice_name=None)
            cs.source_type.select(SourceType.SOURCE_FRAME)
            cs.aligned_face_id.disable()
        else:
            cs.source_type.enable()
            cs.source_type.set_choices(SourceType, ViewModeNames, none_choice_name='@misc.menu_select')
            st = state.source_type if state.source_type is not None else SourceType.SOURCE_FRAME
            cs.source_type.select(st)
            self.on_cs_source_type(0, st)

    def on_start(self, weak_heap : BackendWeakHeap, reemit_frame_signal : BackendSignal,
                       bc_in_pipeline : BackendConnection,
                       bc_in_direct : Optional[BackendConnection],
                       save_default_path : Path):
        self.weak_heap = weak_heap
        self.reemit_frame_signal = reemit_frame_signal
        self.bc_in_pipeline = bc_in_pipeline
        self.bc_in_direct = bc_in_direct

        self.call_on_msg('_direct_route_ack', self._on_direct_route_ack)
        self.call_on_msg('_pipeline_route_ack', self._on_pipeline_route_ack)
        self._pending_direct_ack = False
        self._pending_pipeline_ack = False

        self.fps_counter = lib_time.FPSCounter()

        self.buffered_frames = lib_logic.DelayedBuffers()
        self.is_show_window = False

        self.prev_frame_num = -1

        self._wnd_name = 'DeepFaceLive output'
        self._wnd_showing = False

        self._streamer = FFMPEGStreamer()

        lib_os.set_timer_resolution(1)

        state, cs = self.get_state(), self.get_control_sheet()

        cs.input_route.call_on_selected(self.on_cs_input_route)
        cs.source_type.call_on_selected(self.on_cs_source_type)
        cs.show_hide_window.call_on_signal(self.on_cs_show_hide_window_signal)
        cs.aligned_face_id.call_on_number(self.on_cs_aligned_face_id)
        cs.target_delay.call_on_number(self.on_cs_target_delay)
        cs.save_sequence_path.call_on_paths(self.on_cs_save_sequence_path)
        cs.save_fill_frame_gap.call_on_flag(self.on_cs_save_fill_frame_gap)
        cs.is_streaming.call_on_flag(self.on_cs_is_streaming)
        cs.stream_addr.call_on_text(self.on_cs_stream_addr)
        cs.stream_port.call_on_number(self.on_cs_stream_port)

        if state.input_route is None:
            state.input_route = InputRoute.PIPELINE
        if self.bc_in_direct is None:
            state.input_route = InputRoute.PIPELINE

        if state.source_type is None:
            state.source_type = SourceType.SOURCE_FRAME

        self._apply_input_route_controls(sync_input_route_widget=True)

        cs.target_delay.enable()
        cs.target_delay.set_config(lib_csw.Number.Config(min=0, max=5000, step=100, decimals=0, allow_instant_update=True))
        cs.target_delay.set_number(state.target_delay if state.target_delay is not None else 500)

        cs.avg_fps.enable()
        cs.avg_fps.set_config(lib_csw.Number.Config(min=0, max=240, decimals=1, read_only=True))
        cs.avg_fps.set_number(0)

        cs.show_hide_window.enable()
        self.hide_window()

        if state.is_showing_window is None:
            state.is_showing_window = False

        if state.is_showing_window:
            state.is_showing_window = not state.is_showing_window
            cs.show_hide_window.signal()

        cs.save_sequence_path.enable()
        cs.save_sequence_path.set_config( lib_csw.Paths.Config.Directory('Choose output sequence directory', directory_path=save_default_path) )
        cs.save_sequence_path.set_paths(state.sequence_path)

        cs.save_fill_frame_gap.enable()
        cs.save_fill_frame_gap.set_flag(state.save_fill_frame_gap if state.save_fill_frame_gap is not None else True )

        cs.is_streaming.enable()
        cs.is_streaming.set_flag(state.is_streaming if state.is_streaming is not None else False )

        cs.stream_addr.enable()
        cs.stream_addr.set_text(state.stream_addr if state.stream_addr is not None else '127.0.0.1')

        cs.stream_port.enable()
        cs.stream_port.set_config(lib_csw.Number.Config(min=1, max=65535, decimals=0, allow_instant_update=True))
        cs.stream_port.set_number(
            state.stream_port if state.stream_port is not None else _default_output_stream_udp_port()
        )

        if self.bc_in_direct is not None and state.input_route == InputRoute.SOURCE_DIRECT:
            self._wait_direct_ack()

    def on_stop(self):
        self._streamer.stop()

    def on_cs_input_route(self, idx, route : InputRoute):
        state, cs = self.get_state(), self.get_control_sheet()
        if self.bc_in_direct is None:
            return
        # set_choices() issues unselect() first; ignore that re-entrant callback.
        if route is None:
            return
        if state.input_route == route:
            return
        prev = state.input_route
        if route == InputRoute.SOURCE_DIRECT:
            self._wait_direct_ack()
            state.input_route = route
            self.save_state()
            self._apply_input_route_controls(sync_input_route_widget=False)
        else:
            if prev == InputRoute.SOURCE_DIRECT:
                self._wait_pipeline_ack()
            state.input_route = route
            self.save_state()
            self._apply_input_route_controls(sync_input_route_widget=False)

    def on_cs_source_type(self, idx, source_type):
        state, cs = self.get_state(), self.get_control_sheet()
        if state.input_route == InputRoute.SOURCE_DIRECT:
            return
        if source_type in [SourceType.ALIGNED_FACE, SourceType.ALIGNED_N_SWAPPED_FACE]:
            cs.aligned_face_id.enable()
            cs.aligned_face_id.set_config(lib_csw.Number.Config(min=0, max=16, step=1, allow_instant_update=True))
            cs.aligned_face_id.set_number(state.aligned_face_id or 0)
        else:
            cs.aligned_face_id.disable()
        state.source_type = source_type

        self.save_state()
        self.reemit_frame_signal.send()

    def show_window(self):
        state, cs = self.get_state(), self.get_control_sheet()
        cv2.namedWindow(self._wnd_name)
        self._wnd_showing = True

    def hide_window(self):
        state, cs = self.get_state(), self.get_control_sheet()
        if self._wnd_showing:
            cv2.destroyAllWindows()
            self._wnd_showing = False

    def on_cs_show_hide_window_signal(self,):
        state, cs = self.get_state(), self.get_control_sheet()

        state.is_showing_window = not state.is_showing_window
        if state.is_showing_window:
            cv2.namedWindow(self._wnd_name)
        else:
            cv2.destroyAllWindows()
        self.save_state()
        self.reemit_frame_signal.send()


    def on_cs_aligned_face_id(self, aligned_face_id):
        state, cs = self.get_state(), self.get_control_sheet()
        cfg = cs.aligned_face_id.get_config()
        aligned_face_id = state.aligned_face_id = np.clip(aligned_face_id, cfg.min, cfg.max)
        cs.aligned_face_id.set_number(aligned_face_id)
        self.save_state()
        self.reemit_frame_signal.send()

    def on_cs_target_delay(self, target_delay):
        state, cs = self.get_state(), self.get_control_sheet()
        cfg = cs.target_delay.get_config()
        target_delay = state.target_delay = int(np.clip(target_delay, cfg.min, cfg.max))
        self.buffered_frames.set_target_delay(target_delay / 1000.0)
        cs.target_delay.set_number(target_delay)
        self.save_state()
        self.reemit_frame_signal.send()

    def on_cs_save_sequence_path(self, paths : List[Path], prev_paths):
        state, cs = self.get_state(), self.get_control_sheet()
        cs.save_sequence_path_error.set_error(None)
        sequence_path = paths[0] if len(paths) != 0 else None

        if sequence_path is None or sequence_path.exists():
            state.sequence_path = sequence_path
            cs.save_sequence_path.set_paths(sequence_path, block_event=True)
        else:
            cs.save_sequence_path_error.set_error(f'{sequence_path} does not exist.')
            cs.save_sequence_path.set_paths(prev_paths, block_event=True)
        self.save_state()
        self.reemit_frame_signal.send()

    def on_cs_save_fill_frame_gap(self, save_fill_frame_gap):
        state, cs = self.get_state(), self.get_control_sheet()
        state.save_fill_frame_gap = save_fill_frame_gap
        self.save_state()

    def on_cs_is_streaming(self, is_streaming):
        state, cs = self.get_state(), self.get_control_sheet()
        state.is_streaming = is_streaming
        self.save_state()

    def on_cs_stream_addr(self, stream_addr):
        state, cs = self.get_state(), self.get_control_sheet()
        state.stream_addr = stream_addr
        self.save_state()
        self._streamer.set_addr_port(state.stream_addr, state.stream_port)

    def on_cs_stream_port(self, stream_port):
        state, cs = self.get_state(), self.get_control_sheet()
        state.stream_port = stream_port
        self.save_state()
        self._streamer.set_addr_port(state.stream_addr, state.stream_port)

    def on_tick(self):
        cs, state = self.get_control_sheet(), self.get_state()

        active_bc = self.bc_in_pipeline
        if self.bc_in_direct is not None and state.input_route == InputRoute.SOURCE_DIRECT:
            active_bc = self.bc_in_direct

        bcd = active_bc.read(timeout=0.005)
        if bcd is not None:
            bcd.assign_weak_heap(self.weak_heap)
            cs.avg_fps.set_number( self.fps_counter.step() )

            prev_frame_num = self.prev_frame_num
            frame_num = self.prev_frame_num = bcd.get_frame_num()
            if frame_num < prev_frame_num:
                prev_frame_num = self.prev_frame_num = -1

            source_type = SourceType.SOURCE_FRAME if state.input_route == InputRoute.SOURCE_DIRECT else state.source_type
            if source_type is not None and \
                (state.is_showing_window or \
                 state.sequence_path is not None or \
                 state.is_streaming):
                buffered_frames = self.buffered_frames

                view_image = None

                if source_type == SourceType.SOURCE_FRAME:
                    view_image = bcd.get_image(bcd.get_frame_image_name())
                elif source_type in [SourceType.MERGED_FRAME, SourceType.MERGED_FRAME_OR_SOURCE_FRAME]:
                    view_image = bcd.get_image(bcd.get_merged_image_name())
                    if view_image is None and source_type == SourceType.MERGED_FRAME_OR_SOURCE_FRAME:
                        view_image = bcd.get_image(bcd.get_frame_image_name())

                elif source_type == SourceType.ALIGNED_FACE:
                    aligned_face_id = state.aligned_face_id
                    for i, fsi in enumerate(bcd.get_face_swap_info_list()):
                        if aligned_face_id == i:
                            view_image = bcd.get_image(fsi.face_align_image_name)
                            break

                elif source_type == SourceType.SWAPPED_FACE:
                    for fsi in bcd.get_face_swap_info_list():
                        view_image = bcd.get_image(fsi.face_swap_image_name)
                        if view_image is not None:
                            break

                elif source_type in [SourceType.SOURCE_N_MERGED_FRAME, SourceType.SOURCE_N_MERGED_FRAME_OR_SOURCE_FRAME]:
                    source_frame = bcd.get_image(bcd.get_frame_image_name())
                    if source_frame is not None:
                        source_frame = ImageProcessor(source_frame).to_ufloat32().get_image('HWC')

                    merged_frame = bcd.get_image(bcd.get_merged_image_name())

                    if merged_frame is None and source_type == SourceType.SOURCE_N_MERGED_FRAME_OR_SOURCE_FRAME:
                        merged_frame = source_frame

                    if source_frame is not None and merged_frame is not None:
                        view_image = np.concatenate( (source_frame, merged_frame), 1 )

                elif source_type == SourceType.ALIGNED_N_SWAPPED_FACE:
                    aligned_face_id = state.aligned_face_id
                    aligned_face = None
                    swapped_face = None
                    for i, fsi in enumerate(bcd.get_face_swap_info_list()):
                        if aligned_face_id == i:
                            aligned_face = bcd.get_image(fsi.face_align_image_name)
                            break

                    for fsi in bcd.get_face_swap_info_list():
                        swapped_face = bcd.get_image(fsi.face_swap_image_name)
                        if swapped_face is not None:
                            break

                    if aligned_face is not None and swapped_face is not None:
                        view_image = np.concatenate( (aligned_face, swapped_face), 1 )


                if view_image is not None:
                    buffered_frames.add_buffer( bcd.get_frame_timestamp(), view_image )

                    if state.sequence_path is not None:
                        img = ImageProcessor(view_image, copy=True).to_uint8().get_image('HWC')

                        file_ext, cv_args = '.jpg', [int(cv2.IMWRITE_JPEG_QUALITY), 100]

                        frame_diff = abs(frame_num - prev_frame_num) if state.save_fill_frame_gap else 1
                        for i in range(frame_diff):
                            n = frame_num - i
                            filename = f'{n:06}'
                            lib_cv.imwrite(state.sequence_path / (filename+file_ext), img, cv_args)

                    if state.is_streaming:
                        stream_img = ImageProcessor(view_image).to_uint8().get_image('HWC')
                        self._streamer.push_frame(stream_img)

                pr = buffered_frames.process()

                img = pr.new_data
                if img is not None and state.is_showing_window:
                    cv2.imshow(self._wnd_name, img)

        if state.is_showing_window:
            cv2.waitKey(1)

class Sheet:
    class Host(lib_csw.Sheet.Host):
        def __init__(self):
            super().__init__()
            self.input_route = lib_csw.DynamicSingleSwitch.Client()
            self.source_type = lib_csw.DynamicSingleSwitch.Client()
            self.aligned_face_id = lib_csw.Number.Client()
            self.target_delay = lib_csw.Number.Client()
            self.avg_fps = lib_csw.Number.Client()
            self.show_hide_window = lib_csw.Signal.Client()
            self.save_sequence_path = lib_csw.Paths.Client()
            self.save_sequence_path_error = lib_csw.Error.Client()
            self.save_fill_frame_gap = lib_csw.Flag.Client()
            self.is_streaming = lib_csw.Flag.Client()
            self.stream_addr = lib_csw.Text.Client()
            self.stream_port = lib_csw.Number.Client()

    class Worker(lib_csw.Sheet.Worker):
        def __init__(self):
            super().__init__()
            self.input_route = lib_csw.DynamicSingleSwitch.Host()
            self.source_type = lib_csw.DynamicSingleSwitch.Host()
            self.aligned_face_id = lib_csw.Number.Host()
            self.target_delay = lib_csw.Number.Host()
            self.avg_fps = lib_csw.Number.Host()
            self.show_hide_window = lib_csw.Signal.Host()
            self.save_sequence_path = lib_csw.Paths.Host()
            self.save_sequence_path_error = lib_csw.Error.Host()
            self.save_fill_frame_gap = lib_csw.Flag.Host()
            self.is_streaming = lib_csw.Flag.Host()
            self.stream_addr = lib_csw.Text.Host()
            self.stream_port = lib_csw.Number.Host()

class WorkerState(BackendWorkerState):
    input_route : InputRoute = None
    source_type : SourceType = None
    is_showing_window : bool = None
    aligned_face_id : int = None
    target_delay : int = None
    sequence_path : Path = None
    save_fill_frame_gap : bool = None
    is_streaming : bool = None
    stream_addr : str = None
    stream_port : int = None
