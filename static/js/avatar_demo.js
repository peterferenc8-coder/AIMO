/* avatar_demo.js
 * Proof-of-concept: load a VRM avatar with lifelike idle animations
 * (breathing, blinking, looking around, body sway), plus the target feature:
 * the RIGHT HAND grips a vertical cylinder and strokes up/down along it,
 * driven by a 0-100 position (the same signal a funscript feeds the device).
 *
 * The arm uses analytic 2-bone IK so the wrist stays on the cylinder while the
 * elbow/shoulder bend to follow. Grip pose, cylinder placement and elbow
 * direction are exposed in a live "Tuning" panel because those values are best
 * dialed in by eye.
 */
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';

const MODEL_URL = document.currentScript?.dataset.modelUrl
  || document.querySelector('script[data-model-url]')?.dataset.modelUrl;

const statusEl = document.getElementById('status');
const setStatus = (t) => { if (statusEl) statusEl.textContent = t; };

// Inject panel styles from JS so they can never go stale behind a cached page.
(function injectStyles() {
  const css = `
    #stage{position:fixed;inset:0}
    canvas{display:block}
    #panel,#tuning,#pose,#speak{position:fixed;z-index:10;background:rgba(20,22,28,0.9);
      backdrop-filter:blur(8px);border:1px solid #2a2d36;border-radius:12px;
      box-shadow:0 8px 30px rgba(0,0,0,0.45)}
    #panel{top:16px;left:16px;width:280px;padding:16px 18px}
    #tuning{top:16px;right:16px;width:240px;padding:14px 16px;
      max-height:calc(100vh - 32px);overflow-y:auto}
    #pose{bottom:16px;left:16px;width:280px;padding:14px 16px}
    #speak{bottom:16px;right:16px;width:300px;padding:14px 16px}
    #speak textarea{width:100%;height:64px;resize:vertical;background:#14161c;
      color:#e8e8ea;border:1px solid #2a2d36;border-radius:8px;padding:8px;
      font-size:12px;font-family:inherit;line-height:1.4}
    #speak-btn{width:100%;margin-top:8px;padding:8px;font-size:12px;cursor:pointer;
      background:#7c8cff;color:#0e0f13;border:none;border-radius:8px;font-weight:600}
    #speak-btn:disabled{opacity:0.5;cursor:default}
    #speak-status{font-size:11px;color:#9aa0ab;margin-top:8px;min-height:14px}
    #viseme-readout{font-size:10px;color:#6b7280;margin-top:4px;min-height:13px;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:-0.2px}
    #viseme-debug{margin-top:10px;padding-top:10px;border-top:1px solid #2a2d36}
    .dbg-title{font-size:11px;color:#9aa0ab;margin-bottom:6px}
    .dbg-row{display:flex;gap:4px;margin-bottom:6px}
    .dbg-btn{flex:1;padding:5px 0;font-size:11px;cursor:pointer;background:#2a2d36;
      color:#e8e8ea;border:1px solid #3a3d46;border-radius:6px}
    .dbg-btn:hover{background:#3a3f4d}
    #dbg-phonemes{width:100%;margin:6px 0 2px;background:#14161c;color:#e8e8ea;
      border:1px solid #2a2d36;border-radius:8px;padding:6px;font-size:12px}
    #panel h1,#tuning h1,#pose h1,#speak h1{font-size:14px;margin:0 0 6px;font-weight:600;cursor:pointer}
    #panel .sub,#tuning .sub{font-size:11px;color:#9aa0ab;margin:0 0 12px}
    .row{margin:10px 0}
    .row label{display:flex;justify-content:space-between;font-size:11px;color:#b8bdc7;margin-bottom:4px}
    .row label b{color:#7c8cff;font-variant-numeric:tabular-nums}
    input[type=range]{width:100%;accent-color:#7c8cff}
    .toggle{display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;
      user-select:none;margin:8px 0}
    .toggle input{accent-color:#7c8cff;width:16px;height:16px}
    #status{font-size:12px;color:#7c8cff;margin-top:10px;min-height:16px}
    .divider{height:1px;background:#2a2d36;margin:12px 0}
    .hint{font-size:11px;color:#6b7280;line-height:1.5;margin-top:6px}
    select{width:100%;background:#14161c;color:#e8e8ea;border:1px solid #2a2d36;
      border-radius:8px;padding:6px;font-size:12px}
    #log-btn{width:100%;margin-top:10px;padding:8px;font-size:12px;cursor:pointer;
      background:#7c8cff;color:#0e0f13;border:none;border-radius:8px;font-weight:600}
    .pose-btn{width:100%;margin-top:8px;padding:7px;font-size:12px;cursor:pointer;
      background:#2a2d36;color:#e8e8ea;border:1px solid #3a3d46;border-radius:8px}
    .caret{float:right;color:#9aa0ab;user-select:none;font-size:12px}
  `;
  const el = document.createElement('style');
  el.textContent = css;
  document.head.appendChild(el);
})();

// Make a panel collapse/expand when its title is clicked.
function makeCollapsible(panel, titleEl, startCollapsed = false) {
  if (!panel || !titleEl) return;
  const caret = document.createElement('span');
  caret.className = 'caret';
  titleEl.appendChild(caret);
  const bodyEls = [...panel.children].filter((el) => el !== titleEl);
  let collapsed = startCollapsed;
  const apply = () => {
    for (const el of bodyEls) el.style.display = collapsed ? 'none' : '';
    caret.textContent = collapsed ? ' ▸' : ' ▾';
  };
  titleEl.addEventListener('click', () => { collapsed = !collapsed; apply(); });
  apply();
}

