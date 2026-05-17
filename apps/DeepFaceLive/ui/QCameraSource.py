from localization import L
from xlib import qt as qtx

from ..backend import CameraSource
from .widgets.QBackendPanel import QBackendPanel
from .widgets.QCheckBoxCSWFlag import QCheckBoxCSWFlag
from .widgets.QComboBoxCSWDynamicSingleSwitch import \
    QComboBoxCSWDynamicSingleSwitch
from .widgets.QLabelPopupInfo import QLabelPopupInfo
from .widgets.QLineEditCSWText import QLineEditCSWText
from .widgets.QSpinBoxCSWNumber import QSpinBoxCSWNumber
from .widgets.QXPushButtonCSWSignal import QXPushButtonCSWSignal


class QCameraSource(QBackendPanel):
    def __init__(self, backend : CameraSource):
        cs = backend.get_control_sheet()

        q_driver_label    = QLabelPopupInfo(label=L('@QCameraSource.driver'), popup_info_text=L('@QCameraSource.help.driver') )
        q_driver          = QComboBoxCSWDynamicSingleSwitch(cs.driver, reflect_state_widgets=[q_driver_label])
        
        q_device_idx_label = QLabelPopupInfo(label=L('@QCameraSource.device_index') )
        q_device_idx       = QComboBoxCSWDynamicSingleSwitch(cs.device_idx, reflect_state_widgets=[q_device_idx_label])

        q_resolution_label = QLabelPopupInfo(label=L('@QCameraSource.resolution'), popup_info_text=L('@QCameraSource.help.resolution') )
        q_resolution       = QComboBoxCSWDynamicSingleSwitch(cs.resolution, reflect_state_widgets=[q_resolution_label])

        q_camera_settings_group_label = QLabelPopupInfo(label=L('@QCameraSource.camera_settings') )

        q_open_settings   = QXPushButtonCSWSignal(cs.open_settings, text=L('@QCameraSource.open_settings'))
        q_load_settings   = QXPushButtonCSWSignal(cs.load_settings, text=L('@QCameraSource.load_settings'), reflect_state_widgets=[q_camera_settings_group_label])
        q_save_settings   = QXPushButtonCSWSignal(cs.save_settings, text=L('@QCameraSource.save_settings'))

        camera_grid = qtx.QXGridLayout(spacing=5)
        row = 0
        camera_grid.addWidget(q_driver_label, row, 0, alignment=qtx.AlignRight | qtx.AlignVCenter  )
        camera_grid.addWidget(q_driver, row, 1, alignment=qtx.AlignLeft )
        row += 1
        camera_grid.addWidget(q_device_idx_label, row, 0, alignment=qtx.AlignRight | qtx.AlignVCenter  )
        camera_grid.addWidget(q_device_idx, row, 1, alignment=qtx.AlignLeft )
        row += 1
        camera_grid.addWidget(q_resolution_label, row, 0, alignment=qtx.AlignRight | qtx.AlignVCenter  )
        camera_grid.addWidget(q_resolution, row, 1, alignment=qtx.AlignLeft )
        row += 1
        btn_height = 24
        camera_grid.addWidget(q_camera_settings_group_label, row, 0, alignment=qtx.AlignRight | qtx.AlignVCenter  )
        camera_grid.addWidget( qtx.QXWidgetHBox([q_open_settings, q_load_settings, q_save_settings],
                                            contents_margins=(1,0,1,0), spacing=1, fixed_height=btn_height), row, 1, alignment=qtx.AlignLeft  )

        camera_page = qtx.QWidget()
        camera_page.setLayout(camera_grid)

        q_protocol_label = QLabelPopupInfo(label=L('@QCameraSource.stream_protocol'), popup_info_text=L('@QCameraSource.help.stream_protocol') )
        q_protocol       = QComboBoxCSWDynamicSingleSwitch(cs.stream_protocol, reflect_state_widgets=[q_protocol_label])

        q_listen_label = QLabelPopupInfo(label=L('@QCameraSource.listen_url'), popup_info_text=L('@QCameraSource.help.listen_url') )
        q_listen_url   = QLineEditCSWText(cs.stream_url, read_only=True, reflect_state_widgets=[q_listen_label])

        q_client_label = QLabelPopupInfo(label=L('@QCameraSource.client_url'), popup_info_text=L('@QCameraSource.help.client_url') )
        q_client_url   = QLineEditCSWText(cs.stream_client_hint, read_only=True, reflect_state_widgets=[q_client_label])

        q_wait_pb = qtx.QXProgressBar(min=0, max=0, fixed_width=120, fixed_height=14)
        q_wait_pb.setTextVisible(False)
        q_wait_msg = qtx.QXLabel(text=L('@QCameraSource.waiting_for_stream'))
        q_waiting_row = qtx.QXWidgetHBox([q_wait_pb, q_wait_msg], spacing=8)
        q_waiting_row.hide()

        def on_stream_waiting(flag : bool):
            q_waiting_row.setVisible(bool(flag))

        cs.stream_waiting.call_on_flag(on_stream_waiting)

        network_grid = qtx.QXGridLayout(spacing=5)
        nrow = 0
        network_grid.addWidget(q_protocol_label, nrow, 0, alignment=qtx.AlignRight | qtx.AlignVCenter)
        network_grid.addWidget(q_protocol, nrow, 1, alignment=qtx.AlignLeft)
        nrow += 1
        network_grid.addWidget(q_listen_label, nrow, 0, alignment=qtx.AlignRight | qtx.AlignVCenter)
        network_grid.addWidget(q_listen_url, nrow, 1, alignment=qtx.AlignLeft)
        nrow += 1
        network_grid.addWidget(q_client_label, nrow, 0, alignment=qtx.AlignRight | qtx.AlignVCenter)
        network_grid.addWidget(q_client_url, nrow, 1, alignment=qtx.AlignLeft)
        nrow += 1
        network_grid.addWidget(q_waiting_row, nrow, 0, 1, 2, alignment=qtx.AlignLeft)

        network_page = qtx.QWidget()
        network_page.setLayout(network_grid)

        # ── WebRTC tab ──
        q_webrtc_url_label = QLabelPopupInfo(label=L('@QCameraSource.webrtc_url'), popup_info_text=L('@QCameraSource.help.webrtc_url') )
        q_webrtc_url       = QLineEditCSWText(cs.webrtc_url, read_only=True, reflect_state_widgets=[q_webrtc_url_label])

        q_webrtc_wait_pb = qtx.QXProgressBar(min=0, max=0, fixed_width=120, fixed_height=14)
        q_webrtc_wait_pb.setTextVisible(False)
        q_webrtc_wait_msg = qtx.QXLabel(text=L('@QCameraSource.waiting_for_browser'))
        q_webrtc_waiting_row = qtx.QXWidgetHBox([q_webrtc_wait_pb, q_webrtc_wait_msg], spacing=8)
        q_webrtc_waiting_row.hide()

        def on_webrtc_waiting(flag : bool):
            q_webrtc_waiting_row.setVisible(bool(flag))

        cs.webrtc_waiting.call_on_flag(on_webrtc_waiting)

        webrtc_grid = qtx.QXGridLayout(spacing=5)
        wrow = 0
        webrtc_grid.addWidget(q_webrtc_url_label, wrow, 0, alignment=qtx.AlignRight | qtx.AlignVCenter)
        webrtc_grid.addWidget(q_webrtc_url, wrow, 1, alignment=qtx.AlignLeft)
        wrow += 1
        webrtc_grid.addWidget(q_webrtc_waiting_row, wrow, 0, 1, 2, alignment=qtx.AlignLeft)

        webrtc_page = qtx.QWidget()
        webrtc_page.setLayout(webrtc_grid)

        q_tabs = qtx.QTabWidget()
        q_tabs.addTab(camera_page, L('@QCameraSource.tab_camera'))
        q_tabs.addTab(network_page, L('@QCameraSource.tab_network'))
        q_tabs.addTab(webrtc_page, L('@QCameraSource.tab_webrtc'))

        _source_to_tab = {
            CameraSource.SOURCE_TYPE_CAMERA:  0,
            CameraSource.SOURCE_TYPE_NETWORK: 1,
            CameraSource.SOURCE_TYPE_WEBRTC:  2,
        }
        _tab_to_source = {v: k for k, v in _source_to_tab.items()}

        def sync_tab_from_source(_idx, choice):
            tab_i = _source_to_tab.get(choice, 0)
            with qtx.BlockSignals(q_tabs):
                q_tabs.setCurrentIndex(tab_i)

        cs.source_type.call_on_selected(sync_tab_from_source)

        def on_tab_changed(i : int):
            src = _tab_to_source.get(i)
            if src is not None:
                cs.source_type.select(src)

        q_tabs.currentChanged.connect(on_tab_changed)

        idx0 = cs.source_type.get_selected_idx()
        if idx0 is not None:
            with qtx.BlockSignals(q_tabs):
                q_tabs.setCurrentIndex(idx0)

        q_fps_label       = QLabelPopupInfo(label=L('@QCameraSource.fps'), popup_info_text=L('@QCameraSource.help.fps') )
        q_fps             = QSpinBoxCSWNumber(cs.fps, reflect_state_widgets=[q_fps_label])

        q_rotation_label  = QLabelPopupInfo(label=L('@QCameraSource.rotation') )
        q_rotation        = QComboBoxCSWDynamicSingleSwitch(cs.rotation, reflect_state_widgets=[q_rotation_label])

        q_flip_horizontal_label  = QLabelPopupInfo(label=L('@QCameraSource.flip_horizontal') )
        q_flip_horizontal = QCheckBoxCSWFlag(cs.flip_horizontal, reflect_state_widgets=[q_flip_horizontal_label])

        common_grid = qtx.QXGridLayout(spacing=5)
        row = 0
        common_grid.addWidget(q_fps_label, row, 0, alignment=qtx.AlignRight | qtx.AlignVCenter  )
        common_grid.addWidget(q_fps, row, 1, alignment=qtx.AlignLeft )
        row += 1
        common_grid.addWidget(q_rotation_label, row, 0, alignment=qtx.AlignRight | qtx.AlignVCenter  )
        common_grid.addWidget(q_rotation, row, 1, alignment=qtx.AlignLeft )
        row += 1
        common_grid.addWidget(q_flip_horizontal_label, row, 0, alignment=qtx.AlignRight | qtx.AlignVCenter  )
        common_grid.addWidget(q_flip_horizontal, row, 1, alignment=qtx.AlignLeft )

        main_l = qtx.QXVBoxLayout([q_tabs, common_grid], spacing=5)

        super().__init__(backend, L('@QCameraSource.module_title'),
                         layout=main_l,
                         content_align_top=True)

