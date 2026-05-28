// Vanilla WebRTC client matching pipecat's SmallWebRTCConnection signaling.
// Protocol: POST /api/offer with {sdp, type, pc_id?} → returns {sdp, type, pc_id}.

const connectBtn = document.getElementById("btn-connect");
const disconnectBtn = document.getElementById("btn-disconnect");
const playBtn = document.getElementById("btn-play");
const statusEl = document.getElementById("status");
const remoteAudio = document.getElementById("remote-audio");
const levelBar = document.getElementById("level-bar");

let pc = null;
let pcId = null;
let micStream = null;
let audioCtx = null;
let levelRafId = null;

function setStatus(text, className = "") {
  statusEl.textContent = text;
  statusEl.className = "status" + (className ? " " + className : "");
}

// STUN server comes from the server (/api/config) so it stays single-sourced;
// fall back to Google's public STUN if the config call fails.
const DEFAULT_STUN = "stun:stun.l.google.com:19302";
async function getStunServer() {
  try {
    const res = await fetch("/api/config");
    if (res.ok) {
      const cfg = await res.json();
      if (cfg.stun_server) return cfg.stun_server;
    }
  } catch (e) {
    /* fall through to default */
  }
  return DEFAULT_STUN;
}

async function connect() {
  connectBtn.disabled = true;
  setStatus("requesting microphone…", "connecting");
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        // Enable browser-level AEC so the bot does not hear its own audio
        // played through speakers. WebRTC AEC is the primary echo defence.
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (e) {
    setStatus("microphone denied", "error");
    connectBtn.disabled = false;
    return;
  }

  startLevelMeter(micStream);

  pc = new RTCPeerConnection({ iceServers: [{ urls: await getStunServer() }] });

  // Pipecat sends transcriptions/metrics over a data channel. The server
  // never creates one itself, so the client must.
  pc.createDataChannel("pipecat");

  // addTrack creates an audio transceiver with direction=sendrecv automatically.
  // No explicit addTransceiver — that can lead to two m-lines.
  micStream.getAudioTracks().forEach((t) => pc.addTrack(t, micStream));

  pc.ontrack = (e) => {
    if (e.track.kind !== "audio") return;

    // Prefer the stream the peer attached the track to; fall back to wrapping.
    if (e.streams && e.streams[0]) {
      remoteAudio.srcObject = e.streams[0];
    } else {
      remoteAudio.srcObject = new MediaStream([e.track]);
    }
    remoteAudio.volume = 1.0;
    remoteAudio.muted = false;

    tryPlay();
  };

  pc.onconnectionstatechange = () => {
    if (!pc) return;
    const s = pc.connectionState;
    if (s === "connected") setStatus("connected — speak now", "connected");
    else if (s === "connecting") setStatus("connecting…", "connecting");
    else if (s === "failed") {
      setStatus("connection failed", "error");
      cleanup();
    } else if (s === "disconnected" || s === "closed") {
      setStatus("disconnected");
      cleanup();
    }
  };

  setStatus("creating offer…", "connecting");
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  await waitForIceGatheringComplete(pc);

  setStatus("signaling…", "connecting");
  let answer;
  try {
    const res = await fetch("/api/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
        pc_id: pcId,
      }),
    });
    if (!res.ok) throw new Error(`signaling failed: ${res.status}`);
    answer = await res.json();
  } catch (e) {
    setStatus("signaling failed", "error");
    cleanup();
    return;
  }

  pcId = answer.pc_id || pcId;
  await pc.setRemoteDescription({ sdp: answer.sdp, type: answer.type });

  disconnectBtn.disabled = false;
}

function tryPlay() {
  const p = remoteAudio.play();
  if (p && typeof p.catch === "function") {
    p.catch(() => {
      // Autoplay blocked — surface a manual play button.
      if (playBtn) playBtn.hidden = false;
    });
  }
}

function waitForIceGatheringComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const check = () => {
      if (pc.iceGatheringState === "complete") {
        pc.removeEventListener("icegatheringstatechange", check);
        resolve();
      }
    };
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(() => {
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    }, 2000);
  });
}

function startLevelMeter(stream) {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioCtx.createMediaStreamSource(stream);
  const analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const buf = new Uint8Array(analyser.frequencyBinCount);
  const tick = () => {
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / buf.length);
    const pct = Math.min(100, Math.round(rms * 300));
    levelBar.style.width = pct + "%";
    levelRafId = requestAnimationFrame(tick);
  };
  tick();
}

function cleanup() {
  if (pc) {
    pc.getSenders().forEach((s) => s.track && s.track.stop());
    pc.close();
    pc = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
  }
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
  }
  if (levelRafId) {
    cancelAnimationFrame(levelRafId);
    levelRafId = null;
  }
  levelBar.style.width = "0";
  pcId = null;
  if (playBtn) playBtn.hidden = true;
  connectBtn.disabled = false;
  disconnectBtn.disabled = true;
}

connectBtn.addEventListener("click", connect);
disconnectBtn.addEventListener("click", () => {
  setStatus("disconnecting…");
  cleanup();
  setStatus("idle");
});
if (playBtn) {
  playBtn.addEventListener("click", () => {
    tryPlay();
    playBtn.hidden = true;
  });
}
