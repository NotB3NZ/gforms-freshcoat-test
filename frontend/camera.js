export function cameraError(error) {
  const messages = {
    NotAllowedError: 'Camera permission was denied. Allow camera access in browser settings, or search participants.',
    NotFoundError: 'No camera found. Connect a camera, use a QR image, or search participants.',
    NotReadableError: 'Camera is in use or unavailable. Close other camera apps and try again, or search participants.',
    OverconstrainedError: 'That camera is unavailable. Select another camera or search participants.',
    SecurityError: 'Camera access requires HTTPS or localhost. Open the secure event address or search participants.'
  };
  return messages[error.name] || error.message || 'Camera stopped. Try again or search participants.';
}

export class CameraScanner {
  constructor({video, mediaDevices, decodeFrame, onResult, onError, onDevices, onState, canvasFactory = () => document.createElement('canvas')}) {
    Object.assign(this, {video, mediaDevices, decodeFrame, onResult, onError, onDevices, onState, canvasFactory});
    this.generation = 0;
    this.running = false;
    this.stream = null;
    this.timer = null;
  }
  async start(deviceId = '') {
    this.stop();
    const generation = this.generation;
    try {
      if (!this.mediaDevices?.getUserMedia) throw new DOMException('Use HTTPS or localhost, or search participants.', 'SecurityError');
      this.onState('Starting camera…');
      const stream = await this.mediaDevices.getUserMedia({audio: false, video: deviceId ? {deviceId: {exact: deviceId}} : {facingMode: {ideal: 'environment'}, width: {ideal: 960}, height: {ideal: 720}}});
      if (generation !== this.generation) { stream.getTracks().forEach(t => t.stop()); return; }
      this.stream = stream;
      this.video.srcObject = stream;
      await this.video.play();
      if (generation !== this.generation) return;
      this.running = true;
      this.onState('Camera ready · Hold one QR steady');
      try { this.onDevices((await this.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput')); } catch { /* selection remains optional */ }
      this.tick(generation);
    } catch (error) {
      if (generation === this.generation) { this.stop(); this.onError(cameraError(error)); }
    }
  }
  stop() {
    this.generation++;
    this.running = false;
    clearTimeout(this.timer);
    this.stream?.getTracks().forEach(t => t.stop());
    this.stream = null;
    this.video.srcObject = null;
    this.onState('Camera off');
  }
  async tick(generation) {
    if (!this.running || generation !== this.generation) return;
    try {
      if (this.video.videoWidth) {
        const canvas = this.canvasFactory();
        const ratio = Math.min(1, 960 / this.video.videoWidth);
        canvas.width = Math.round(this.video.videoWidth * ratio);
        canvas.height = Math.round(this.video.videoHeight * ratio);
        canvas.getContext('2d').drawImage(this.video, 0, 0, canvas.width, canvas.height);
        const result = await this.decodeFrame(canvas);
        if (generation !== this.generation) return;
        if (result.status !== 'no_qr') {
          this.stop(); // No repeated detection, camera capture or frame request during review.
          this.onResult(result);
          return;
        }
      }
    } catch (error) {
      if (generation === this.generation) { this.stop(); this.onError(error.message); }
      return;
    }
    if (this.running && generation === this.generation) this.timer = setTimeout(() => this.tick(generation), 450);
  }
}

export async function imageFrame(file) {
  if (file.size > 8 * 1024 * 1024) throw new Error('Choose a QR image smaller than 8 MB.');
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    const ratio = Math.min(1, 1280 / Math.max(image.width, image.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.width * ratio));
    canvas.height = Math.max(1, Math.round(image.height * ratio));
    canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
    return canvas;
  } finally { URL.revokeObjectURL(url); }
}
