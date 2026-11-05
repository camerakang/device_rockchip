"use strict";
// Video-only WHEP with complete ICE gathering, no external STUN server.
// Status loss closes the session; decoded-frame progress is checked separately.
class StreamPlayer {
  constructor(video, message) {
    this.video = video;
    this.message = message;
    this.pc = null;
    this.url = null;
    this.session = null;
    this.generation = 0;
    this.retryAt = 0;
    this.frames = 0;
    this.lastFrameAt = 0;
    this.startedAt = 0;
    this.abort = null;
  }
  setSource(url) {
    if (this.url !== url) { this.close(); this.url = url; this.retryAt = 0; }
    if (url && !this.pc && performance.now() >= this.retryAt) this.connect();
  }
  async connect() {
    const generation = ++this.generation;
    this.startedAt = performance.now();
    if (typeof RTCPeerConnection === "undefined") { this.message.textContent="此浏览器不支持 WebRTC"; this.retryAt=Infinity; return; }
    const pc = new RTCPeerConnection({iceServers: [], bundlePolicy: "max-bundle"});
    this.pc = pc;
    this.abort = new AbortController();
    const deadline = setTimeout(() => this.abort?.abort(), 7000);
    const active = () => generation === this.generation;
    pc.addTransceiver("video", {direction: "recvonly"});
    pc.ontrack = event => {
      if (!active()) return;
      this.video.srcObject = event.streams[0] || new MediaStream([event.track]);
      this.frames = this.video.getVideoPlaybackQuality?.().totalVideoFrames || 0;
      this.video.play().catch(() => { this.message.textContent = "点击画面播放"; });
    };
    pc.onconnectionstatechange = () => {
      if (active() && ["failed", "closed"].includes(pc.connectionState)) this.fail("视频连接中断");
    };
    this.message.hidden = false;
    this.message.textContent = "正在连接视频";
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise((resolve, reject) => {
        if (pc.iceGatheringState === "complete") { resolve(); return; }
        const timer = setTimeout(() => { pc.removeEventListener("icegatheringstatechange", changed); reject(new Error("ICE timeout")); }, 3000);
        const changed = () => {
          if (pc.iceGatheringState === "complete") { clearTimeout(timer); pc.removeEventListener("icegatheringstatechange", changed); resolve(); }
        };
        pc.addEventListener("icegatheringstatechange", changed);
      });
      if (!active()) return;
      const response = await SS07Session.request(this.url, {method: "POST", headers: {"Content-Type": "application/sdp"},
        body: pc.localDescription.sdp, signal: this.abort.signal});
      if (response.status !== 201) throw new Error("WHEP unavailable");
      const session = response.headers.get("Location");
      if (!session || !/^\/media\/(front|back|left|right|down|sdi)\/whep\/[a-fA-F0-9-]{36}$/.test(session)) throw new Error("Invalid WHEP session");
      if (!active()) { SS07Session.request(session, {method: "DELETE"}).catch(() => {}); return; }
      this.session = session;
      await pc.setRemoteDescription({type: "answer", sdp: await response.text()});
    } catch (error) {
      if (active()) this.fail("视频暂不可用，正在重连");
    } finally { clearTimeout(deadline); }
  }
  check() {
    if (!this.pc) { this.setSource(this.url); return; }
    const frames = this.video.getVideoPlaybackQuality?.().totalVideoFrames || 0;
    const now = performance.now();
    if (frames > this.frames && this.video.readyState >= 2) {
      this.frames = frames;
      this.lastFrameAt = now;
      this.message.hidden = true;
    } else if (now - (this.lastFrameAt || this.startedAt) > (this.lastFrameAt ? 3000 : 10000)) {
      this.fail("画面已停滞，正在重连");
    }
  }
  fail(message) {
    const url = this.url;
    this.close();
    this.url = url;
    this.retryAt = performance.now() + 2000;
    this.message.textContent = message;
    this.message.hidden = false;
  }
  close() {
    ++this.generation;
    this.abort?.abort();
    this.abort = null;
    if (this.pc) { this.pc.onconnectionstatechange = null; this.pc.ontrack = null; this.pc.close(); }
    if (this.session) SS07Session.request(this.session, {method: "DELETE", keepalive: true}).catch(() => {});
    this.pc = null; this.session = null; this.url = null;
    this.video.srcObject = null;
    this.frames = 0; this.lastFrameAt = 0;
    this.message.hidden = false;
  }
}
