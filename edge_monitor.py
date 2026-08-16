"""
edge_monitor.py
---------------
Live session UI: girth (serial) + heart rate (BLE, RR-interval based) + a
0-10 arousal dial you type on the numpad, with the edge detector running
causally on top and drawing its prediction while the session happens.

What this adds over sensor_fusion_poc.py
----------------------------------------
* RR intervals instead of integer BPM. The strap's Heart Rate Measurement
  characteristic carries beat-to-beat RR at 1/1024 s (~0.98 ms) resolution when
  flags bit 4 is set. Integer BPM quantises to ~6 ms at 100 bpm and ~17 ms at
  60 bpm, which is what made the old HR trigger fire on quantisation steps
  rather than on physiology. RR beats are also back-dated from the packet
  arrival time using the cumulative interval, so a batched packet doesn't
  stack several beats on one timestamp.
* A 0-10 subjective arousal channel. Keys 0-9, and '+' or '.' for 10. This is
  the highest-value addition for modelling: it turns each on-period from one
  binary event into a continuously labelled trace.
* The edge detector, live. Same rule as the offline analysis (CFG_B): causal
  trailing-mean derivative over a 1.5 s span, fire when it falls to 50% of its
  running peak since the resume and stays there 1.0 s, no earlier than 3 s in.
  It draws a line where it thinks you're about to press, then reports the error
  once you actually press.
* No pynput. Keys are read from the plot window via matplotlib's own event
  loop, so the window just needs focus.

The CSV schema is unchanged, so every offline analysis script still works. New
rows simply use new channel names:
    girth, hr          - as before (hr = the BPM byte the strap reports)
    hr_inst            - 60000/RR, the RR-derived instantaneous rate
    rr                 - one row per beat, value = RR interval in ms
    arousal            - value = 0..10, whenever you press a digit
    event              - value = edge | calm  (unchanged)
    prediction         - value = girth | hr, logged the moment the detector fires

Usage:
    python edge_monitor.py --demo                        # no hardware
    python edge_monitor.py --serial-port /dev/ttyACM0    # real session
    python edge_monitor.py --replay logs/sensor_fusion/session_20260809_111808.csv

Keys (with the plot window focused):
    SPACE   edge  - on the edge, stimulation stops
    S       calm  - calmed down, resume
    0-9     arousal level 0-9      '+' or '.'  arousal level 10
    Q       quit
"""

import argparse
import asyncio
import csv
import os
import queue
import random
import re
import threading
import time
from collections import deque
from datetime import datetime

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

FRAME_RE = re.compile(
    r"Raw Frame:\s*(0x[0-9A-Fa-f]+)\s*\|\s*Position:\s*(-?\d+)\s*\|\s*Status:\s*(0x[0-9A-Fa-f]+)"
)
VALID_STATUS = "0x30"

WINDOW_SECONDS = 90
PLOT_INTERVAL_MS = 200
TICK_HZ = 10.0
DT = 1.0 / TICK_HZ

# Detector constants -- CFG_B from the offline analysis. Do not change these
# without re-running the offline sweep; they sit on a sharp trade-off curve
# between firing early and firing late.
SPAN = 1.5      # seconds of trailing mean, and the derivative's lag
ROLL = 0.5      # fire when the derivative falls to this fraction of its peak
K_MAD = 1.0     # the peak must have exceeded K_MAD * MAD(derivative) to count
MIN_ON = 3.0    # never fire less than this long after a resume
HOLD = 1.0      # the rollover must be sustained this long
WARMUP = 30.0   # seconds of history before the MAD scale is trustworthy

GIRTH_C = "#2a78d6"
HR_C = "#eb6834"
PRED_C = "#1baf7a"
EDGE_C = "#c8383a"
CALM_C = "#4a4a46"


class CsvLogger:
    """Appends samples from every source to one CSV via a writer thread, so
    producer threads never block on file I/O."""

    def __init__(self, path):
        self.path = path
        self._queue = queue.Queue()
        self._stop = threading.Event()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._file = open(path, "w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(
            ["wall_clock", "t", "channel", "value", "raw_value", "status", "raw_frame"]
        )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def log(self, t, channel, value, raw_value=None, status="ok", raw_frame=""):
        self._queue.put((
            datetime.now().isoformat(timespec="milliseconds"),
            f"{t:.4f}", channel, value,
            raw_value if raw_value is not None else value, status, raw_frame,
        ))

    def _run(self):
        while not self._stop.is_set() or not self._queue.empty():
            try:
                row = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            self._writer.writerow(row)
            self._file.flush()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._file.close()


