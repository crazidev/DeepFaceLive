"""
WebRTC signaling + media server for DeepFaceLive.

Runs as a **separate process** spawned by CameraSourceWorker.
Receives a browser camera feed via aiortc, decodes each frame to BGR24 numpy,
and writes it into a multiprocessing.shared_memory buffer that the main
DeepFaceLive process reads in its tick loop.

Protocol
--------
Shared-memory layout (total = 12 + W*H*3 bytes):
    Bytes  0‑3   : uint32  width
    Bytes  4‑7   : uint32  height
    Bytes  8‑11  : uint32  sequence counter (monotonic, wraps)
    Bytes 12‑end : raw BGR24 pixel data  (row-major)

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
from multiprocessing import shared_memory
from pathlib import Path

import numpy as np

logger = logging.getLogger('dfl_webrtc')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HEADER_SIZE = 12                       # 3 × uint32
MAX_WIDTH   = 1920
MAX_HEIGHT  = 1080
SHM_SIZE    = HEADER_SIZE + MAX_WIDTH * MAX_HEIGHT * 3   # ~6.2 MB


def _write_frame_to_shm(shm: shared_memory.SharedMemory, frame_bgr: np.ndarray, seq: int):
    """Write a BGR numpy frame + metadata into the shared-memory segment."""
    h, w = frame_bgr.shape[:2]
    data_size = w * h * 3
    if HEADER_SIZE + data_size > shm.size:
        return seq  # frame too large – skip silently
    struct.pack_into('<III', shm.buf, 0, w, h, seq & 0xFFFFFFFF)
    shm.buf[HEADER_SIZE:HEADER_SIZE + data_size] = frame_bgr.tobytes()
    return seq + 1


def read_frame_from_shm(shm: shared_memory.SharedMemory):
    """Read width, height, seq, and the BGR frame from shared memory.

    Returns (w, h, seq, frame_bgr_numpy) or None if the buffer is empty.
    """
    w, h, seq = struct.unpack_from('<III', shm.buf, 0)
    if w == 0 or h == 0:
        return None
    data_size = w * h * 3
    if HEADER_SIZE + data_size > shm.size:
        return None
    raw = bytes(shm.buf[HEADER_SIZE:HEADER_SIZE + data_size])
    frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
    return w, h, seq, frame


# ---------------------------------------------------------------------------
# WebRTC track → shared-memory bridge
# ---------------------------------------------------------------------------

class VideoReceiver:
    """Consumes an aiortc video track and writes decoded frames to SHM."""

    def __init__(self, shm: shared_memory.SharedMemory):
        self._shm = shm
        self._seq = 1
        self._task = None

    async def start(self, track):
        self._task = asyncio.ensure_future(self._run(track))

    async def _run(self, track):
        import av  # imported here so the top-level import stays lightweight
        try:
            while True:
                frame = await track.recv()
                # frame is an av.VideoFrame; convert to BGR numpy
                img = frame.to_ndarray(format='bgr24')
                # Resize if larger than max (keep aspect ratio)
                h, w = img.shape[:2]
                if w > MAX_WIDTH or h > MAX_HEIGHT:
                    scale = min(MAX_WIDTH / w, MAX_HEIGHT / h)
                    new_w, new_h = int(w * scale), int(h * scale)
                    import cv2
                    img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                self._seq = _write_frame_to_shm(self._shm, img, self._seq)
        except Exception as e:
            # MediaStreamError is normal on disconnect
            if 'MediaStreamError' not in type(e).__name__:
                logger.error('VideoReceiver error: %s', e)

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()


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
        return web.json_response({'status': 'ok', 'peers': len(pcs)})

    async def on_shutdown(app):
        coros = [pc.close() for pc in pcs]
        await asyncio.gather(*coros)
        pcs.clear()
        for r in receivers:
            r.stop()

    app = web.Application()
    app.on_shutdown.append(on_shutdown)

    app.router.add_get('/', index)
    app.router.add_post('/offer', offer)
    app.router.add_get('/ice-config', ice_config)
    app.router.add_get('/health', health)
    # Serve static assets (JS, CSS) from webrtc_client/
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
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info('WebRTC server listening on http://0.0.0.0:%d', port)

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