// ── Lip-sync config ────────────────────────────────────────────────────────
// Declared up here because the panel builders below run at module load and
// reference them — leaving them beside updateViseme() puts them in the
// temporal dead zone and the whole module fails to initialise.
const VISEME_NAMES = ['aa', 'ih', 'ou', 'ee', 'oh'];
let VISEME_GAIN = 0.75;       // overall mouth openness (live slider)
// Per-shape trim. The VRoid morphs are not equally strong: Fcl_MTH_A and
// Fcl_MTH_U open far wider than the others at the same weight, so driving all
// five from one gain leaves "aa"/"ou" gaping while "ih"/"ee" barely register.
const VISEME_SCALE = { aa: 0.55, ih: 0.95, ou: 0.55, ee: 0.9, oh: 0.8 };
// Time constant for the mouth easing toward each new shape, in seconds. This
// MUST stay well under a typical phoneme (30-100ms) or the mouth never reaches
// its target before the next viseme replaces it — at 250ms the avatar barely
// opens at all and reads as motionless. ~25ms hits ~90% within 60ms.
const VISEME_TAU = 0.025;

// ── Tuning values (dialed in live via the right-hand panel) ────────────────
const TUNING = {
  cx: -0.015, cy: 1.36, cz: 0.315,   // cylinder position (world)
  cr: 0.022, ch: 0.30,               // cylinder radius, height
  gripR: 0.06,                       // wrist offset from cylinder axis toward shoulder
  gx: -1.62, gy: -3.14, gz: -1.32,   // hand grip orientation (world euler, radians)
  poleX: -1.55, poleY: -0.5, poleZ: -0.75, // elbow "pole" direction
  curl: -0.78,                       // finger curl amount
  thumbX: 0.0, thumbY: -0.6, thumbZ: -0.6, // thumb curl (its own axes — wraps inward)
  stroke: 0.15,                      // total vertical travel of the stroke
  strokeC: 1.36,                     // vertical center of the stroke motion
  modelYaw: Math.PI,                 // rotate model to face the camera (0 or ±π)
};

// ── Scene / renderer ───────────────────────────────────────────────────────
const stage = document.getElementById('stage');
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();

const camera = new THREE.PerspectiveCamera(30, window.innerWidth / window.innerHeight, 0.1, 20);
camera.position.set(0, 1.15, 1.15);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 1.0, 0.05);
controls.enableDamping = true;
controls.minDistance = 0.4;
controls.maxDistance = 5;
controls.update();

// Gizmo for direct joint posing (click a joint handle, drag to rotate it).
const gizmo = new TransformControls(camera, renderer.domElement);
gizmo.setMode('rotate');
gizmo.setSpace('local');
gizmo.setSize(1.5);
gizmo.addEventListener('dragging-changed', (e) => { controls.enabled = !e.value; });
scene.add(gizmo.getHelper());

// Default mouse mapping (left = orbit). In pose mode we free the left button
// for the gizmo and move orbit to the right button so drags never fight.
const ORBIT_BUTTONS = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
const POSE_BUTTONS  = { LEFT: null, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.ROTATE };

// Pose-mode state.
let poseMode = false;
let gripCaptured = false;      // true once fingers have been hand-posed
const handles = [];            // clickable joint markers
let posableBones = [];
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

scene.add(new THREE.AmbientLight(0xffffff, 1.1));
const key = new THREE.DirectionalLight(0xffffff, 1.4);
key.position.set(1, 2, 1.5);
scene.add(key);
const rim = new THREE.DirectionalLight(0x99bbff, 0.6);
rim.position.set(-1.5, 1.2, -1.2);
scene.add(rim);

const floor = new THREE.Mesh(
  new THREE.CircleGeometry(1.2, 48),
  new THREE.MeshStandardMaterial({ color: 0x14161c, roughness: 1 })
);
floor.rotation.x = -Math.PI / 2;
scene.add(floor);

// The cylinder the hand grips. Unit geometry, positioned/scaled from TUNING.
const cyl = new THREE.Mesh(
  new THREE.CylinderGeometry(1, 1, 1, 24),
  new THREE.MeshStandardMaterial({ color: 0xff5a7a, roughness: 0.4, metalness: 0.1,
    emissive: 0x551122, emissiveIntensity: 0.6 })
);
scene.add(cyl);
function updateCylinder() {
  cyl.position.set(TUNING.cx, TUNING.cy, TUNING.cz);
  cyl.scale.set(TUNING.cr, TUNING.ch, TUNING.cr);
}
updateCylinder();

// Debug marker showing where the wrist is being sent (green = IK target).
const targetMarker = new THREE.Mesh(
  new THREE.SphereGeometry(0.018, 16, 16),
  new THREE.MeshBasicMaterial({ color: 0x33ff88 })
);
scene.add(targetMarker);

// ── Idle UI ─────────────────────────────────────────────────────────────────
const ui = {
  blink:   document.getElementById('chk-blink'),
  breathe: document.getElementById('chk-breathe'),
  look:    document.getElementById('chk-look'),
  sway:    document.getElementById('chk-sway'),
  rhythm:  document.getElementById('chk-rhythm'),
  bpm:     document.getElementById('rng-bpm'),
  pos:     document.getElementById('rng-pos'),
  expr:    document.getElementById('sel-expr'),
};
document.getElementById('rng-bpm').addEventListener('input', (e) => {
  document.getElementById('bpm-val').textContent = e.target.value;
});
let manualPos = 0;
document.getElementById('rng-pos').addEventListener('input', (e) => {
  manualPos = Number(e.target.value);
  document.getElementById('pos-val').textContent = manualPos;
});

