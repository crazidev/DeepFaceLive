/**
 * DeepFaceLive WebRTC Camera Streamer
 *
 * Captures the browser camera via getUserMedia, establishes a WebRTC
 * peer connection to the Python signaling server, and streams video
 * frames for face-swap processing.
 */

// ── Configuration & Presets ──────────────────────────────────────────
const PRESETS = {
  'low-latency': {
    width: 1280,
    height: 720,
    fps: 30,
    bitrate: 1500 * 1000 // 1.5 Mbps
  },
  'balanced': {
    width: 1280,
    height: 720,
    fps: 30,
    bitrate: 3000 * 1000 // 3.0 Mbps
  },
  'high-quality': {
    width: 1920,
    height: 1080,
    fps: 30,
    bitrate: 6000 * 1000 // 6.0 Mbps
  }
};

// ── DOM refs ─────────────────────────────────────────────────────────
const videoEl = document.getElementById('preview');
const placeholder = document.getElementById('placeholder');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const cameraSelect = document.getElementById('camera-select');
const presetSel = document.getElementById('preset-select');
const btnStart = document.getElementById('btn-start');
const btnStop = document.getElementById('btn-stop');

// ── State ────────────────────────────────────────────────────────────
let pc = null;         // RTCPeerConnection
let localStream = null; // Persistent MediaStream from camera for preview + streaming
let iceConfig = null;   // Fetched dynamic ICE configurations (STUN/TURN)
let running = false;

// ── Realtime Stats & Adaptation State ────────────────────────────────
let statsInterval = null;
let lastBytesSent = 0;
let lastPacketsSent = 0;
let lastPacketsLost = 0;
let lastStatsTime = 0;
let adaptiveBitrateAdjustment = 1.0; // 1.0 = 100% of preset bitrate

// ── Helpers ──────────────────────────────────────────────────────────
function setStatus(state, text) {
  statusDot.className = 'status-dot ' + state;   // '', 'connecting', 'connected', 'error'
  statusText.textContent = text;
}

function getPresetConstraints() {
  const preset = PRESETS[presetSel.value] || PRESETS['balanced'];
  return {
    width: preset.width,
    height: preset.height,
    fps: preset.fps
  };
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

  const res = getPresetConstraints();
  
  // Attempt 1: Optimal presets
  const optimalConstraints = {
    video: {
      deviceId: { exact: deviceId },
      width: { ideal: res.width },
      height: { ideal: res.height },
      frameRate: { ideal: res.fps },
    },
    audio: false,
  };

  try {
    localStream = await navigator.mediaDevices.getUserMedia(optimalConstraints);
    console.log("Successfully acquired camera with optimal presets:", res);
  } catch (err) {
    console.warn("Optimal constraints failed, trying safe mobile compatibility fallback...", err);
    
    // Attempt 2: iPhone/mobile fallback (remove exact framerate and ideal dimension bounds)
    const fallbackConstraints = {
      video: {
        deviceId: { exact: deviceId }
      },
      audio: false
    };
    
    try {
      localStream = await navigator.mediaDevices.getUserMedia(fallbackConstraints);
      console.log("Successfully acquired camera with fallback constraints.");
    } catch (fallbackErr) {
      console.error("Camera acquisition completely failed:", fallbackErr);
      setStatus('error', 'Camera access failed');
      placeholder.style.display = '';
      videoEl.srcObject = null;
      return;
    }
  }

  videoEl.srcObject = localStream;
  placeholder.style.display = 'none';
  setStatus('', 'Preview Active');
  
  // Explicitly trigger play to prevent Safari pausing playback
  try {
    await videoEl.play();
  } catch (playErr) {
    console.warn("Autoplay blocked, waiting for user gesture:", playErr);
  }
  
  // Dynamically apply settings to active sender if already streaming
  if (running && pc) {
    // Re-configure active sender with new resolution by replacing track
    const videoTrack = localStream.getVideoTracks()[0];
    const senders = pc.getSenders();
    for (const sender of senders) {
      if (sender.track && sender.track.kind === 'video') {
        await sender.replaceTrack(videoTrack);
        console.log("Dynamically replaced active stream track with new resolution/framerate.");
      }
    }
    await applySenderParameters(presetSel.value);
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

    await startPreview();
  } catch (err) {
    console.error('Device enumeration failed:', err);
    cameraSelect.innerHTML = '<option value="">Camera access denied</option>';
    setStatus('error', 'Camera permission denied');
  }
}

// ── Apply Sender Parameters (Bitrate/Framerate Pacing) ───────────────
async function applySenderParameters(presetName) {
  if (!pc) return;
  const preset = PRESETS[presetName] || PRESETS['balanced'];
  const targetBitrate = Math.round(preset.bitrate * adaptiveBitrateAdjustment);

  const senders = pc.getSenders();
  for (const sender of senders) {
    if (sender.track && sender.track.kind === 'video') {
      const params = sender.getParameters();
      if (!params.encodings) {
        params.encodings = [{}];
      }
      params.encodings.forEach(enc => {
        enc.maxBitrate = targetBitrate;
        enc.maxFramerate = preset.fps;
      });
      try {
        await sender.setParameters(params);
        console.log(`Applied sender parameter (preset: ${presetName}): maxBitrate = ${targetBitrate} bps, maxFramerate = ${preset.fps} FPS`);
      } catch (err) {
        console.error("Failed to setParameters on sender:", err);
      }
    }
  }
}

