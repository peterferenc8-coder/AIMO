/* avatar.js
 * The speaking avatar for the AI tab: a VRM model with idle life (breathing,
 * blinking, looking around, gentle sway) whose mouth is driven by the viseme
 * track that tts.py returns alongside each utterance.
 *
 * This is the speech half of the old /avatar demo. The demo's hand-grip IK,
 * tuning sliders and lip-sync debug panel are deliberately not carried over —
 * they were authoring tools, and they live in git history if ever needed.
 *
 * Loaded as an ES module and published as window.Avatar for the classic
 * scripts (ai.js) to drive.
 */
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';

// ── Lip-sync config ─────────────────────────────────────────────────────────
const VISEME_NAMES = ['aa', 'ih', 'ou', 'ee', 'oh'];
// Overall mouth openness.
const VISEME_GAIN = 0.75;
// Per-shape trim: the VRoid morphs are not equally strong. Fcl_MTH_A and
// Fcl_MTH_U open far wider than the rest at the same weight, so one flat gain
// leaves "aa"/"ou" gaping while "ih"/"ee" barely register.
const VISEME_SCALE = { aa: 0.55, ih: 0.95, ou: 0.55, ee: 0.9, oh: 0.8 };
// Easing time constants, seconds. All MUST stay well under a typical phoneme
// (30-100ms) or the mouth never reaches a shape before the next replaces it
// and the avatar reads as motionless.
//
// Real mouths open faster than they close, so the attack is the quickest of
// the three. A closure ('sil' mid-utterance — a /p/, /b/, /m/, or the pause
// before punctuation) still has to be crisp to read as a closure at all, so it
// gets its own constant rather than the slow settle used when nothing is being
// said and the face is simply returning to rest.
const VISEME_TAU_OPEN = 0.018;
const VISEME_TAU_CLOSE = 0.030;
const VISEME_TAU_REST = 0.060;

const MODEL_YAW = Math.PI;   // turn the model to face the camera

class Avatar {
  constructor() {
    this.vrm = null;
    this.ready = false;
    this.bones = {};
    this.visemeTrack = [];
    this.visemeIdx = 0;
    this.clock = null;          // () => milliseconds into the utterance, or null
    this.weights = Object.fromEntries(VISEME_NAMES.map((n) => [n, 0]));
    this._raf = null;
  }

  /** Boot the renderer into `container` and start loading `modelUrl`. */
  init(container, modelUrl) {
    if (this.renderer) return;          // already mounted
    this.container = container;

    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(28, 1, 0.1, 20);

    this.scene.add(new THREE.AmbientLight(0xffffff, 1.1));
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(1, 2, 1.5);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x99bbff, 0.6);
    rim.position.set(-1.5, 1.2, -1.2);
    this.scene.add(rim);

    this.lookTarget = new THREE.Object3D();
    this.scene.add(this.lookTarget);