// ── Tuning panel (built in JS so we can iterate without touching HTML) ───────
buildTuningPanel();
buildPosePanel();
buildSpeakPanel();

// Click a joint handle to attach the rotation gizmo to that bone. Deferred one
// tick so that if the click actually grabbed a gizmo ring, we leave it alone.
renderer.domElement.addEventListener('pointerdown', (e) => {
  if (!poseMode) return;
  const r = renderer.domElement.getBoundingClientRect();
  pointer.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  setTimeout(() => {
    if (gizmo.dragging) return;   // grabbed a ring — don't reselect
    scene.updateMatrixWorld(true);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(handles.filter((h) => h.visible), false)[0];
    if (hit) attachToBone(hit.object.__bone);
  }, 0);
});

function buildPosePanel() {
  const wrap = document.createElement('div');
  wrap.id = 'pose';
  wrap.innerHTML = '<h1>Hand Posing</h1>'
    + '<label class="toggle"><input type="checkbox" id="chk-pose"> <b>Pose fingers</b></label>'
    + '<div class="row" id="joint-row" style="display:none">'
    + '<label>Joint</label><select id="joint-select"></select></div>'
    + '<p class="hint" id="pose-hint" style="display:none">'
    + '<b>Left-drag</b> a ring to rotate the joint. <b>Right-drag</b> to orbit the camera. '
    + 'Pick a joint below or click its dot.</p>';
  const btnExport = mkBtn('Export finger pose', () => {
    const pose = {};
    for (const b of posableBones) pose[b.__hname] = [...b.quaternion.toArray()];
    const json = JSON.stringify(pose);
    console.log('[avatar] FINGER POSE =', json);
    navigator.clipboard?.writeText(json).catch(() => {});
    setStatus('finger pose logged + copied ✓');
  });
  const btnReset = mkBtn('Reset to sliders', () => {
    gripCaptured = false;
    setStatus('fingers back to slider curl');
  });
  wrap.appendChild(btnExport);
  wrap.appendChild(btnReset);
  document.body.appendChild(wrap);
  makeCollapsible(wrap, wrap.querySelector('h1'));
  // The main "Avatar Demo" panel (from the template) is collapsible too.
  makeCollapsible(document.getElementById('panel'), document.querySelector('#panel h1'));

  document.getElementById('chk-pose').addEventListener('change', (e) => {
    poseMode = e.target.checked;
    for (const h of handles) h.visible = poseMode;
    document.getElementById('joint-row').style.display = poseMode ? '' : 'none';
    document.getElementById('pose-hint').style.display = poseMode ? '' : 'none';
    controls.mouseButtons = poseMode ? POSE_BUTTONS : ORBIT_BUTTONS;
    if (!poseMode) gizmo.detach();
    if (poseMode) { ui.rhythm.checked = false; attachToBone(posableBones[0]); }
    setStatus(poseMode ? 'pose mode — left-drag rings, right-drag to orbit' : 'ready ✓');
  });

  document.getElementById('joint-select').addEventListener('change', (e) => {
    attachToBone(posableBones[Number(e.target.value)]);
  });
}

// Friendly label for a humanoid finger-bone name, e.g. "Index · Proximal".
function jointLabel(hname) {
  const m = hname.match(/^right(Index|Middle|Ring|Little|Thumb)(.+)$/);
  return m ? `${m[1]} · ${m[2]}` : hname;
}

function populateJointSelect() {
  const sel = document.getElementById('joint-select');
  if (!sel) return;
  sel.innerHTML = '';
  posableBones.forEach((b, i) => {
    const o = document.createElement('option');
    o.value = i; o.textContent = jointLabel(b.__hname);
    sel.appendChild(o);
  });
}

// Attach the rotation gizmo to a bone and highlight its handle.
function attachToBone(bone) {
  if (!bone) return;
  for (const h of handles) h.material.color.set(0xffdd44);
  const handle = handles.find((h) => h.__bone === bone);
  if (handle) handle.material.color.set(0xff3344);
  gizmo.attach(bone);
  const sel = document.getElementById('joint-select');
  if (sel) sel.value = String(posableBones.indexOf(bone));
}
function mkBtn(label, onClick) {
  const b = document.createElement('button');
  b.className = 'pose-btn'; b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}