class SharedStream:
    """Ring buffer of (t, value) for a live plot. CPython's GIL makes the
    append/iterate race benign here -- worst case is one torn read of the
    newest point on a redraw."""

    def __init__(self, maxlen=20000, channel=None, logger=None):
        self.points = deque(maxlen=maxlen)
        self.rejected = deque(maxlen=500)
        self.channel = channel
        self.logger = logger

    def add(self, t, value, status="ok", raw_frame="", raw_value=None):
        self.points.append((t, value))
        if self.logger:
            self.logger.log(t, self.channel, value, raw_value, status, raw_frame)

    def add_rejected(self, t, value, status="rejected", raw_frame="", raw_value=None):
        self.rejected.append((t, value))
        if self.logger:
            self.logger.log(t, self.channel, value, raw_value, status, raw_frame)

    def at(self, t):
        """Linear interpolation at time t, or None if there's nothing to use.
        Never extrapolates forward past the newest sample by more than 2 s --
        a stalled sensor should read as missing, not as a flat line."""
        pts = list(self.points)
        if len(pts) < 2:
            return None
        if t > pts[-1][0] + 2.0:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return float(np.interp(t, xs, ys))

    def window(self, t0, seconds):
        return [(t, v) for t, v in self.points if t >= t0 - seconds]

    def window_rejected(self, t0, seconds):
        return [(t, v) for t, v in self.rejected if t >= t0 - seconds]


class MarkerStore:
    """Ground truth the user types: Space = 'on the edge, stop', S = 'calmed
    down, resume'. Nothing is actuated here -- these are timestamps."""

    def __init__(self, logger=None):
        self.marks = deque(maxlen=2000)
        self.logger = logger

    def add(self, t, label):
        self.marks.append((t, label))
        if self.logger:
            self.logger.log(t, "event", label)

    def window(self, t0, seconds):
        return [(t, l) for t, l in self.marks if t >= t0 - seconds]


class ArousalStore:
    """The 0-10 dial. Held as a step function: the level stands until changed."""

    def __init__(self, logger=None):
        self.points = deque(maxlen=2000)
        self.logger = logger

    def add(self, t, level):
        self.points.append((t, level))
        if self.logger:
            self.logger.log(t, "arousal", level)

    @property
    def current(self):
        return self.points[-1][1] if self.points else None

    def step_trace(self, t_now, seconds):
        """(xs, ys) drawn as a staircase over the visible window."""
        pts = [p for p in self.points if p[0] >= t_now - seconds - 600]
        if not pts:
            return [], []
        xs, ys = [], []
        for i, (t, v) in enumerate(pts):
            t_end = pts[i + 1][0] if i + 1 < len(pts) else t_now
            xs += [t, t_end]
            ys += [v, v]
        return xs, ys


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------

