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
let pc = null;         // RTCPeerConnection
let localStream = null; // Persistent MediaStream from camera for preview + streaming
let iceConfig = null;   // Fetched dynamic ICE configurations (STUN/TURN)
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

/**
 * Modifies the SDP string before setting local description to force the
 * browser to use a high target bitrate (Application Specific bandwidth cap).
 */
function setSDPBitrate(sdp, bitrateKbps) {
  const lines = sdp.split('\r\n');
  let lineIndex = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].indexOf('m=video') === 0) {
      lineIndex = i;
      break;
    }
  }
  if (lineIndex === -1) {
    return sdp;
  }
  
  let hasAS = false;
  for (let i = lineIndex + 1; i < lines.length; i++) {
    if (lines[i].indexOf('m=') === 0) {
      break; // reached next media section
    }
    if (lines[i].indexOf('b=AS:') === 0) {
      lines[i] = `b=AS:${bitrateKbps}`;
      hasAS = true;
      break;
    }
  }
  
  if (!hasAS) {
    lines.splice(lineIndex + 1, 0, `b=AS:${bitrateKbps}`);
  }
  
  return lines.join('\r\n');
}

// ── Fetch ICE Config ─────────────────────────────────────────────────
async function fetchIceConfig() {
  try {
    const resp = await fetch('/ice-config');
    if (resp.ok) {
      const data = await resp.json();
      iceConfig = data.iceServers;
      console.log('Successfully loaded ICE relay config:', iceConfig);
    }
  } catch (err) {
    console.error('Failed to load dynamic ICE configuration:', err);
  }
}

// ── Camera Preview ───────────────────────────────────────────────────
async function startPreview() {
  cleanupStream();
  const deviceId = cameraSelect.value;
  if (!deviceId) return;

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    const warningEl = document.getElementById('secure-warning');
    if (warningEl) warningEl.style.display = 'flex';
    setStatus('error', 'Camera access blocked (HTTPS Required)');
    return;
  }

  const res = parseResolution();
  try {
    // Ideal settings for high definition (HD) camera streaming
    localStream = await navigator.mediaDevices.getUserMedia({
      video: {
        deviceId: { exact: deviceId },
        width: { ideal: res.width },
        height: { ideal: res.height },
        frameRate: { ideal: 60 },
      },
      audio: false,
    });
    videoEl.srcObject = localStream;
    placeholder.style.display = 'none';
    setStatus('', 'Preview Active');
  } catch (err) {
    console.error('Failed to get camera preview:', err);
    setStatus('error', 'Camera access failed');
    placeholder.style.display = '';
    videoEl.srcObject = null;
  }
}

// ── Enumerate cameras ────────────────────────────────────────────────
async function enumerateDevices() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    const warningEl = document.getElementById('secure-warning');
    if (warningEl) warningEl.style.display = 'flex';
    cameraSelect.innerHTML = '<option value="">HTTPS required</option>';
    setStatus('error', 'Camera access blocked (HTTPS Required)');
    return;
  }

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

    // Start previewing immediately on dropdown load
    await startPreview();
  } catch (err) {
    console.error('Device enumeration failed:', err);
    cameraSelect.innerHTML = '<option value="">Camera access denied</option>';
    setStatus('error', 'Camera permission denied');
  }
}

// ── Start streaming ──────────────────────────────────────────────────
async function startStreaming() {
  if (running) return;

  // Ensure camera preview stream is warm and active
  if (!localStream || !localStream.getVideoTracks().some(t => t.readyState === 'live')) {
    await startPreview();
  }

  if (!localStream) {
    setStatus('error', 'No active camera feed to stream');
    return;
  }

  btnStart.disabled = true;
  setStatus('connecting', 'Connecting to DeepFaceLive…');

  try {
    // Connect WebRTC using dynamic dynamic configurations (supporting remote tunnels like RunPod TLS TURN)
    pc = new RTCPeerConnection({
      iceServers: iceConfig || [
        { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302', 'stun:stun2.l.google.com:19302'] }
      ],
    });

    // Feed tracks from existing warm localStream
    localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

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

    // Create and send offer
    let offer = await pc.createOffer();

    // Optimize streaming quality: set SDP video bandwidth to 8000 Kbps (8 Mbps) for full HD
    offer.sdp = setSDPBitrate(offer.sdp, 8000);
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering to complete (max 3 s)
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
    btnStart.disabled = false;
  }
}

// ── Stop ─────────────────────────────────────────────────────────────
function stopStreaming() {
  running = false;
  cleanupPC();
  btnStart.disabled = false;
  btnStop.disabled = true;
  setStatus('', 'Stopped (Preview Active)');
}

function cleanupPC() {
  if (pc) {
    pc.oniceconnectionstatechange = null;
    pc.close();
    pc = null;
  }
}

function cleanupStream() {
  if (localStream) {
    localStream.getTracks().forEach(t => t.stop());
    localStream = null;
  }
}

// ── Reconnect (same stream) ──────────────────────────────────────────
async function reconnect() {
  cleanupPC();
  if (localStream && localStream.getVideoTracks().some(t => t.readyState === 'live')) {
    setStatus('connecting', 'Reconnecting…');
    try {
      pc = new RTCPeerConnection({
        iceServers: iceConfig || [
          { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302', 'stun:stun2.l.google.com:19302'] }
        ],
      });
      localStream.getTracks().forEach(track => pc.addTrack(track, localStream));

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

      let offer = await pc.createOffer();
      offer.sdp = setSDPBitrate(offer.sdp, 8000);
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
cameraSelect.addEventListener('change', startPreview);
resolutionSel.addEventListener('change', startPreview);

// Init
(async () => {
  await fetchIceConfig();
  await enumerateDevices();
})();