function buildTuningPanel() {
  const specs = [
    ['modelYaw', 'Model turn', -3.14, 3.14, 0.02],
    ['cx', 'Cyl X', -0.4, 0.4, 0.005],
    ['cy', 'Cyl Y', 0.6, 1.6, 0.005],
    ['cz', 'Cyl Z', -0.1, 0.5, 0.005],
    ['cr', 'Cyl radius', 0.005, 0.06, 0.001],
    ['ch', 'Cyl height', 0.1, 0.6, 0.01],
    ['gripR', 'Grip offset', 0.0, 0.08, 0.002],
    ['gx', 'Grip pitch', -3.14, 3.14, 0.02],
    ['gy', 'Grip yaw', -3.14, 3.14, 0.02],
    ['gz', 'Grip roll', -3.14, 3.14, 0.02],
    ['poleX', 'Elbow pole X', -2, 2, 0.05],
    ['poleY', 'Elbow pole Y', -2, 2, 0.05],
    ['poleZ', 'Elbow pole Z', -2, 2, 0.05],
    ['curl', 'Finger curl', -2, 2, 0.02],
    ['thumbX', 'Thumb X (curl)', -2, 2, 0.02],
    ['thumbY', 'Thumb Y (wrap)', -2, 2, 0.02],
    ['thumbZ', 'Thumb Z (twist)', -2, 2, 0.02],
    ['strokeC', 'Stroke center', 1.0, 1.6, 0.005],
    ['stroke', 'Stroke travel', 0.0, 0.5, 0.01],
  ];
  const wrap = document.createElement('div');
  wrap.id = 'tuning';
  wrap.innerHTML = '<h1>Tuning</h1><p class="sub">Dial in the grip, then hit “Log”</p>';
  for (const [key, label, min, max, step] of specs) {
    const row = document.createElement('div');
    row.className = 'row';
    const val = document.createElement('b');
    val.textContent = TUNING[key];
    const lab = document.createElement('label');
    lab.textContent = label + ' ';
    lab.appendChild(val);
    const inp = document.createElement('input');
    inp.type = 'range'; inp.min = min; inp.max = max; inp.step = step; inp.value = TUNING[key];
    inp.addEventListener('input', () => {
      TUNING[key] = Number(inp.value);
      val.textContent = Number(inp.value).toFixed(3);
      updateCylinder();
    });
    row.appendChild(lab); row.appendChild(inp);
    wrap.appendChild(row);
  }
  const btn = document.createElement('button');
  btn.id = 'log-btn'; btn.textContent = 'Log settings to console';
  btn.addEventListener('click', () => {
    const json = JSON.stringify(TUNING);
    console.log('[avatar] TUNING =', json);
    navigator.clipboard?.writeText(json).catch(() => {});
    setStatus('settings logged + copied ✓');
  });
  wrap.appendChild(btn);
  document.body.appendChild(wrap);
  makeCollapsible(wrap, wrap.querySelector('h1'));
}

// ── Load the VRM ─────────────────────────────────────────────────────────────
let vrm = null;
const bones = {};
let rUpper, rLower, rHand, restUpperQ, restLowerQ;
let rFingers = [], rThumb = [];
const lookTarget = new THREE.Object3D();
scene.add(lookTarget);

const loader = new GLTFLoader();
loader.register((parser) => new VRMLoaderPlugin(parser));

setStatus('loading model…');
loader.load(MODEL_URL, (gltf) => {
  vrm = gltf.userData.vrm;
  VRMUtils.removeUnnecessaryVertices(gltf.scene);
  VRMUtils.combineSkeletons(gltf.scene);
  vrm.scene.traverse((o) => { o.frustumCulled = false; });
  scene.add(vrm.scene);

  const B = (n) => vrm.humanoid.getNormalizedBoneNode(n);
  bones.spine = B('spine');
  bones.chest = B('chest') || B('upperChest');
  bones.neck  = B('neck');
  bones.head  = B('head');
  bones.hips  = B('hips');

  // Right arm — fully IK-controlled to grip the cylinder.
  rUpper = B('rightUpperArm');
  rLower = B('rightLowerArm');
  rHand  = B('rightHand');
  restUpperQ = rUpper.quaternion.clone();
  restLowerQ = rLower.quaternion.clone();

  // Left arm — rest down at the side (static).
  const lU = B('leftUpperArm'); if (lU) lU.rotation.z = 1.15;

  // Collect right-hand finger bones (with per-segment curl weights + humanoid name).
  const tag = (bone, hname, w) => { if (bone) { bone.__w = w; bone.__hname = hname; } return bone; };
  const fnames = ['Index', 'Middle', 'Ring', 'Little'];
  for (const n of fnames) {
    const p = tag(B('right' + n + 'Proximal'), 'right' + n + 'Proximal', 0.9);
    const i = tag(B('right' + n + 'Intermediate'), 'right' + n + 'Intermediate', 1.1);
    const d = tag(B('right' + n + 'Distal'), 'right' + n + 'Distal', 0.7);
    if (p) rFingers.push(p);
    if (i) rFingers.push(i);
    if (d) rFingers.push(d);
  }
  rThumb.push(...['Metacarpal', 'Proximal', 'Distal']
    .map((s) => tag(B('rightThumb' + s), 'rightThumb' + s, 0.6))
    .filter(Boolean));

  // Build clickable joint handles for every posable finger/thumb bone.
  posableBones = [...rFingers, ...rThumb];
  const handleGeo = new THREE.SphereGeometry(0.007, 12, 12);
  for (const b of posableBones) {
    const h = new THREE.Mesh(handleGeo, new THREE.MeshBasicMaterial({
      color: 0xffdd44, depthTest: false, transparent: true, opacity: 0.9,
    }));
    h.renderOrder = 999;
    h.visible = false;
    h.__bone = b;
    b.add(h);
    handles.push(h);
  }
  populateJointSelect();

  if (vrm.lookAt) vrm.lookAt.target = lookTarget;

  logExpressions();
  // Debug handle — lets you poke the avatar from the console (or a test).
  window.__avatar = {
    vrm, speak, visemeWeights,
    get track() { return visemeTrack; },
    get audio() { return speechAudio; },
  };
  setStatus('ready ✓  drag to orbit');
}, (e) => {
  if (e.total) setStatus(`loading model… ${Math.round((e.loaded / e.total) * 100)}%`);
}, (err) => {
  console.error(err);
  setStatus('failed to load model — see console');
});

function logExpressions() {
  const em = vrm.expressionManager;
  if (!em) return;
  console.log('[avatar] expressions:', em.expressions.map((x) => x.expressionName));
}

// ── 2-bone IK helpers ────────────────────────────────────────────────────────
const _A = new THREE.Vector3(), _B = new THREE.Vector3(), _C = new THREE.Vector3();
const _n = new THREE.Vector3(), _pole = new THREE.Vector3(), _elbow = new THREE.Vector3();
const _cur = new THREE.Vector3(), _des = new THREE.Vector3(), _child = new THREE.Vector3();
const _tgt = new THREE.Vector3(), _off = new THREE.Vector3();
const _q = new THREE.Quaternion(), _pq = new THREE.Quaternion();
const _euler = new THREE.Euler();