class EdgeDetector:
    """The offline CFG_B rule, run causally on a 10 Hz grid.

    Girth and HR are each smoothed with a trailing mean over SPAN seconds; the
    derivative is (mean_now - mean_SPAN_ago) / SPAN. Within an on-period the
    running peak of each derivative is tracked from the resume, and the channel
    fires when its derivative has sat at or below ROLL x that peak for HOLD
    seconds -- no earlier than MIN_ON after the resume.

    One difference from the offline version, stated so it isn't mistaken for a
    bug: offline the MAD scale is computed over the whole session, which is not
    causal. Here it is computed over history so far, and the detector stays
    disarmed for the first WARMUP seconds while that estimate settles.
    """

    def __init__(self, on_fire=None):
        self.grid_t = []
        self.raw = {"girth": [], "hr": []}
        self.smooth = {"girth": [], "hr": []}
        self.deriv = {"girth": [], "hr": []}
        self.k = max(1, int(round(SPAN / DT)))
        self.on_fire = on_fire
        self.reset_episode(0.0)
        self.fires = []            # (t_fire, channel)
        self.episode_start = 0.0

    # -- episode state -------------------------------------------------
    def reset_episode(self, t):
        self.episode_start = t
        self.peak = {"girth": 0.0, "hr": 0.0}
        self.held = {"girth": 0.0, "hr": 0.0}
        self.fired = False
        self.status = {"girth": (0.0, 0.0), "hr": (0.0, 0.0)}  # (ratio, held)

    def mark(self, t, label):
        if label == "calm":
            self.reset_episode(t)
        elif label == "edge":
            self.reset_episode(t)   # disarmed until the next resume anyway

    # -- per-tick ------------------------------------------------------
    def tick(self, t, girth, hr):
        self.grid_t.append(t)
        for ch, v in (("girth", girth), ("hr", hr)):
            series = self.raw[ch]
            series.append(v if v is not None else (series[-1] if series else None))
            self._update_channel(ch)
        if len(self.grid_t) > 40000:                     # ~66 min of history
            self._trim()
        return self._evaluate(t)

    def _update_channel(self, ch):
        raw, sm, dv = self.raw[ch], self.smooth[ch], self.deriv[ch]
        vals = [x for x in raw[-self.k:] if x is not None]
        sm.append(float(np.mean(vals)) if vals else None)
        if len(sm) > self.k and sm[-1] is not None and sm[-1 - self.k] is not None:
            dv.append((sm[-1] - sm[-1 - self.k]) / SPAN)
        else:
            dv.append(None)

    def _trim(self):
        n = 20000
        self.grid_t = self.grid_t[-n:]
        for d in (self.raw, self.smooth, self.deriv):
            for ch in d:
                d[ch] = d[ch][-n:]

    def scale(self, ch):
        """MAD of the derivative over history so far, as a robust sd."""
        d = [x for x in self.deriv[ch] if x is not None]
        if len(d) < int(WARMUP / DT):
            return None
        a = np.asarray(d[-18000:])
        return float(1.4826 * np.median(np.abs(a - np.median(a)))) + 1e-9

    def _evaluate(self, t):
        elapsed = t - self.episode_start
        for ch in ("girth", "hr"):
            d = self.deriv[ch][-1] if self.deriv[ch] else None
            if d is None:
                continue
            self.peak[ch] = max(self.peak[ch], d)
            s = self.scale(ch)
            pk = self.peak[ch]
            ratio = d / pk if pk > 1e-9 else 1.0
            armed = s is not None and pk >= K_MAD * s and t >= WARMUP
            ok = armed and elapsed >= MIN_ON and d <= ROLL * pk
            self.held[ch] = self.held[ch] + DT if ok else 0.0
            self.status[ch] = (ratio, self.held[ch])
            if not self.fired and ok and self.held[ch] > HOLD - 1e-9:
                self.fired = True
                self.fires.append((t, ch))
                if self.on_fire:
                    self.on_fire(t, ch)
                return ch
        return None


# ---------------------------------------------------------------------------
# Heart rate (BLE) -- full Heart Rate Measurement parse, RR included
# ---------------------------------------------------------------------------

def parse_hr_measurement(data):
    """Returns (bpm, [rr_ms, ...]) per the BLE Heart Rate Measurement spec."""
    flags = data[0]
    offset = 1
    if flags & 0x01:
        bpm = int.from_bytes(data[offset:offset + 2], "little")
        offset += 2
    else:
        bpm = data[offset]
        offset += 1
    if flags & 0x08:                      # energy expended field present
        offset += 2
    rr = []
    if flags & 0x10:
        while offset + 1 < len(data):
            raw = int.from_bytes(data[offset:offset + 2], "little")
            offset += 2
            rr.append(raw / 1024.0 * 1000.0)
    return bpm, rr


def hr_notification_handler(hr_stream, inst_stream, rr_stream, start_time, state):
    def handler(_sender, data):
        now = time.time() - start_time
        bpm, rr = parse_hr_measurement(data)
        hr_stream.add(now, bpm)
        if not rr:
            if not state["warned"]:
                print("[HR] Strap is not sending RR intervals -- falling back to "
                      "integer BPM. Check that RR/HRV is enabled on the strap.")
                state["warned"] = True
            return
        state["rr_seen"] = True
        # Beats in one packet happened before it arrived: back-date them so a
        # batched packet doesn't stack several beats on the same timestamp.
        total = sum(rr) / 1000.0
        t_beat = now - total
        for interval in rr:
            t_beat += interval / 1000.0
            rr_stream.add(t_beat, round(interval, 2))
            inst_stream.add(t_beat, round(60000.0 / interval, 3))
    return handler


