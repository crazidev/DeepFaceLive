/**
 * DeepFaceLive WebRTC Camera Streamer
 *
 * Captures the browser camera via getUserMedia, establishes a WebRTC
 * peer connection to the Python signaling server, and streams video
 * frames for face-swap processing.
 */

// ── DOM refs ─────────────────────────────────────────────────────────
const videoEl = document.getElementById('preview');
const placeholder = document.getElementById('placeholder');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const cameraSelect = document.getElementById('camera-select');
const resolutionSel = document.getElementById('resolution-select');
const btnStart = document.getElementById('btn-start');
const btnStop = document.getElementById('btn-stop');

// ── State ────────────────────────────────────────────────────────────
let pc = null;   // RTCPeerConnection
let stream = null;   // MediaStream from camera
let running = false;

// ── Helpers ──────────────────────────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = 'status-dot ' + state;   // '', 'connecting', 'connected', 'error'
  statusText.textContent = text;
}

function parseResolution() {
  const [w, h] = resolutionSel.value.split('x').map(Number);
  return { width: w, height: h };
}

// ── Enumerate cameras ────────────────────────────────────────────────
async function enumerateDevices() {
  try {
    // Need a temporary stream to get labelled device list in most browsers
    const tmpStream = await navigator.mediaDevices.getUserMedia({ video: true });
    tmpStream.getTracks().forEach(t => t.stop());

    const devices = await navigator.mediaDevices.enumerateDevices();
    const videoDevices = devices.filter(d => d.kind === 'videoinput');

    cameraSelect.innerHTML = '';
    if (videoDevices.length === 0) {
      cameraSelect.innerHTML = '<option value="">No cameras found</option>';
      return;
    }
    videoDevices.forEach((d, i) => {
      const opt = document.createElement('option');
      opt.value = d.deviceId;
      opt.textContent = d.label || `Camera ${i + 1}`;
      cameraSelect.appendChild(opt);
    });
  } catch (err) {
    console.error('Device enumeration failed:', err);
    cameraSelect.innerHTML = '<option value="">Camera access denied</option>';
    setStatus('error', 'Camera permission denied');
  }
}

// ── Start streaming ──────────────────────────────────────────────────
async function startStreaming() {
  if (running) return;
  const deviceId = cameraSelect.value;
  if (!deviceId) { setStatus('error', 'No camera selected'); return; }

  btnStart.disabled = true;
  setStatus('connecting', 'Requesting camera…');

  const res = parseResolution();

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        deviceId: { exact: deviceId },
        width: { ideal: res.width },
        height: { ideal: res.height },
        frameRate: { ideal: 60 },
      },
      audio: false,
    });
  } catch (err) {
    console.error('getUserMedia failed:', err);
    setStatus('error', 'Camera access failed');
    btnStart.disabled = false;
    return;
  }

  // Show preview
  videoEl.srcObject = stream;
  placeholder.style.display = 'none';

  setStatus('connecting', 'Connecting to server…');

  try {
    pc = new RTCPeerConnection({
      iceServers: [],   // LAN-only; no STUN needed
    });

    // Add camera tracks
    stream.getTracks().forEach(track => pc.addTrack(track, stream));

    pc.oniceconnectionstatechange = () => {
      const st = pc.iceConnectionState;
      if (st === 'connected' || st === 'completed') {
        setStatus('connected', 'Streaming to DeepFaceLive');
      } else if (st === 'disconnected') {
        setStatus('error', 'Disconnected — retrying…');
        // Auto-reconnect after short delay
        setTimeout(() => { if (running) reconnect(); }, 2000);
      } else if (st === 'failed') {
        setStatus('error', 'Connection failed');
        stopStreaming();
      }
    };

    // Create and send offer
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering to complete (or timeout after 3 s)
    await new Promise((resolve) => {
      if (pc.iceGatheringState === 'complete') { resolve(); return; }
      const timeout = setTimeout(resolve, 3000);
      pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === 'complete') {
          clearTimeout(timeout);
          resolve();
        }
      };
    });

    const resp = await fetch('/offer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
      }),
    });

    if (!resp.ok) throw new Error(`Server responded ${resp.status}`);
    const answer = await resp.json();
    await pc.setRemoteDescription(answer);

    running = true;
    btnStop.disabled = false;
    setStatus('connected', 'Streaming to DeepFaceLive');

  } catch (err) {
    console.error('WebRTC setup failed:', err);
    setStatus('error', 'Failed to connect');
    cleanupPC();
    cleanupStream();
    btnStart.disabled = false;
  }
}

// ── Stop ─────────────────────────────────────────────────────────────
function stopStreaming() {
  running = false;
  cleanupPC();
  cleanupStream();
  placeholder.style.display = '';
  videoEl.srcObject = null;
  btnStart.disabled = false;
  btnStop.disabled = true;
  setStatus('', 'Stopped');
}

function cleanupPC() {
  if (pc) {
    pc.oniceconnectionstatechange = null;
    pc.close();
    pc = null;
  }
}

function cleanupStream() {
  if (stream) {
    stream.getTracks().forEach(t => t.stop());
    stream = null;
  }
}

// ── Reconnect (same camera) ─────────────────────────────────────────
async function reconnect() {
  cleanupPC();
  // Re-use existing stream if tracks are still live
  if (stream && stream.getVideoTracks().some(t => t.readyState === 'live')) {
    setStatus('connecting', 'Reconnecting…');
    try {
      pc = new RTCPeerConnection({ iceServers: [] });
      stream.getTracks().forEach(track => pc.addTrack(track, stream));

      pc.oniceconnectionstatechange = () => {
        const st = pc.iceConnectionState;
        if (st === 'connected' || st === 'completed') {
          setStatus('connected', 'Streaming to DeepFaceLive');
        } else if (st === 'disconnected') {
          setStatus('error', 'Disconnected — retrying…');
          setTimeout(() => { if (running) reconnect(); }, 2000);
        } else if (st === 'failed') {
          setStatus('error', 'Connection failed');
          stopStreaming();
        }
      };

      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise((resolve) => {
        if (pc.iceGatheringState === 'complete') { resolve(); return; }
        const timeout = setTimeout(resolve, 3000);
        pc.onicegatheringstatechange = () => {
          if (pc.iceGatheringState === 'complete') {
            clearTimeout(timeout);
            resolve();
          }
        };
      });

      const resp = await fetch('/offer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
      });
      if (!resp.ok) throw new Error(`Server ${resp.status}`);
      const answer = await resp.json();
      await pc.setRemoteDescription(answer);
      setStatus('connected', 'Streaming to DeepFaceLive');
    } catch (err) {
      console.error('Reconnect failed:', err);
      setStatus('error', 'Reconnection failed');
      stopStreaming();
    }
  } else {
    stopStreaming();
  }
}

// ── Bind events ──────────────────────────────────────────────────────
btnStart.addEventListener('click', startStreaming);
btnStop.addEventListener('click', stopStreaming);

// Init
enumerateDevices();
