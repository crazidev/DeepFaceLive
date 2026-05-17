"""
WebRTC signaling + media server for DeepFaceLive.

Runs as a **separate process** spawned by CameraSourceWorker.
Receives a browser camera feed via aiortc, decodes each frame,
and writes it into a multiprocessing.shared_memory buffer that the main
DeepFaceLive process reads in its tick loop.

Protocol
--------
Shared-memory layout (total = 24 + W*H*3 bytes):
    Bytes  0‑3   : uint32  width
    Bytes  4‑7   : uint32  height
    Bytes  8‑11  : uint32  sequence counter (monotonic, wraps)
    Bytes 12‑15  : uint32  format (0 = BGR24, 1 = YUV420P)
    Bytes 16‑23  : double  timestamp (seconds)
    Bytes 24‑end : raw pixel data (row-major)

The writer (this process) bumps the sequence counter after every frame write.
The reader (CameraSource) polls the counter and copies the pixel data when it
changes.
"""

import asyncio
import json
import logging
import os
import signal
import struct
import sys
import traceback
import time
from multiprocessing import shared_memory
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import numpy as np

# Apply uvloop policy for high performance asyncio event loop if available
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    print("[WebRTC] Using uvloop for high-performance network events.")
except ImportError:
    pass

logger = logging.getLogger('dfl_webrtc')