async def run_hr_ble(hr_stream, inst_stream, rr_stream, start_time, stop_event, state):
    from bleak import BleakScanner, BleakClient

    print("[HR] Scanning for heart rate monitor...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: HR_SERVICE_UUID in ad.service_uuids
    )
    if not device:
        print("[HR] No HRM found. Is it powered on and not connected elsewhere?")
        return
    print(f"[HR] Connecting to {device.name or 'Unknown'} ({device.address})...")
    async with BleakClient(device) as client:
        await client.start_notify(
            HR_MEASUREMENT_UUID,
            hr_notification_handler(hr_stream, inst_stream, rr_stream, start_time, state),
        )
        print("[HR] Streaming.")
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
        await client.stop_notify(HR_MEASUREMENT_UUID)


def hr_thread_main(hr_stream, inst_stream, rr_stream, start_time, stop_event, state):
    try:
        asyncio.run(run_hr_ble(hr_stream, inst_stream, rr_stream, start_time, stop_event, state))
    except Exception as exc:
        print(f"[HR] thread stopped: {exc}")


def hr_demo_thread_main(hr_stream, inst_stream, rr_stream, start_time, stop_event, state):
    """Synthetic beats, emitted as RR intervals so the RR path is exercised."""
    state["rr_seen"] = True
    bpm = 68.0
    ramping = False
    t_next = time.time() + random.uniform(8, 15)
    while not stop_event.is_set():
        now = time.time()
        if not ramping and now >= t_next:
            ramping = True
        if ramping:
            bpm += random.uniform(0.4, 1.4)
            if bpm > 132 or random.random() < 0.02:
                ramping, t_next = False, now + random.uniform(10, 20)
        else:
            bpm = max(58.0, bpm - random.uniform(0.0, 0.7))
        rr = 60000.0 / (bpm + random.uniform(-2.5, 2.5))
        t = now - start_time
        rr_stream.add(t, round(rr, 2))
        inst_stream.add(t, round(60000.0 / rr, 3))
        hr_stream.add(t, round(60000.0 / rr))
        time.sleep(rr / 1000.0)


# ---------------------------------------------------------------------------
# Girth sensor (serial)
# ---------------------------------------------------------------------------

def girth_thread_main(stream, start_time, stop_event, port, baud, um_per_count):
    import serial

    try:
        ser = serial.Serial(port, baud, timeout=1)
    except Exception as exc:
        print(f"[Girth] Could not open {port}: {exc}")
        return
    print(f"[Girth] Reading {port} @ {baud} baud.")
    with ser:
        while not stop_event.is_set():
            try:
                raw_line = ser.readline()
            except Exception as exc:
                print(f"[Girth] read error: {exc}")
                break
            if not raw_line:
                continue
            m = FRAME_RE.search(raw_line.decode("ascii", errors="ignore").strip())
            if not m:
                continue
            raw_frame, position_str, status = m.groups()
            position = int(position_str)
            value = position * um_per_count if um_per_count else position
            t = time.time() - start_time
            if status == VALID_STATUS:
                stream.add(t, value, status, raw_frame, position)
            else:
                stream.add_rejected(t, value, status, raw_frame, position)


def girth_demo_thread_main(stream, start_time, stop_event, um_per_count, marker_store):
    """Synthetic girth that actually responds to the markers, so the detector
    can be exercised end to end: it climbs and rolls over after a 'calm', and
    decays after an 'edge'."""
    position = 2600.0
    target = 2600.0
    while not stop_event.is_set():
        now = time.time()
        t = now - start_time
        last = marker_store.marks[-1][1] if marker_store.marks else "calm"
        since = t - (marker_store.marks[-1][0] if marker_store.marks else 0.0)
        if last == "calm":
            target = 2600.0 + 900.0 * (1.0 - np.exp(-since / 6.0))
        else:
            target = 2600.0 + (target - 2600.0) * 0.97
        position += (target - position) * 0.05 + random.uniform(-9, 9)
        value = position * um_per_count if um_per_count else position
        if random.random() < 0.02:
            stream.add_rejected(t, value, "0x20", "", position)
        else:
            stream.add(t, value, VALID_STATUS, "", position)
        time.sleep(1 / 8.9)


# ---------------------------------------------------------------------------
# Replay -- runs the live detector over a recorded CSV, headless
# ---------------------------------------------------------------------------