// Apply a world-space rotation to a bone (converts to the bone's local frame).
function rotateBoneWorldQ(bone, worldQ) {
  if (bone.parent) {
    bone.parent.getWorldQuaternion(_pq);
    const delta = _pq.clone().invert().multiply(worldQ).multiply(_pq);
    bone.quaternion.premultiply(delta);
  } else {
    bone.quaternion.premultiply(worldQ);
  }
  bone.updateWorldMatrix(true, false);
}

// Rotate `bone` so its `childBone` points from `fromPos` toward `goalPos`.
function aimBone(bone, childBone, fromPos, goalPos) {
  bone.updateWorldMatrix(true, false);
  childBone.getWorldPosition(_child);
  _cur.copy(_child).sub(fromPos).normalize();
  _des.copy(goalPos).sub(fromPos).normalize();
  _q.setFromUnitVectors(_cur, _des);
  rotateBoneWorldQ(bone, _q);
}

// Position the wrist at `target` using analytic 2-bone IK with a pole vector.
function solveArmIK(target) {
  rUpper.quaternion.copy(restUpperQ);
  rLower.quaternion.copy(restLowerQ);
  rUpper.updateWorldMatrix(true, true);      // refresh elbow + hand to rest

  rUpper.getWorldPosition(_A);
  rLower.getWorldPosition(_B);
  rHand.getWorldPosition(_C);
  const l1 = _A.distanceTo(_B);
  const l2 = _B.distanceTo(_C);

  _n.copy(target).sub(_A);
  let d = _n.length();
  _n.divideScalar(d || 1);
  d = Math.min(Math.max(d, Math.abs(l1 - l2) + 1e-3), l1 + l2 - 1e-3);

  // Elbow lies on a circle; find its offset along/perpendicular to the target dir.
  const a = (d * d + l1 * l1 - l2 * l2) / (2 * d);
  const h = Math.sqrt(Math.max(0, l1 * l1 - a * a));
  _pole.set(TUNING.poleX, TUNING.poleY, TUNING.poleZ);
  _pole.addScaledVector(_n, -_pole.dot(_n));  // project perpendicular to target dir
  if (_pole.lengthSq() < 1e-6) _pole.set(0, -1, 0).addScaledVector(_n, -_n.y);
  _pole.normalize();
  _elbow.copy(_A).addScaledVector(_n, a).addScaledVector(_pole, h);

  aimBone(rUpper, rLower, _A, _elbow);
  rLower.updateWorldMatrix(true, false);
  rLower.getWorldPosition(_B);
  aimBone(rLower, rHand, _B, target);
}

function setHandGrip() {
  rLower.updateWorldMatrix(true, false);
  _q.setFromEuler(_euler.set(TUNING.gx, TUNING.gy, TUNING.gz));
  if (rHand.parent) {
    rHand.parent.getWorldQuaternion(_pq);
    rHand.quaternion.copy(_pq.invert()).multiply(_q);
  } else {
    rHand.quaternion.copy(_q);
  }
  rHand.updateWorldMatrix(true, false);

  if (poseMode) {
    // Leave the finger bones to the user's gizmo; snapshot their live rotations.
    for (const b of posableBones) (b.__grip ||= new THREE.Quaternion()).copy(b.quaternion);
    gripCaptured = true;
  } else if (gripCaptured) {
    // Replay the hand-posed grip (local to the hand, so it rides the stroke).
    for (const b of posableBones) if (b.__grip) b.quaternion.copy(b.__grip);
  } else {
    for (const seg of rFingers) seg.rotation.set(0, 0, TUNING.curl * seg.__w);
    // Thumb wraps on its own axes so it can close inward around the cylinder.
    for (const seg of rThumb) {
      seg.rotation.set(TUNING.thumbX * seg.__w, TUNING.thumbY * seg.__w, TUNING.thumbZ * seg.__w);
    }
  }
}

// Drive the whole gripping arm from a 0-100 position.
let armPos = 0;
function updateGripArm(dt, t) {
  if (!rUpper) return;
  let target;
  if (ui.rhythm.checked) {
    const bpm = Number(ui.bpm.value);
    target = (Math.sin(t * (bpm / 60) * Math.PI * 2) * 0.5 + 0.5) * 100;
    document.getElementById('pos-val').textContent = Math.round(target);
  } else {
    target = manualPos;
  }
  armPos = THREE.MathUtils.lerp(armPos, target, 1 - Math.pow(0.002, dt));
  const f = armPos / 100;

  // Map position to a height on the cylinder, then offset the wrist toward the
  // shoulder so the palm sits against the cylinder rather than through it.
  const y = TUNING.strokeC - TUNING.stroke / 2 + f * TUNING.stroke;
  _tgt.set(TUNING.cx, y, TUNING.cz);
  rUpper.getWorldPosition(_A);
  _off.copy(_A).sub(_tgt); _off.y = 0;
  if (_off.lengthSq() > 1e-6) _off.normalize().multiplyScalar(TUNING.gripR); else _off.set(0, 0, TUNING.gripR);
  _tgt.add(_off);

  targetMarker.position.copy(_tgt);
  solveArmIK(_tgt);
  setHandGrip();
}