// ── Force H264 Codec preference ──────────────────────────────────────
function applyCodecPreferences() {
  try {
    if (!pc) return;
    if (typeof RTCRtpTransceiver.prototype.setCodecPreferences !== 'undefined') {
      const transceivers = pc.getTransceivers();
      for (const transceiver of transceivers) {
        // Safe check for video transceiver kind (sender-only check avoids TypeError on null receiver tracks in Safari)
        const isVideoTransceiver = (transceiver.sender && transceiver.sender.track && transceiver.sender.track.kind === 'video');
        if (isVideoTransceiver) {
          const cap = RTCRtpReceiver.getCapabilities('video');
          if (cap && cap.codecs) {
            // Sort to prioritize H264 (baseline, main, high profiles) over VP8 / others
            const sortedCodecs = [...cap.codecs].sort((a, b) => {
              const aName = a.mimeType.toLowerCase();
              const bName = b.mimeType.toLowerCase();
              const aIsH264 = aName.includes('h264');
              const bIsH264 = bName.includes('h264');

              if (aIsH264 && !bIsH264) return -1;
              if (!aIsH264 && bIsH264) return 1;

              if (aIsH264 && bIsH264) {
                const aParams = JSON.stringify(a.sdpFmtpLine || '').toLowerCase();
                const bParams = JSON.stringify(b.sdpFmtpLine || '').toLowerCase();
                // Prioritize packetization-mode=1 for H264
                const aHasMode = aParams.includes('packetization-mode=1');
                const bHasMode = bParams.includes('packetization-mode=1');
                if (aHasMode && !bHasMode) return -1;
                if (!aHasMode && bHasMode) return 1;
              }
              return 0;
            });
            try {
              transceiver.setCodecPreferences(sortedCodecs);
              console.log("Configured prioritized H264 codecs preferences.");
            } catch (e) {
              console.warn("setCodecPreferences warning:", e);
            }
          }
        }
      }
    }
  } catch (err) {
    console.warn("applyCodecPreferences failed safely:", err);
  }
}

// ── Stats Monitoring & Network Adaptation Loop ───────────────────────
function startStatsMonitoring() {
  if (statsInterval) clearInterval(statsInterval);
  lastBytesSent = 0;
  lastPacketsSent = 0;
  lastPacketsLost = 0;
  lastStatsTime = performance.now();
  adaptiveBitrateAdjustment = 1.0;

  document.getElementById('metrics-overlay').style.display = 'flex';

  statsInterval = setInterval(async () => {
    if (!pc || pc.iceConnectionState !== 'connected') return;

    try {
      const stats = await pc.getStats();
      let bytesSent = 0;
      let packetsSent = 0;
      let packetsLost = 0;
      let rtt = 0;
      let width = 0;
      let height = 0;
      let fps = 0;

      stats.forEach(report => {
        if (report.type === 'outbound-rtp' && report.kind === 'video') {
          bytesSent = report.bytesSent;
          packetsSent = report.packetsSent;
          width = report.frameWidth || 0;
          height = report.frameHeight || 0;
          fps = report.framesPerSecond || 0;
        }
        if (report.type === 'remote-inbound-rtp' && report.kind === 'video') {
          packetsLost = report.packetsLost || 0;
          rtt = report.roundTripTime || 0;
        }
        if (report.type === 'candidate-pair' && report.state === 'succeeded') {
          rtt = report.currentRoundTripTime || rtt;
        }
      });

      const now = performance.now();
      const durationSec = (now - lastStatsTime) / 1000.0;

      let bitrateKbps = 0;
      if (lastBytesSent > 0 && durationSec > 0) {
        bitrateKbps = Math.round(((bytesSent - lastBytesSent) * 8) / (durationSec * 1000));
      }

      let lossRate = 0;
      if (lastPacketsSent > 0 && packetsSent > lastPacketsSent) {
        const sentDiff = packetsSent - lastPacketsSent;
        const lostDiff = Math.max(0, packetsLost - lastPacketsLost);
        lossRate = (lostDiff / (sentDiff + lostDiff)) * 100;
      }

      lastBytesSent = bytesSent;
      lastPacketsSent = packetsSent;
      lastPacketsLost = packetsLost;
      lastStatsTime = now;

      // Update UI elements
      document.getElementById('metric-res').textContent = width ? `${width}x${height}` : '720p';
      document.getElementById('metric-fps').textContent = Math.round(fps) || '0';
      document.getElementById('metric-bitrate').textContent = `${bitrateKbps} Kbps`;
      document.getElementById('metric-rtt').textContent = `${Math.round(rtt * 1000)}ms`;
      document.getElementById('metric-loss').textContent = `${lossRate.toFixed(1)}%`;

      // Adaptation Logic
      const rttMs = rtt * 1000;
      let needsAdaptation = false;

      if (rttMs > 250 || lossRate > 5.0) {
        if (adaptiveBitrateAdjustment > 0.5) {
          adaptiveBitrateAdjustment = 0.5;
          needsAdaptation = true;
          console.warn(`Severe congestion. Adapting bitrate factor to 50%.`);
        }
      } else if (rttMs > 150 || lossRate > 2.0) {
        if (adaptiveBitrateAdjustment > 0.75) {
          adaptiveBitrateAdjustment = 0.75;
          needsAdaptation = true;
          console.warn(`Moderate congestion. Adapting bitrate factor to 75%.`);
        }
      } else if (rttMs < 80 && lossRate < 1.0) {
        if (adaptiveBitrateAdjustment < 1.0) {
          adaptiveBitrateAdjustment = Math.min(1.0, adaptiveBitrateAdjustment + 0.1);
          needsAdaptation = true;
          console.log(`Good network metrics. Restoring bitrate factor to ${(adaptiveBitrateAdjustment * 100).toFixed(0)}%.`);
        }
      }

      if (needsAdaptation) {
        await applySenderParameters(presetSel.value);
      }

    } catch (err) {
      console.warn("Error pulling WebRTC statistics:", err);
    }
  }, 2000);
}

