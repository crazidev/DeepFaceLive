import sys
import os
from pathlib import Path

# Add current directory to sys.path to allow imports from apps, xlib, etc.
current_dir = Path(__file__).parent.absolute()
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

def check_deps():
    missing = []
    try:
        import PyQt6
    except ImportError:
        missing.append("PyQt6")
    try:
        import cv2
    except ImportError:
        missing.append("opencv-python")
    try:
        import numpy
    except ImportError:
        missing.append("numpy")
    
    if missing:
        print("Warning: Some dependencies for GUI test are missing:")
        for m in missing:
            print(f"  - {m}")
        print("\nAttempting to continue with mocks for heavy modules...")
    return True

# ─── Mocking heavy modules to allow standalone test ──────────────────────────
import unittest.mock as mock

# Mock onnxruntime and onnx which are often hard to install or not needed for this test
for module_name in ["onnxruntime", "onnx", "modelhub", "modelhub.onnx", "modelhub.onnx.LIA", "xlib.onnxruntime"]:
    try:
        if module_name in sys.modules:
            continue
        __import__(module_name)
    except ImportError:
        print(f"[MOCK] Mocking {module_name}")
        sys.modules[module_name] = mock.MagicMock()

if not check_deps():
    sys.exit(1)

from xlib import qt as qtx
from apps.DeepFaceLive import backend
from apps.DeepFaceLive.ui.QCameraSource import QCameraSource
from apps.DeepFaceLive.ui.widgets.QBCFrameViewer import QBCFrameViewer
from localization import Localization

class StandaloneCameraApp(qtx.QXMainApplication):
    def __init__(self, userdata_path):
        self.userdata_path = userdata_path
        settings_dirpath = userdata_path / 'settings'
        settings_dirpath.mkdir(parents=True, exist_ok=True)
        
        # Initialize QXMainApplication
        super().__init__(app_name='DeepFaceLiveCameraTest', settings_dirpath=settings_dirpath)

        # Set language
        Localization.set_language('en-US')

        # Backend setup - Minimal setup for CameraSource only
        backend_db = self.backend_db = backend.BackendDB(settings_dirpath / 'camera_test_states.dat')
        backend_weak_heap = self.backend_weak_heap = backend.BackendWeakHeap(size_mb=512)
        
        # This connection will hold the frames produced by CameraSource
        bc_out = self.bc_out = backend.BackendConnection()
        
        # Signal used to notify UI to refresh (though QBCFrameViewer uses a timer)
        reemit_frame_signal = self.reemit_frame_signal = backend.BackendSignal()

        # Initialize CameraSource module
        self.camera_source = backend.CameraSource(weak_heap=backend_weak_heap, bc_out=bc_out, backend_db=backend_db)
        
        # UI setup
        self.q_camera_source = QCameraSource(self.camera_source)
        
        # Viewer setup - QBCFrameViewer is what's used in the main app to see "Source Frame"
        self.q_frame_viewer = QBCFrameViewer(backend_weak_heap, bc_out, preview_width=512)
        self.q_frame_viewer.open()

        # Main Window
        self.main_wnd = qtx.QXWindow(save_load_state=False)
        self.main_wnd.setWindowTitle("DeepFaceLive - Camera/Stream Source Test")
        
        # Layout: Camera controls on top, Frame viewer below
        main_layout = qtx.QXVBoxLayout([
            self.q_camera_source,
            self.q_frame_viewer
        ], spacing=10)
        
        self.main_wnd.setLayout(main_layout)
        self.main_wnd.resize(600, 800)
        self.main_wnd.show()
        
        # Restore state (loads previous settings if any)
        self.camera_source.restore_on_off_state(default_state=False)
        
        # Timer to process backend messages (crucial for host-worker communication)
        self._timer = qtx.QXTimer(interval=5, timeout=self._on_timer, start=True)
        print("Test UI Started. Use the Camera / Network stream tabs; on Network, pick a protocol or set DFL_STREAM_* env vars.")

    def _on_timer(self):
        self.backend_db.process_messages()
        self.camera_source.process_messages()
        
        # Drain the connection to update the read index, 
        # otherwise CameraSourceWorker will stop producing frames (is_full_read check)
        while self.bc_out.read() is not None:
            pass

    def finalize(self):
        print("Stopping CameraSource...")
        self.camera_source.save_on_off_state()
        self.camera_source.stop()
        while not self.camera_source.is_stopped():
            self.backend_db.process_messages()
            self.camera_source.process_messages()
        self.backend_db.finish_pending_jobs()

if __name__ == '__main__':
    # Use a local directory for test data
    userdata_path = Path(current_dir / 'camera_test_userdata')
    userdata_path.mkdir(parents=True, exist_ok=True)
    
    app = StandaloneCameraApp(userdata_path)
    
    try:
        app.exec()
    finally:
        app.finalize()