// ── Idle animation drivers ───────────────────────────────────────────────────
let nextBlink = 1.5, blinkT = -1;
function updateBlink(dt, t) {
  const em = vrm.expressionManager; if (!em) return;
  if (!ui.blink.checked) { em.setValue('blink', 0); return; }
  if (blinkT < 0 && t >= nextBlink) blinkT = 0;
  let v = 0;
  if (blinkT >= 0) {
    const DUR = 0.14;
    blinkT += dt;
    v = 1 - Math.abs((blinkT / DUR) * 2 - 1);
    if (blinkT >= DUR) { blinkT = -1; nextBlink = t + (Math.random() < 0.15 ? 0.18 : 1.5 + Math.random() * 4.5); }
  }
  em.setValue('blink', Math.max(0, Math.min(1, v)));
}

function updateBreathe(t) {
  if (!bones.chest) return;
  const on = ui.breathe.checked ? 1 : 0;
  const b = Math.sin(t * Math.PI * 2 * (14 / 60)) * 0.035 * on;
  bones.chest.rotation.x = b;
  if (bones.spine) bones.spine.rotation.x = b * 0.5;
}

let lookGoal = new THREE.Vector3(0, 1.3, 2), nextLook = 0;
const lookCur = new THREE.Vector3(0, 1.3, 2);
function updateLook(dt, t) {
  if (!ui.look.checked) {
    lookGoal.set(camera.position.x, camera.position.y, camera.position.z);
  } else if (t >= nextLook) {
    lookGoal.set((Math.random() - 0.5) * 2.2, 1.15 + (Math.random() - 0.5) * 0.5, 1.8);
    nextLook = t + 1.5 + Math.random() * 3.5;
  }
  lookCur.lerp(lookGoal, 1 - Math.pow(0.001, dt));
  lookTarget.position.copy(lookCur);
  if (bones.head) {
    const yaw = THREE.MathUtils.clamp(lookCur.x * 0.12, -0.35, 0.35);
    const pitch = THREE.MathUtils.clamp((1.3 - lookCur.y) * 0.15, -0.2, 0.2);
    bones.head.rotation.y = THREE.MathUtils.lerp(bones.head.rotation.y, yaw, 0.05);
    bones.head.rotation.x = THREE.MathUtils.lerp(bones.head.rotation.x, pitch, 0.05);
  }
}

function updateSway(t) {
  if (!bones.hips) return;
  const on = ui.sway.checked ? 1 : 0;
  bones.hips.rotation.z = Math.sin(t * 0.5) * 0.02 * on;
  bones.hips.position.x = Math.sin(t * 0.5) * 0.01 * on;
}

let curExpr = 'neutral';
ui.expr.addEventListener('change', (e) => {
  curExpr = e.target.value;
  document.getElementById('expr-name').textContent = curExpr;
});
const EXPR_PRESETS = ['happy', 'angry', 'sad', 'relaxed', 'surprised'];
function updateExpression() {
  const em = vrm.expressionManager; if (!em) return;
  for (const name of EXPR_PRESETS) {
    const target = (name === curExpr) ? 0.85 : 0;
    em.setValue(name, THREE.MathUtils.lerp(em.getValue(name) ?? 0, target, 0.1));
  }
}

// ── Lip-sync ─────────────────────────────────────────────────────────────────
// The server (tts.py) returns a viseme track alongside the audio: a list of
// {t_ms, dur_ms, viseme, weight} derived from Kokoro's per-token timings and
// misaki's phonemes. We drive the VRM's aa/ih/ou/ee/oh expressions off the
// audio element's own clock, so playback drift can never desync the mouth.
let visemeTrack = [];
let lastTrack = [];      // kept after playback so debug can replay it silently
let visemeIdx = 0;
let speechAudio = null;
const visemeWeights = Object.fromEntries(VISEME_NAMES.map((n) => [n, 0]));

// Debug override. Anything that wants to drive the mouth by hand MUST go
// through here rather than calling em.setValue() from outside, because this
// function rewrites all five visemes every frame and would overwrite it.
//   null                      -- normal: follow the speech audio
//   {viseme, weight}          -- hold one shape open indefinitely
//   {track, t0, label}        -- play a viseme track on the wall clock, no audio
let debugDrive = null;

// Which viseme (if any) should be showing right now, and from which source.
function activeViseme() {
  if (debugDrive && debugDrive.viseme) return debugDrive;

  let track = null;
  let tms = 0;
  if (debugDrive && debugDrive.track) {
    track = debugDrive.track;
    tms = performance.now() - debugDrive.t0;
  } else if (speechAudio && !speechAudio.paused && visemeTrack.length) {
    track = visemeTrack;
    tms = speechAudio.currentTime * 1000;
  }
  if (!track || !track.length) return null;

  // Forward cursor keeps this O(1) per frame; rewinds only on a seek/restart.
  if (visemeIdx >= track.length || track[visemeIdx].t_ms > tms) visemeIdx = 0;
  while (visemeIdx + 1 < track.length && track[visemeIdx + 1].t_ms <= tms) visemeIdx++;
  const v = track[visemeIdx];
  return (tms <= v.t_ms + v.dur_ms) ? v : null;
}