def replay(path):
    rows = list(csv.DictReader(open(path)))

    def series(ch):
        r = [x for x in rows if x["channel"] == ch and x["status"] != "rejected"]
        return ([float(x["t"]) for x in r], [float(x["value"]) for x in r])

    gt, gv = series("girth")
    ht, hv = series("hr_inst")
    if not ht:
        ht, hv = series("hr")
        print("[Replay] No hr_inst channel in this log -- using integer BPM.")
    events = [(float(x["t"]), x["value"]) for x in rows if x["channel"] == "event"]
    if not gt or not events:
        print("[Replay] Nothing to replay.")
        return

    det = EdgeDetector()
    t0, t1 = events[0][0], gt[-1]
    ev = list(events)
    # The detector is only reset on a resume, never on the press, so a fire that
    # lands after the press is recorded as a late fire rather than disappearing.
    # That matches how the offline evaluation scores it.
    t = t0
    while t < t1:
        while ev and ev[0][0] <= t:
            te, label = ev.pop(0)
            if label == "calm":
                det.reset_episode(te)
        g = float(np.interp(t, gt, gv)) if gt[0] <= t <= gt[-1] else None
        h = float(np.interp(t, ht, hv)) if ht and ht[0] <= t <= ht[-1] else None
        det.tick(t, g, h)
        t += DT

    pairs = [(a, b) for (a, la), (b, lb) in zip(events, events[1:])
             if la == "calm" and lb == "edge"]
    print(f"\n{os.path.basename(path)}   [{len(pairs)} on-periods]")
    print(f"  {'resume':>8}{'press':>9}{'on-time':>9}{'fired':>9}{'delta':>9}  channel")
    deltas = []
    for t_res, te in pairs:
        hit = next(((tf, ch) for tf, ch in det.fires if tf >= t_res), None)
        if hit is None:
            print(f"  {t_res:8.1f}{te:9.1f}{te-t_res:9.1f}{'never':>9}{'-':>9}")
            continue
        tf, ch = hit
        deltas.append(tf - te)
        flag = "  LATE" if tf > te else ""
        print(f"  {t_res:8.1f}{te:9.1f}{te-t_res:9.1f}{tf:9.1f}{tf-te:+9.1f}  {ch}{flag}")
    if deltas:
        a = np.array(deltas)
        print(f"  n={len(a)}  late={int((a > 0).sum())}  mean {a.mean():+.2f}s  "
              f"sd {a.std(ddof=1):.2f}s  earliest {a.min():+.1f}s  worst late {a.max():+.1f}s")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