    this._load(modelUrl);
    this._animate();
  }

  _load(modelUrl) {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    loader.load(modelUrl, (gltf) => {
      const vrm = gltf.userData.vrm;
      VRMUtils.removeUnnecessaryVertices(gltf.scene);
      VRMUtils.combineSkeletons(gltf.scene);
      vrm.scene.traverse((o) => { o.frustumCulled = false; });
      vrm.scene.rotation.y = MODEL_YAW;
      this.scene.add(vrm.scene);

      const B = (n) => vrm.humanoid.getNormalizedBoneNode(n);
      this.bones.spine = B('spine');
      this.bones.chest = B('chest') || B('upperChest');
      this.bones.head = B('head');
      this.bones.hips = B('hips');

      // Arms down at the sides — the demo posed the right arm for its IK rig.
      const lU = B('leftUpperArm'); if (lU) lU.rotation.z = 1.15;
      const rU = B('rightUpperArm'); if (rU) rU.rotation.z = -1.15;

      if (vrm.lookAt) vrm.lookAt.target = this.lookTarget;
      this.vrm = vrm;
      this.ready = true;
      this._frameHead();
    }, undefined, (err) => {
      // The model is gitignored on licence grounds, so a fresh clone has none.
      // Say so in the panel rather than leaving an unexplained empty box.
      console.error('[avatar] failed to load model', err);
      const note = document.createElement('p');
      note.className = 'avatar-missing';
      note.textContent = 'No avatar model. Add one at static/models/avatar.glb '
        + '— see static/models/README.md';
      this.container.appendChild(note);
    });
  }

  /** Frame the head and shoulders — this is a talking portrait, not a body shot. */
  _frameHead() {
    if (!this.bones.head) return;
    this._headPos = new THREE.Vector3();
    this.bones.head.getWorldPosition(this._headPos);
    this._applyFraming();
  }

  /**
   * Pull the camera back far enough that the subject fits both dimensions.
   *
   * The panel's shape is not fixed — it is 2 of 6 grid columns, so it is tall
   * and narrow on a wide screen. A fixed distance framed for a short, wide
   * panel turns into an extreme close-up once the aspect goes portrait, since
   * horizontal FOV shrinks with it. Solving for whichever axis is tighter
   * keeps the head and shoulders in shot at any shape.
   */
  _applyFraming() {
    if (!this._headPos || !this.camera) return;
    const SUBJECT_W = 0.36;   // metres of subject to keep visible across
    const SUBJECT_H = 0.44;   // ...and down
    const vFov = THREE.MathUtils.degToRad(this.camera.fov);
    const aspect = this.camera.aspect || 1;
    const hFov = 2 * Math.atan(Math.tan(vFov / 2) * aspect);
    const dist = Math.max(
      (SUBJECT_H / 2) / Math.tan(vFov / 2),
      (SUBJECT_W / 2) / Math.tan(hFov / 2),
    );
    const y = this._headPos.y - 0.05;
    this.camera.position.set(this._headPos.x, y, this._headPos.z + dist);
    this.camera.lookAt(this._headPos.x, y, this._headPos.z);
  }

  /**
   * Match the drawing buffer to the container, checked every frame.
   *
   * The AI tab is display:none at page load, so the canvas starts at zero size
   * and three.js keeps its 300x150 default — the avatar then renders blurry and
   * squashed once the tab is shown. A ResizeObserver misses this (it fires
   * before layout has settled, then never again), so poll instead: it is a
   * couple of property reads per frame and is always right.
   */
  _syncSize() {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    if (!w || !h) return false;         // hidden tab — nothing to size to yet
    if (w === this._w && h === this._h) return true;
    this._w = w; this._h = h;
    this.renderer.setSize(w, h, false); // false: CSS owns the display size
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this._applyFraming();               // re-fit: the panel's shape drives this
    return true;
  }

  // ── Speech ────────────────────────────────────────────────────────────────

  /**
   * Drive the mouth from `visemes`, timed by `clock()` in milliseconds.
   * ai.js passes the audio element's own currentTime, so playback drift can
   * never desync the mouth from the voice.
   */
  speak(visemes, clock) {
    this.visemeTrack = Array.isArray(visemes) ? visemes : [];
    this.visemeIdx = 0;
    this.clock = this.visemeTrack.length ? clock : null;
  }

  /** Stop lip-sync and let the mouth close. */
  stopSpeaking() {
    this.visemeTrack = [];
    this.visemeIdx = 0;
    this.clock = null;
  }

  _activeViseme() {
    if (!this.clock || !this.visemeTrack.length) return null;
    const t = this.clock();
    if (t === null || t === undefined) return null;
    const track = this.visemeTrack;
    // Forward cursor: O(1) per frame, rewinds only on a seek or restart.
    if (this.visemeIdx >= track.length || track[this.visemeIdx].t_ms > t) this.visemeIdx = 0;
    while (this.visemeIdx + 1 < track.length && track[this.visemeIdx + 1].t_ms <= t) this.visemeIdx++;
    const v = track[this.visemeIdx];
    return (t <= v.t_ms + v.dur_ms) ? v : null;
  }

  _updateViseme(dt) {
    const em = this.vrm.expressionManager; if (!em) return;
    const active = this._activeViseme();
    // Off the end of the track (or between utterances) the face settles back
    // slowly; while speaking, shapes are driven at articulation speed.
    const closeTau = active ? VISEME_TAU_CLOSE : VISEME_TAU_REST;
    for (const name of VISEME_NAMES) {
      // A 'sil' (closed) viseme matches no name, so all five fall to zero.
      const goal = (active && active.viseme === name)
        ? active.weight * VISEME_GAIN * VISEME_SCALE[name] : 0;
      const tau = goal > this.weights[name] ? VISEME_TAU_OPEN : closeTau;
      this.weights[name] += (goal - this.weights[name]) * (1 - Math.exp(-dt / tau));
      em.setValue(name, this.weights[name]);
    }
  }

  // ── Idle life ─────────────────────────────────────────────────────────────

  _updateBlink(dt, t) {
    const em = this.vrm.expressionManager; if (!em) return;
    if (this._blinkT === undefined) { this._blinkT = -1; this._nextBlink = 1.5; }
    if (this._blinkT < 0 && t >= this._nextBlink) this._blinkT = 0;
    let v = 0;
    if (this._blinkT >= 0) {
      const DUR = 0.14;
      this._blinkT += dt;
      v = 1 - Math.abs((this._blinkT / DUR) * 2 - 1);
      if (this._blinkT >= DUR) {
        this._blinkT = -1;
        this._nextBlink = t + (Math.random() < 0.15 ? 0.18 : 1.5 + Math.random() * 4.5);
      }
    }
    em.setValue('blink', Math.max(0, Math.min(1, v)));
  }

  _updateBreathe(t) {
    if (!this.bones.chest) return;
    const b = Math.sin(t * Math.PI * 2 * (14 / 60)) * 0.035;
    this.bones.chest.rotation.x = b;
    if (this.bones.spine) this.bones.spine.rotation.x = b * 0.5;
  }

  _updateLook(dt, t) {
    if (!this._lookGoal) {
      this._lookGoal = new THREE.Vector3(0, 1.3, 2);
      this._lookCur = new THREE.Vector3(0, 1.3, 2);
      this._nextLook = 0;
    }
    // While speaking, hold the gaze on the viewer — glancing away mid-sentence
    // reads as distracted.
    if (this.clock) {
      this._lookGoal.set(0, 1.3, 2);
    } else if (t >= this._nextLook) {
      this._lookGoal.set((Math.random() - 0.5) * 1.6, 1.2 + (Math.random() - 0.5) * 0.4, 1.8);
      this._nextLook = t + 1.5 + Math.random() * 3.5;
    }
    this._lookCur.lerp(this._lookGoal, 1 - Math.pow(0.001, dt));
    this.lookTarget.position.copy(this._lookCur);
    if (this.bones.head) {
      const yaw = THREE.MathUtils.clamp(this._lookCur.x * 0.12, -0.3, 0.3);
      const pitch = THREE.MathUtils.clamp((1.3 - this._lookCur.y) * 0.15, -0.18, 0.18);
      this.bones.head.rotation.y = THREE.MathUtils.lerp(this.bones.head.rotation.y, yaw, 0.05);
      this.bones.head.rotation.x = THREE.MathUtils.lerp(this.bones.head.rotation.x, pitch, 0.05);
    }
  }

  _updateSway(t) {
    if (!this.bones.hips) return;
    this.bones.hips.rotation.z = Math.sin(t * 0.5) * 0.02;
    this.bones.hips.position.x = Math.sin(t * 0.5) * 0.01;
  }

  _animate() {
    this._raf = requestAnimationFrame(() => this._animate());
    if (!this._clock3) this._clock3 = new THREE.Clock();
    const dt = Math.min(this._clock3.getDelta(), 0.05);
    const t = this._clock3.elapsedTime;
    // Skip rendering entirely while the tab is hidden — no point burning GPU
    // on a zero-size canvas, and it keeps the idle animation clock honest.
    if (this.vrm && this._syncSize()) {
      this._updateBreathe(t);
      this._updateSway(t);
      this._updateLook(dt, t);
      this._updateBlink(dt, t);
      this._updateViseme(dt);
      this.vrm.update(dt);
      this.renderer.render(this.scene, this.camera);
    }
  }
}

window.Avatar = new Avatar();

// Mount ourselves rather than waiting to be called: this module is deferred,
// so the classic scripts (ai.js) have already run by the time it executes and
// cannot have initialised us. ai.js only ever calls speak()/stopSpeaking(),
// both guarded, so load order stops mattering.
function mountAvatar() {
  const stage = document.getElementById('ai-avatar-stage');
  if (!stage || !stage.dataset.modelUrl) return;
  window.Avatar.init(stage, stage.dataset.modelUrl);
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountAvatar);
} else {
  mountAvatar();
}
