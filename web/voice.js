// Voice: a WebRTC call to the gateway. The conversation itself still arrives through
// the chat WebSocket, so spoken turns, action cards and traces show in the window.
"use strict";

const voice = { pc: null, stream: null, dc: null, ping: null, audio: null };
const micBtn = document.getElementById("mic");
const bar = document.getElementById("voice-bar");
const label = document.getElementById("voice-state");

function setVoiceState(s) {
  bar.dataset.state = s;
  label.textContent = { connecting: "Connecting…", listening: "Listening", thinking: "Thinking…", speaking: "Speaking (talk to interrupt)", ended: "Call ended" }[s] || s;
}

async function startVoice() {
  if (voice.pc) return stopVoice();
  bar.hidden = false;
  setVoiceState("connecting");
  micBtn.setAttribute("aria-pressed", "true");
  try {
    voice.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    const pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
    voice.pc = pc;
    // Transceiver order matches the server: audio, camera video, screen video.
    pc.addTransceiver(voice.stream.getAudioTracks()[0], { direction: "sendrecv" });
    pc.addTransceiver("video", { direction: "sendrecv" });
    pc.addTransceiver("video", { direction: "sendrecv" });
    voice.dc = pc.createDataChannel("chat");
    voice.dc.onopen = () => { voice.ping = setInterval(() => voice.dc.readyState === "open" && voice.dc.send("ping-" + Date.now()), 1000); };
    pc.ontrack = (ev) => {
      if (ev.track.kind !== "audio") return;
      voice.audio = voice.audio || new Audio();
      voice.audio.autoplay = true;
      voice.audio.srcObject = new MediaStream([ev.track]);
    };
    pc.onconnectionstatechange = () => {
      if (["failed", "closed", "disconnected"].includes(pc.connectionState)) stopVoice();
    };
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((resolve) => {  // send one offer with all candidates (no trickle)
      if (pc.iceGatheringState === "complete") return resolve();
      const done = () => pc.iceGatheringState === "complete" && resolve();
      pc.addEventListener("icegatheringstatechange", done);
      setTimeout(resolve, 2000);
    });
    const r = await fetch("/api/voice/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + window.app.token },
      body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    await pc.setRemoteDescription(await r.json());
  } catch (err) {
    stopVoice();
    bar.hidden = false;
    label.textContent = "Voice unavailable: " + err.message;
  }
}

function stopVoice() {
  clearInterval(voice.ping);
  if (voice.stream) voice.stream.getTracks().forEach((t) => t.stop());
  if (voice.pc) voice.pc.close();
  Object.assign(voice, { pc: null, stream: null, dc: null, ping: null });
  micBtn.setAttribute("aria-pressed", "false");
  setVoiceState("ended");
  setTimeout(() => { if (!voice.pc) bar.hidden = true; }, 1500);
}

micBtn.onclick = startVoice;
document.getElementById("voice-stop").onclick = stopVoice;
window.addEventListener("assistant-event", (e) => {
  const ev = e.detail;
  if (ev.type === "voice_state" && voice.pc) setVoiceState(ev.state);
});