def _get_ssl_context():
    import ssl
    cert_dir = Path(__file__).parent / 'certs'
    cert_dir.mkdir(exist_ok=True)
    cert_path = cert_dir / 'cert.pem'
    key_path = cert_dir / 'key.pem'
    
    if not cert_path.exists() or not key_path.exists():
        logger.info("Generating self-signed SSL certificate for remote HTTPS accesses...")
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        import datetime
        import socket

        # Generate private key
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        # Generate self-signed cert
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, u'DeepFaceLive WebRTC'),
        ])
        
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "127.0.0.1"

        import ipaddress
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.datetime.utcnow() - datetime.timedelta(days=1)
        ).not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=365)
        ).add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName(u'localhost'),
                x509.IPAddress(ipaddress.IPv4Address('127.0.0.1')),
                x509.IPAddress(ipaddress.IPv4Address(local_ip)),
            ]),
            critical=False,
        ).sign(key, hashes.SHA256())

        # Write key and cert
        with open(key_path, 'wb') as f:
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        with open(cert_path, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(str(cert_path), str(key_path))
    return ssl_context


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADER_SIZE = 24                       # 4 × uint32 + 1 × double = 24 bytes
MAX_WIDTH   = 1920
MAX_HEIGHT  = 1080
SHM_SIZE    = HEADER_SIZE + MAX_WIDTH * MAX_HEIGHT * 3   # ~6.2 MB (BGR24 size)


def _write_frame_to_shm(shm: shared_memory.SharedMemory, img: np.ndarray, seq: int, w: int, h: int, fmt_code: int, timestamp: float):
    """Write a numpy frame + metadata into the shared-memory segment."""
    data = img.tobytes()
    data_size = len(data)
    if HEADER_SIZE + data_size > shm.size:
        return seq  # frame too large – skip silently
    struct.pack_into('<IIIId', shm.buf, 0, w, h, seq & 0xFFFFFFFF, fmt_code, timestamp)
    shm.buf[HEADER_SIZE:HEADER_SIZE + data_size] = data
    return seq + 1


def read_frame_from_shm(shm: shared_memory.SharedMemory):
    """Read width, height, seq, format, timestamp, and the frame from shared memory.

    Returns (w, h, seq, format, timestamp, frame_numpy) or None if the buffer is empty.
    """
    try:
        w, h, seq, fmt_code, timestamp = struct.unpack_from('<IIIId', shm.buf, 0)
        if w == 0 or h == 0:
            return None
        
        # Format 1 = YUV420P, Format 0 = BGR24
        if fmt_code == 1:
            data_size = (w * h * 3) // 2
        else:
            data_size = w * h * 3
            
        if HEADER_SIZE + data_size > shm.size:
            return None
            
        raw = bytes(shm.buf[HEADER_SIZE:HEADER_SIZE + data_size])
        if fmt_code == 1:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape(((h * 3) // 2, w))
        else:
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
        return w, h, seq, fmt_code, timestamp, frame
    except Exception:
        return None


# ---------------------------------------------------------------------------
# GPU-Ready Codec / Decoder Abstraction Layer
# ---------------------------------------------------------------------------

class VideoDecoder:
    """
    Handles hardware/software decoding. Modularized for NVDEC (CUDA)
    readiness in RunPod GPU environments while maintaining CPU compatibility.
    """
    def __init__(self, use_gpu: bool = False):
        self.use_gpu = use_gpu
        if use_gpu:
            logger.info("Initializing GPU-accelerated video decoding context (Stub/NVDEC ready)...")
        else:
            logger.info("Using standard CPU decoding context.")

    def decode_frame(self, frame, yuv_mode: bool = True):
        """
        Decodes PyAV VideoFrame to numpy array.
        Returns: (decoded_numpy, format_code)
          format_code: 0 = BGR24, 1 = YUV420P
        """
        if self.use_gpu:
            # Future-Ready: Implement PyCUDA / Cupy NVDEC hardware decode copy here.
            # Currently falling back cleanly to CPU decoding
            pass
            
        if yuv_mode:
            # Native YUV420p decoding (extremely fast, avoids BGR24 overhead)
            return frame.to_ndarray(format='yuv420p'), 1
        else:
            # Standard BGR24 decoding
            return frame.to_ndarray(format='bgr24'), 0


# ---------------------------------------------------------------------------
# WebRTC track → shared-memory bridge
# ---------------------------------------------------------------------------

class VideoReceiver:
    """Consumes an aiortc video track, decodes frames asynchronously via workers, and writes to SHM."""

    def __init__(self, shm: shared_memory.SharedMemory):
        self._shm = shm
        self._seq = 1
        self._task = None
        self._running = False
        self._processing = False
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._decoder = VideoDecoder(use_gpu=False)

        # Performance & Health Diagnostics
        self.fps = 0.0
        self.dropped_frames = 0
        self.processed_frames = 0
        self.latency_ms = 0.0
        self.current_res = "0x0"

        # Frame pacing and FPS tracker
        self._last_fps_time = time.perf_counter()
        self._fps_counter = 0

    async def start(self, track):
        self._running = True
        self._task = asyncio.ensure_future(self._run(track))

    async def _run(self, track):
        import av
        loop = asyncio.get_running_loop()
        
        while self._running:
            try:
                frame = await track.recv()
                
                # Active Frame Dropping Strategy:
                # If background worker thread is currently busy, drop the frame
                # to prevent network lag buildup.
                if self._processing:
                    self.dropped_frames += 1
                    continue
                
                self._processing = True
                # Separated asynchronous processing (Receive -> Queue -> Worker Thread Decode)
                loop.run_in_executor(self._executor, self._process_and_write, frame)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if 'MediaStreamError' not in type(e).__name__:
                    logger.error('VideoReceiver connection closed or error: %s', e)
                break

    def _process_and_write(self, frame):
        """Worker thread executing standard CPU decoding and direct buffer memory writes."""
        import cv2
        t_start = time.perf_counter()
        try:
            h, w = frame.height, frame.width
            
            # Enforce even dimensions to prevent OpenCV CvtHelper assertions in YUV/BGR pipeline
            if w % 2 != 0 or h % 2 != 0:
                # If width or height is odd, decode to BGR24, crop 1px to make even, and write as BGR24 (fmt_code=0)
                img = frame.to_ndarray(format='bgr24')
                w = w - (w % 2)
                h = h - (h % 2)
                img = img[:h, :w]
                fmt_code = 0
            else:
                # Prefer fast YUV420p mode internally
                yuv_mode = os.environ.get('DFL_WEBRTC_YUV_MODE', '1') == '1'
                img, fmt_code = self._decoder.decode_frame(frame, yuv_mode=yuv_mode)

            self.current_res = f"{w}x{h}"

            # Prevent oversized frame processing to protect memory bounds
            if w > MAX_WIDTH or h > MAX_HEIGHT:
                scale = min(MAX_WIDTH / w, MAX_HEIGHT / h)
                new_w, new_h = int(w * scale), int(h * scale)
                # Enforce even dimensions for resized bounds
                new_w = new_w - (new_w % 2)
                new_h = new_h - (new_h % 2)
                
                if fmt_code == 1:
                    # YUV conversion and scaling
                    bgr = frame.to_ndarray(format='bgr24')
                    bgr_resized = cv2.resize(bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                    img = cv2.cvtColor(bgr_resized, cv2.COLOR_BGR2YUV_I420)
                else:
                    img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                w, h = new_w, new_h
                self.current_res = f"{w}x{h}"

            # Fast direct writes
            t_write = time.perf_counter()
            timestamp = time.time()
            self._seq = _write_frame_to_shm(self._shm, img, self._seq, w, h, fmt_code, timestamp)
            t_end = time.perf_counter()

            # Record latencies (ms)
            self.latency_ms = (t_end - t_start) * 1000.0
            
            self.processed_frames += 1
            self._fps_counter += 1
            
            # Update diagnostics FPS
            now = time.perf_counter()
            if now - self._last_fps_time >= 1.0:
                self.fps = self._fps_counter / (now - self._last_fps_time)
                self._fps_counter = 0
                self._last_fps_time = now

        except Exception as e:
            logger.error('Worker decoding exception: %s', e)
        finally:
            self._processing = False

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# aiohttp application
# ---------------------------------------------------------------------------

def _get_rtc_configuration():
    from aiortc import RTCIceServer, RTCConfiguration
    ice_servers = [
        RTCIceServer(urls='stun:stun.l.google.com:19302'),
        RTCIceServer(urls='stun:stun1.l.google.com:19302'),
        RTCIceServer(urls='stun:stun2.l.google.com:19302'),
    ]
    
    turn_server = os.environ.get('DFL_TURN_SERVER') or os.environ.get('DFL_TURN_PUBLIC_HOST')
    if turn_server:
        if not (turn_server.startswith('turn:') or turn_server.startswith('turns:')):
            turn_server = f"turns:{turn_server}:443?transport=tcp"
            
        turn_user = os.environ.get('DFL_TURN_USER', 'dfl')
        turn_pass = os.environ.get('DFL_TURN_PASSWORD', 'dflturn')
        
        logger.info(f"Using TURN relay server config: {turn_server} with user: {turn_user}")
        ice_servers.append(RTCIceServer(urls=turn_server, username=turn_user, credential=turn_pass))
        
    return RTCConfiguration(iceServers=ice_servers)


def _get_ice_servers_json():
    ice_servers = [
        {"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302", "stun:stun2.l.google.com:19302"]}
    ]
    
    turn_server = os.environ.get('DFL_TURN_SERVER') or os.environ.get('DFL_TURN_PUBLIC_HOST')
    if turn_server:
        if not (turn_server.startswith('turn:') or turn_server.startswith('turns:')):
            turn_server = f"turns:{turn_server}:443?transport=tcp"
            
        turn_user = os.environ.get('DFL_TURN_USER', 'dfl')
        turn_pass = os.environ.get('DFL_TURN_PASSWORD', 'dflturn')
        
        ice_servers.append({
            "urls": [turn_server],
            "username": turn_user,
            "credential": turn_pass
        })
    return ice_servers


async def _create_app(shm: shared_memory.SharedMemory, port: int):
    from aiohttp import web
    from aiortc import RTCPeerConnection, RTCSessionDescription

    pcs = set()          # active peer connections
    receivers = []       # active VideoReceivers

    static_dir = Path(__file__).parent / 'webrtc_client'

    async def index(request):
        return web.FileResponse(static_dir / 'index.html')

    async def offer(request):
        params = await request.json()
        offer_sdp = RTCSessionDescription(sdp=params['sdp'], type=params['type'])

        config = _get_rtc_configuration()
        pc = RTCPeerConnection(configuration=config)
        pcs.add(pc)

        receiver = VideoReceiver(shm)
        receivers.append(receiver)

        @pc.on('track')
        async def on_track(track):
            logger.info('Track received: kind=%s', track.kind)
            if track.kind == 'video':
                await receiver.start(track)

            @track.on('ended')
            async def on_ended():
                logger.info('Track ended: kind=%s', track.kind)
                receiver.stop()

        @pc.on('connectionstatechange')
        async def on_connectionstatechange():
            logger.info('Connection state: %s', pc.connectionState)
            if pc.connectionState in ('failed', 'closed'):
                await pc.close()
                pcs.discard(pc)
                if receiver in receivers:
                    receivers.remove(receiver)
                receiver.stop()

        await pc.setRemoteDescription(offer_sdp)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return web.json_response({
            'sdp': pc.localDescription.sdp,
            'type': pc.localDescription.type,
        })

    async def ice_config(request):
        return web.json_response({
            'iceServers': _get_ice_servers_json()
        })

    async def health(request):
        # Compile live metrics from active stream receivers
        fps = 0.0
        dropped = 0
        latency = 0.0
        res = "0x0"
        
        if receivers:
            fps = sum(r.fps for r in receivers)
            dropped = sum(r.dropped_frames for r in receivers)
            latency = sum(r.latency_ms for r in receivers) / len(receivers)
            res = receivers[0].current_res

        return web.json_response({
            'status': 'ok',
            'peers': len(pcs),
            'fps': round(fps, 2),
            'dropped_frames': dropped,
            'latency_ms': round(latency, 2),
            'resolution': res
        })

    async def on_shutdown(app):
        coros = [pc.close() for pc in pcs]
        await asyncio.gather(*coros)
        pcs.clear()
        for r in receivers:
            r.stop()
        receivers.clear()

    app = web.Application()
    app.on_shutdown.append(on_shutdown)

    app.router.add_get('/', index)
    app.router.add_post('/offer', offer)
    app.router.add_get('/ice-config', ice_config)
    app.router.add_get('/health', health)
    app.router.add_static('/static/', path=str(static_dir), name='static')

    return app


# ---------------------------------------------------------------------------
# Entry-point (runs inside a child process)
# ---------------------------------------------------------------------------

def run_webrtc_server(shm_name: str, port: int, stop_event):
    """
    Target for multiprocessing.Process.

    Parameters
    ----------
    shm_name : str
        Name of an already-created SharedMemory segment.
    port : int
        HTTP port for signaling + static files.
    stop_event : multiprocessing.Event
        Set by the parent to request graceful shutdown.
    """
    logging.basicConfig(level=logging.INFO, format='[WebRTC] %(message)s')
    logger.info('Starting WebRTC server on port %d  (shm=%s)', port, shm_name)

    shm = shared_memory.SharedMemory(name=shm_name, create=False)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from aiohttp import web

    async def _main():
        app = await _create_app(shm, port)
        runner = web.AppRunner(app)
        await runner.setup()
        ssl_context = _get_ssl_context()
        site = web.TCPSite(runner, '0.0.0.0', port, ssl_context=ssl_context)
        await site.start()
        logger.info('WebRTC server listening on secure HTTPS: https://0.0.0.0:%d', port)

        # Poll stop_event every 0.5 s
        while not stop_event.is_set():
            await asyncio.sleep(0.5)

        logger.info('Stop event received — shutting down…')
        await runner.cleanup()

    try:
        loop.run_until_complete(_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
    finally:
        shm.close()
        loop.close()
        logger.info('WebRTC server exited.')
