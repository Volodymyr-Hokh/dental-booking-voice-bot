// Vanilla WebRTC client matching pipecat's SmallWebRTCConnection signaling.
// Protocol: POST /api/offer with {sdp, type, pc_id?} → returns {sdp, type, pc_id}.

const connectBtn = document.getElementById("btn-connect");
const disconnectBtn = document.getElementById("btn-disconnect");
const playBtn = document.getElementById("btn-play");
const statusEl = document.getElementById("status");
const diagEl = document.getElementById("diag");
const remoteAudio = document.getElementById("remote-audio");
const levelBar = document.getElementById("level-bar");

let pc = null;
let pcId = null;
let micStream = null;
let audioCtx = null;
let levelRafId = null;
let statsTimer = null;
let lastBytesReceived = 0;

function setStatus(text, className = "") {
  statusEl.textContent = text;
  statusEl.className = "status" + (className ? " " + className : "");
}

function setDiag(text) {
  if (diagEl) diagEl.textContent = text;
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

  pc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });

  // Pipecat sends transcriptions/metrics over a data channel. The server
  // never creates one itself, so the client must.
  pc.createDataChannel("pipecat");

  // addTrack creates an audio transceiver with direction=sendrecv automatically.
  // No explicit addTransceiver — that can lead to two m-lines.
  micStream.getAudioTracks().forEach((t) => pc.addTrack(t, micStream));

  pc.ontrack = (e) => {
    console.log("ontrack", e.track.kind, "streams:", e.streams.length,
      "muted:", e.track.muted, "readyState:", e.track.readyState);
    if (e.track.kind !== "audio") return;

    // Prefer the stream the peer attached the track to; fall back to wrapping.
    if (e.streams && e.streams[0]) {
      remoteAudio.srcObject = e.streams[0];
    } else {
      const s = new MediaStream([e.track]);
      remoteAudio.srcObject = s;
    }
    remoteAudio.volume = 1.0;
    remoteAudio.muted = false;

    e.track.onmute = () => console.log("remote track muted");
    e.track.onunmute = () => console.log("remote track unmuted");
    e.track.onended = () => console.log("remote track ended");

    tryPlay();
    startStatsPolling();
  };

  pc.onconnectionstatechange = () => {
    if (!pc) return;
    const s = pc.connectionState;
    console.log("connectionState:", s);
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

  pc.oniceconnectionstatechange = () => {
    console.log("iceConnectionState:", pc && pc.iceConnectionState);
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
    console.error(e);
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
    p.catch((err) => {
      console.warn("remoteAudio.play() failed:", err && err.name, err && err.message);
      // Autoplay blocked — surface a manual play button.
      if (playBtn) playBtn.hidden = false;
    });
  }
}

function startStatsPolling() {
  if (statsTimer) return;
  lastBytesReceived = 0;
  statsTimer = setInterval(async () => {
    if (!pc) return;
    const stats = await pc.getStats();
    let inbound = null;
    let outbound = null;
    let candidatePair = null;
    stats.forEach((r) => {
      if (r.type === "inbound-rtp" && r.kind === "audio") inbound = r;
      if (r.type === "outbound-rtp" && r.kind === "audio") outbound = r;
      if (r.type === "candidate-pair" && r.nominated && r.state === "succeeded") {
        candidatePair = r;
      }
    });
    const inBytes = inbound ? inbound.bytesReceived : 0;
    const inPackets = inbound ? inbound.packetsReceived : 0;
    const inLost = inbound ? inbound.packetsLost : 0;
    const outBytes = outbound ? outbound.bytesSent : 0;
    const outPackets = outbound ? outbound.packetsSent : 0;
    const deltaIn = inBytes - lastBytesReceived;
    lastBytesReceived = inBytes;
    let line = `recv ${inBytes}B (${inPackets} pkts, lost ${inLost}, +${deltaIn}/s) | sent ${outBytes}B (${outPackets} pkts)`;
    if (candidatePair) {
      line += ` | rtt=${(candidatePair.currentRoundTripTime || 0).toFixed(3)}s`;
    }
    setDiag(line);
  }, 1000);
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
  if (statsTimer) {
    clearInterval(statsTimer);
    statsTimer = null;
  }
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
  setDiag("");
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