function stopStatsMonitoring() {
  if (statsInterval) {
    clearInterval(statsInterval);
    statsInterval = null;
  }
  document.getElementById('metrics-overlay').style.display = 'none';
}

// ── Translate SDP Candidates (VM/Docker network translation helper) ────
function translateSDPCandidates(sdp) {
  const reachableHost = window.location.hostname;
  if (reachableHost && !reachableHost.includes('localhost') && reachableHost !== '127.0.0.1') {
    // Replace internal virtual IPs (e.g. 192.168.5.15) with the physical hostname used to access the app
    const ipRegex = /(a=candidate:\S+ \d+ \S+ \d+ )(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/g;
    return sdp.replace(ipRegex, (match, prefix, ip) => {
      const parts = ip.split('.').map(Number);
      const isPrivate = 
        (parts[0] === 10) || 
        (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) || 
        (parts[0] === 192 && parts[1] === 168);
        
      if (isPrivate) {
        console.log(`SDP Translation: mapping local VM IP ${ip} to reachable host IP ${reachableHost}`);
        return prefix + reachableHost;
      }
      return match;
    });
  }
  return sdp;
}

// ── Start streaming ──────────────────────────────────────────────────
async function startStreaming() {
  if (running) return;

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
    pc = new RTCPeerConnection({
      iceServers: iceConfig || [
        { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302', 'stun:stun2.l.google.com:19302'] }
      ],
    });

    localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
    
    // Apply low-latency codec preferences first
    applyCodecPreferences();

    pc.oniceconnectionstatechange = () => {
      const st = pc.iceConnectionState;
      if (st === 'connected' || st === 'completed') {
        setStatus('connected', 'Streaming to DeepFaceLive');
        startStatsMonitoring();
      } else if (st === 'disconnected') {
        setStatus('error', 'Disconnected — retrying…');
        setTimeout(() => { if (running) reconnect(); }, 2000);
      } else if (st === 'failed') {
        setStatus('error', 'Connection failed');
        stopStreaming();
      }
    };

    let offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering
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
    
    // Dynamically translate SDP host candidates to bridge local virtualization routes
    answer.sdp = translateSDPCandidates(answer.sdp);
    
    await pc.setRemoteDescription(answer);

    // Apply high-performance streaming parameters via RTCRtpSender
    await applySenderParameters(presetSel.value);

    running = true;
    btnStop.disabled = false;
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
  stopStatsMonitoring();
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
  stopStatsMonitoring();
  if (localStream && localStream.getVideoTracks().some(t => t.readyState === 'live')) {
    setStatus('connecting', 'Reconnecting…');
    try {
      pc = new RTCPeerConnection({
        iceServers: iceConfig || [
          { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302', 'stun:stun2.l.google.com:19302'] }
        ],
      });
      localStream.getTracks().forEach(track => pc.addTrack(track, localStream));
      applyCodecPreferences();

      pc.oniceconnectionstatechange = () => {
        const st = pc.iceConnectionState;
        if (st === 'connected' || st === 'completed') {
          setStatus('connected', 'Streaming to DeepFaceLive');
          startStatsMonitoring();
        } else if (st === 'disconnected') {
          setStatus('error', 'Disconnected — retrying…');
          setTimeout(() => { if (running) reconnect(); }, 2000);
        } else if (st === 'failed') {
          setStatus('error', 'Connection failed');
          stopStreaming();
        }
      };

      let offer = await pc.createOffer();
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
      
      // Dynamically translate SDP host candidates to bridge local virtualization routes
      answer.sdp = translateSDPCandidates(answer.sdp);
      
      await pc.setRemoteDescription(answer);
      
      await applySenderParameters(presetSel.value);
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
presetSel.addEventListener('change', startPreview);

// Init
(async () => {
  await fetchIceConfig();
  await enumerateDevices();
})();