function updateViseme(dt) {
  const em = vrm.expressionManager; if (!em) return;

  const active = activeViseme();

  // Everything eases toward its goal, so consecutive visemes blend instead of
  // popping — this is also what makes the even-split interpolation read well.
  const k = 1 - Math.exp(-dt / VISEME_TAU);
  for (const name of VISEME_NAMES) {
    // A 'sil' (closed) viseme matches no name, so all five fall to zero.
    const goal = (active && active.viseme === name)
      ? active.weight * VISEME_GAIN * VISEME_SCALE[name] : 0;
    visemeWeights[name] += (goal - visemeWeights[name]) * k;
    em.setValue(name, visemeWeights[name]);
  }

  // Live readout: separates "audio never started" from "morphs not applying".
  // Also reports the real morph influence actually on the mesh after
  // vrm.update() -- if `w` moves but `morph` stays 0, the expression is not
  // bound to the model and the problem is the VRM, not the lip-sync.
  const out = document.getElementById('viseme-readout');
  if (out) {
    let src = 'idle';
    if (debugDrive && debugDrive.viseme) src = 'hold';
    else if (debugDrive && debugDrive.track) src = debugDrive.label || 'track';
    else if (speechAudio) src = `audio ${speechAudio.currentTime.toFixed(2)}s`;
    const w = Math.max(...VISEME_NAMES.map((n) => visemeWeights[n]));
    out.textContent = `${src} | ${active ? active.viseme : '·'} `
      + `| w ${w.toFixed(2)} | morph ${peakMouthMorph().toFixed(2)}`;
  }
}

// Highest Fcl_MTH_* influence currently applied to the mesh (ground truth).
function peakMouthMorph() {
  let peak = 0;
  vrm.scene.traverse((m) => {
    if (!m.isMesh || !m.morphTargetInfluences || !m.morphTargetDictionary) return;
    for (const [name, i] of Object.entries(m.morphTargetDictionary)) {
      if (name.startsWith('Fcl_MTH')) peak = Math.max(peak, m.morphTargetInfluences[i]);
    }
  });
  return peak;
}

function stopSpeaking() {
  if (speechAudio) {
    speechAudio.pause();
    speechAudio.currentTime = 0;
  }
  speechAudio = null;
  visemeTrack = [];
  visemeIdx = 0;
}

// A 1-frame silent WAV, used purely to unlock audio playback (see speak()).
const SILENT_WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEA'
  + 'RKwAAIhYAQACABAAZGF0YQAAAAA=';