AROUSAL_KEYS = {str(i): i for i in range(10)}
AROUSAL_KEYS.update({"+": 10, ".": 10, "add": 10, "decimal": 10})


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--demo", action="store_true", help="synthesize every stream, no hardware")
    p.add_argument("--serial-port", default=None, help="girth sensor port, e.g. /dev/ttyACM0")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--um-per-count", type=float, default=None,
                   help="convert raw Position counts to mm (device spec: 0.002)")
    p.add_argument("--window", type=float, default=WINDOW_SECONDS,
                   help="seconds of history on screen")
    p.add_argument("--log-file", default=None)
    p.add_argument("--no-log", action="store_true")
    p.add_argument("--no-predict", action="store_true",
                   help="log the session but don't run or draw the detector")
    p.add_argument("--replay", default=None,
                   help="run the live detector over a recorded CSV and exit")
    args = p.parse_args()

    if args.replay:
        replay(args.replay)
        return
    if not args.demo and not args.serial_port:
        p.error("--serial-port is required unless --demo is set")

    logger = None
    if not args.no_log:
        path = args.log_file or os.path.join(
            "logs", "sensor_fusion", f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        logger = CsvLogger(path)
        print(f"[Log] Writing to {path}")

    hr_stream = SharedStream(channel="hr", logger=logger)
    inst_stream = SharedStream(channel="hr_inst", logger=logger)
    rr_stream = SharedStream(channel="rr", logger=logger)
    girth_stream = SharedStream(channel="girth", logger=logger)
    marker_store = MarkerStore(logger=logger)
    arousal = ArousalStore(logger=logger)
    stop_event = threading.Event()
    hr_state = {"rr_seen": False, "warned": False}
    start_time = time.time()

    def on_fire(t, ch):
        print(f"\n>>> PREDICTED EDGE at t={t:.1f}s  (via {ch}) <<<")
        if logger:
            logger.log(t, "prediction", ch)

    detector = None if args.no_predict else EdgeDetector(on_fire=on_fire)

    if args.demo:
        threads = [
            threading.Thread(target=hr_demo_thread_main,
                             args=(hr_stream, inst_stream, rr_stream, start_time,
                                   stop_event, hr_state), daemon=True),
            threading.Thread(target=girth_demo_thread_main,
                             args=(girth_stream, start_time, stop_event,
                                   args.um_per_count, marker_store), daemon=True),
        ]
    else:
        threads = [
            threading.Thread(target=hr_thread_main,
                             args=(hr_stream, inst_stream, rr_stream, start_time,
                                   stop_event, hr_state), daemon=True),
            threading.Thread(target=girth_thread_main,
                             args=(girth_stream, start_time, stop_event,
                                   args.serial_port, args.baud, args.um_per_count),
                             daemon=True),
        ]
    for th in threads:
        th.start()

    # -- detector thread: a steady 10 Hz tick, independent of the redraw rate --
    pred_lines = []          # (t_fire, channel)
    last_press_report = {"text": ""}

    def detector_main():
        next_tick = time.time()
        while not stop_event.is_set():
            now = time.time()
            if now < next_tick:
                time.sleep(min(0.02, next_tick - now))
                continue
            t = next_tick - start_time
            next_tick += DT
            if t < 0:
                continue
            g = girth_stream.at(t)
            h = inst_stream.at(t) if hr_state["rr_seen"] else hr_stream.at(t)
            detector.tick(t, g, h)

    if detector:
        threading.Thread(target=detector_main, daemon=True).start()

    # -- figure --------------------------------------------------------
    for km in ("keymap.save", "keymap.grid", "keymap.grid_minor", "keymap.pan",
               "keymap.zoom", "keymap.xscale", "keymap.yscale", "keymap.home",
               "keymap.back", "keymap.forward", "keymap.fullscreen", "keymap.copy"):
        if km in plt.rcParams:
            plt.rcParams[km] = []

    fig, (ax_g, ax_h, ax_a) = plt.subplots(
        3, 1, sharex=True, figsize=(12, 8),
        gridspec_kw={"height_ratios": [3, 2, 1.4]})
    fig.canvas.manager.set_window_title("AIMO edge monitor")

    girth_unit = "mm" if args.um_per_count else "counts"
    (girth_line,) = ax_g.plot([], [], color=GIRTH_C, lw=1.6)
    (girth_sm,) = ax_g.plot([], [], color=GIRTH_C, lw=2.6, alpha=0.35)
    girth_bad = ax_g.scatter([], [], color=EDGE_C, marker="x", s=28, zorder=3)
    ax_g.set_ylabel(f"Girth ({girth_unit})")
    ax_g.grid(alpha=0.25)

    (hr_line,) = ax_h.plot([], [], color=HR_C, lw=1.6)
    ax_h.set_ylabel("Heart rate (bpm)")
    ax_h.grid(alpha=0.25)

    (ar_line,) = ax_a.plot([], [], color="#6b4fa8", lw=2.2, drawstyle="steps-post")
    ax_a.set_ylabel("Arousal")
    ax_a.set_ylim(-0.5, 10.5)
    ax_a.set_yticks([0, 2, 4, 6, 8, 10])
    ax_a.set_xlabel("Time (s)")
    ax_a.grid(alpha=0.25)

    status = fig.text(0.01, 0.975, "", fontsize=10, va="top", family="monospace")
    banner = fig.text(0.5, 0.975, "", fontsize=15, va="top", ha="center",
                      color=PRED_C, fontweight="bold")
    fig.text(0.99, 0.975, "SPACE edge   S calm   0-9/+ arousal   Q quit",
             fontsize=9, va="top", ha="right", color="#666")

    vlines = []

    # -- keys ----------------------------------------------------------
    last_key = {"edge": 0.0, "calm": 0.0}

    def on_key(event):
        key = (event.key or "").lower()
        now = time.time()
        t = now - start_time
        if key in (" ", "space"):
            if now - last_key["edge"] < 0.5:
                return
            last_key["edge"] = now
            marker_store.add(t, "edge")
            if detector:
                fired = [f for f in detector.fires if f[0] >= detector.episode_start]
                if fired:
                    d = fired[0][0] - t
                    last_press_report["text"] = (
                        f"last press: predicted {abs(d):.1f}s "
                        f"{'EARLY' if d < 0 else 'LATE'} via {fired[0][1]}")
                else:
                    last_press_report["text"] = "last press: no prediction fired"
                detector.mark(t, "edge")
            print(f"\n>>> EDGE marked at t={t:.1f}s <<<   {last_press_report['text']}")
        elif key == "s":
            if now - last_key["calm"] < 0.5:
                return
            last_key["calm"] = now
            marker_store.add(t, "calm")
            if detector:
                detector.mark(t, "calm")
            print(f"\n>>> CALM marked at t={t:.1f}s -- detector armed <<<")
        elif key in AROUSAL_KEYS:
            lvl = AROUSAL_KEYS[key]
            arousal.add(t, lvl)
            print(f"    arousal {lvl}/10 at t={t:.1f}s")

    fig.canvas.mpl_connect("key_press_event", on_key)

    def update(_frame):
        now = time.time() - start_time
        lo = max(0.0, now - args.window)

        gp = girth_stream.window(now, args.window)
        if gp:
            xs, ys = zip(*gp)
            girth_line.set_data(xs, ys)
            pad = max(1.0, (max(ys) - min(ys)) * 0.12)
            ax_g.set_ylim(min(ys) - pad, max(ys) + pad)
        bad = girth_stream.window_rejected(now, args.window)
        girth_bad.set_offsets(np.array(bad) if bad else np.empty((0, 2)))
        if detector and detector.grid_t:
            gt = np.asarray(detector.grid_t)
            gs = np.asarray([v if v is not None else np.nan
                             for v in detector.smooth["girth"]], dtype=float)
            m = gt >= lo
            girth_sm.set_data(gt[m], gs[m])

        src = inst_stream if hr_state["rr_seen"] else hr_stream
        hp = src.window(now, args.window)
        if hp:
            xs, ys = zip(*hp)
            hr_line.set_data(xs, ys)
            ax_h.set_ylim(min(ys) - 3, max(ys) + 3)

        ax, ay = arousal.step_trace(now, args.window)
        ar_line.set_data(ax, ay)

        for ln in vlines:
            ln.remove()
        vlines.clear()
        for t, label in marker_store.window(now, args.window):
            c = EDGE_C if label == "edge" else CALM_C
            for a in (ax_g, ax_h, ax_a):
                vlines.append(a.axvline(t, color=c, ls="--", lw=1.4, alpha=0.85))
        if detector:
            for tf, ch in detector.fires:
                if tf >= lo:
                    for a in (ax_g, ax_h, ax_a):
                        vlines.append(a.axvline(tf, color=PRED_C, ls="-", lw=2.0, alpha=0.9))

        # status text
        if detector:
            gr, gh = detector.status["girth"]
            hrr, hh = detector.status["hr"]
            el = now - detector.episode_start
            if now < WARMUP:
                head = f"warming up {now:4.0f}/{WARMUP:.0f}s"
            elif detector.fired:
                head = "FIRED"
            elif el < MIN_ON:
                head = f"armed in {MIN_ON - el:3.1f}s"
            else:
                head = "watching"
            status.set_text(
                f"{head:<18} on-period {el:5.1f}s\n"
                f"girth  d/dt {gr:5.2f} x peak   hold {gh:3.1f}/{HOLD:.1f}s\n"
                f"HR     d/dt {hrr:5.2f} x peak   hold {hh:3.1f}/{HOLD:.1f}s\n"
                f"{last_press_report['text']}")
            recent = [f for f in detector.fires if now - f[0] < 12]
            banner.set_text(f"EDGE PREDICTED  ({now - recent[-1][0]:.0f}s ago, {recent[-1][1]})"
                            if recent else "")
        else:
            status.set_text(f"prediction off    arousal {arousal.current}")

        ax_g.set_xlim(lo, max(args.window, now))
        return ()

    anim = FuncAnimation(fig, update, interval=PLOT_INTERVAL_MS,
                         blit=False, cache_frame_data=False)
    fig._anim = anim   # keep a reference alive

    print("Click the plot window, then: SPACE = edge, S = calm, 0-9/+ = arousal.")
    try:
        fig.tight_layout(rect=[0, 0, 1, 0.955])
        plt.show()
    finally:
        stop_event.set()
        if logger:
            logger.close()
            print(f"[Log] Saved to {logger.path}")


if __name__ == "__main__":
    main()