async function speak(text) {
  const btn = document.getElementById('speak-btn');
  const statusEl = document.getElementById('speak-status');
  const say = (t, bad) => {
    statusEl.textContent = t;
    statusEl.style.color = bad ? '#ff6b81' : '#9aa0ab';
  };

  stopSpeaking();
  debugDrive = null;      // a held debug viseme would mask the real speech
  btn.disabled = true;
  say('synthesising…');

  // Claim audio permission NOW, while the click's user gesture is still valid.
  // Kokoro's first call loads the model and can take far longer than the
  // browser's ~5s transient-activation window, so an <audio> created after the
  // fetch resolves gets refused by the autoplay policy. Playing a silent clip
  // up front unlocks this element; later play() calls on it are then allowed.
  const audio = new Audio(SILENT_WAV);
  let unlocked = true;
  try {
    await audio.play();
  } catch {
    unlocked = false;   // not fatal — report it only if the real play() fails
  }

  try {
    const res = await fetch('/api/tts/synthesize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || `HTTP ${res.status}`);

    visemeTrack = data.visemes || [];
    lastTrack = visemeTrack;
    visemeIdx = 0;
    if (!visemeTrack.length) say('warning: server returned no visemes', true);

    audio.src = data.audio_url;
    audio.currentTime = 0;
    audio.addEventListener('ended', () => say('done ✓'));
    speechAudio = audio;

    try {
      await audio.play();
    } catch (err) {
      throw new Error(unlocked
        ? `playback refused (${err.name})`
        : 'playback blocked by autoplay policy — click the page, then Speak again');
    }
    say(`speaking — ${visemeTrack.length} visemes / ${data.duration_ms} ms`);
  } catch (err) {
    console.error('[avatar] TTS failed', err);
    say(`failed: ${err.message}`, true);
    stopSpeaking();
  } finally {
    btn.disabled = false;
  }
}

function buildSpeakPanel() {
  const wrap = document.createElement('div');
  wrap.id = 'speak';
  wrap.innerHTML = '<h1>Speech</h1>'
    + '<textarea id="speak-text">Hello there! I can move my mouth in time '
    + 'with what I am saying now.</textarea>'
    + '<div id="speak-status"></div>'
    + '<div id="viseme-readout"></div>';
  const btn = mkBtn('Speak', () => {
    const text = document.getElementById('speak-text').value.trim();
    if (text) speak(text);
  });
  btn.id = 'speak-btn';
  btn.className = '';
  wrap.insertBefore(btn, wrap.querySelector('#speak-status'));

  // The default camera frames the whole body, which leaves the mouth a few
  // pixels tall — far too small to judge lip-sync by.
  wrap.appendChild(mkBtn('Focus face', () => {
    if (!bones.head) return;
    const head = new THREE.Vector3();
    bones.head.getWorldPosition(head);
    controls.target.copy(head);
    camera.position.set(head.x, head.y + 0.02, head.z + 0.45);
    controls.update();
  }));

  // Mouth openness is model-specific and best judged by eye, so expose it.
  const gainRow = document.createElement('div');
  gainRow.className = 'row';
  const gainVal = document.createElement('b');
  gainVal.textContent = VISEME_GAIN.toFixed(2);
  const gainLab = document.createElement('label');
  gainLab.textContent = 'Mouth openness (speech) ';
  gainLab.appendChild(gainVal);
  const gainInp = document.createElement('input');
  gainInp.type = 'range';
  gainInp.min = 0; gainInp.max = 1.5; gainInp.step = 0.05;
  gainInp.value = VISEME_GAIN;
  gainInp.addEventListener('input', () => {
    VISEME_GAIN = Number(gainInp.value);
    gainVal.textContent = VISEME_GAIN.toFixed(2);
  });
  gainRow.appendChild(gainLab);
  gainRow.appendChild(gainInp);
  wrap.appendChild(gainRow);

  buildVisemeDebug(wrap);
  document.body.appendChild(wrap);
  makeCollapsible(wrap, wrap.querySelector('h1'));
}

// ── Lip-sync debug ───────────────────────────────────────────────────────────
// Three probes, each isolating one layer:
//   1. Hold buttons  -- can the model show this viseme at all?  (morph binding)
//   2. Phonemes      -- does a typed phoneme string drive the mouth?  (mapping
//                       + timing + blending, using the server's real map)
//   3. Replay silent -- does the LAST real TTS track drive the mouth with the
//                       audio element taken out of the picture?
// If 1 and 2 work but 3 does not, the fault is the audio clock, not the mouth.
function buildVisemeDebug(wrap) {
  const box = document.createElement('div');
  box.id = 'viseme-debug';
  box.innerHTML = '<div class="dbg-title">Lip-sync debug</div>';

  const row = document.createElement('div');
  row.className = 'dbg-row';
  for (const name of [...VISEME_NAMES, 'sil']) {
    const b = document.createElement('button');
    b.className = 'dbg-btn';
    b.textContent = name;
    b.addEventListener('click', () => {
      debugDrive = { viseme: name, weight: Number(slider.value) };
      say(`holding "${name}" at ${Number(slider.value).toFixed(2)}`);
    });
    row.appendChild(b);
  }
  box.appendChild(row);

  // Labelled explicitly: this only scales a *held* debug viseme. It has no
  // effect on speech — that is the "Mouth openness" slider above.
  const sliderRow = document.createElement('div');
  sliderRow.className = 'row';
  const sliderVal = document.createElement('b');
  sliderVal.textContent = '1.00';
  const sliderLab = document.createElement('label');
  sliderLab.textContent = 'Hold weight (debug only) ';
  sliderLab.appendChild(sliderVal);
  const slider = document.createElement('input');
  slider.type = 'range'; slider.min = 0; slider.max = 1; slider.step = 0.05; slider.value = 1;
  slider.addEventListener('input', () => {
    sliderVal.textContent = Number(slider.value).toFixed(2);
    if (debugDrive && debugDrive.viseme) debugDrive.weight = Number(slider.value);
  });
  sliderRow.appendChild(sliderLab);
  sliderRow.appendChild(slider);
  box.appendChild(sliderRow);

  const phon = document.createElement('input');
  phon.type = 'text';
  phon.id = 'dbg-phonemes';
  phon.value = 'həlˈO ðˈɛɹ';
  phon.placeholder = 'IPA phonemes, or aa ih ou ee oh';
  box.appendChild(phon);

  const say = (t) => { document.getElementById('speak-status').textContent = t; };

  box.appendChild(mkBtn('Play phonemes (no audio)', async () => {
    const track = await phonemesToTrack(phon.value);
    if (!track.length) { say('no recognisable phonemes'); return; }
    visemeIdx = 0;
    debugDrive = { track, t0: performance.now(), label: 'phonemes' };
    say(`playing ${track.length} visemes from phonemes`);
  }));

  box.appendChild(mkBtn('Replay last speech (no audio)', () => {
    if (!lastTrack.length) { say('speak something first'); return; }
    visemeIdx = 0;
    debugDrive = { track: lastTrack, t0: performance.now(), label: 'replay' };
    say(`replaying ${lastTrack.length} visemes, silently`);
  }));

  box.appendChild(mkBtn('Release / back to audio', () => {
    debugDrive = null;
    say('debug released');
  }));

  wrap.appendChild(box);
}

// Turn a phoneme string into a viseme track using the SERVER's mapping, so
// this tests the real table rather than a drifting copy of it.
let _visemeMap = null;
const DEBUG_PHONEME_MS = 140;

async function phonemesToTrack(text) {
  if (!_visemeMap) {
    try {
      const res = await fetch('/api/tts/viseme_map');
      _visemeMap = (await res.json()).map || {};
    } catch (err) {
      console.error('[avatar] could not load viseme map', err);
      _visemeMap = {};
    }
  }
  const track = [];
  let t = 0;
  for (const ch of text) {
    if (ch === ' ' || ch === 'ˈ' || ch === 'ˌ' || ch === 'ː') continue;
    const v = _visemeMap[ch];
    if (!v) continue;
    track.push({ t_ms: t, dur_ms: DEBUG_PHONEME_MS, viseme: v, weight: 1 });
    t += DEBUG_PHONEME_MS;
  }
  // Allow typing viseme names directly, e.g. "aa ih ou".
  if (!track.length) {
    for (const word of text.trim().split(/\s+/)) {
      if ([...VISEME_NAMES, 'sil'].includes(word)) {
        track.push({ t_ms: t, dur_ms: DEBUG_PHONEME_MS, viseme: word, weight: 1 });
        t += DEBUG_PHONEME_MS;
      }
    }
  }
  return track;
  document.body.appendChild(wrap);
  makeCollapsible(wrap, wrap.querySelector('h1'));
}

// ── Main loop ────────────────────────────────────────────────────────────────
const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 0.05);
  const t = clock.elapsedTime;
  if (vrm) {
    vrm.scene.rotation.y = TUNING.modelYaw;   // face the camera (live-tunable)
    updateBreathe(t);
    updateSway(t);
    updateLook(dt, t);
    updateExpression();
    updateBlink(dt, t);
    updateViseme(dt);
    updateGripArm(dt, t);   // arm IK last, after torso/hips have moved
    vrm.update(dt);
  }
  controls.update();
  renderer.render(scene, camera);
}
animate();

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});
