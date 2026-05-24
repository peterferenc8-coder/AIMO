import asyncio
import time
import math
import random
import json
import threading
from datetime import datetime
from collections import deque
from pynput import keyboard
from bleak import BleakScanner, BleakClient

# Standard Bluetooth SIG UUIDs for Heart Rate
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"


class EdgeDetector:
    """
    Heart-rate edge detector with adaptive thresholds, session calibration,
    structured logging, and keyboard ground-truth annotation.
    """

    def __init__(self):
        # ---- Ring buffers ----
        self.rr_history = deque(maxlen=300)       # 5 min window for baseline
        self.recent_rr = deque(maxlen=10)         # short window for velocity
        self.hrv_window_rr = deque(maxlen=60)     # 30-60s window for RMSSD
        self.hrv_history = deque(maxlen=200)      # historical RMSSD distribution
        self.recent_bpm = deque(maxlen=10)        # for descent detection

        # ---- State ----
        self.state = "IDLE"
        self.state_entry_time = None
        self.warmup_count = 0
        self.building_peak_bpm = 0.0      # highest BPM seen during current BUILDING phase

        # ---- Metrics ----
        self.arousal = 0.0
        self.last_update_time = None
        self.prev_bpm = None
        self.baseline_rr = None
        self.baseline_bpm = None
        self.last_rmssd = None

        # ---- Session calibration ----
        self.session_max_excess = 8.0    # seed low so real data calibrates quickly
        self.edges_this_session = 0

        # ---- Timers ----
        self.cooldown_start = None
        self.dropoff_start = None
        self.random_additional_delay = 0.0

        # ---- Ground truth ----
        self.user_pressed_space = False
        self.space_press_time = None

        # ---- Configuration ----
        self.cfg = {
            "warmup_beats": 15,
            "building_min_seconds": 20.0,     # base gate (adaptive below)
            "building_min_seconds_fast": 5.0,  # velocity fast-track
            "cooldown_seconds": 10.0,
            "holdoff_seconds": 5.0,
            "floor_margin_bpm": 3.0,
            "building_entry_bpm": 4.0,
            "plateau_hard_bpm_min": 8.0,       # absolute floor for hard trigger
            "plateau_hard_bpm_pct": 0.65,      # % of session max excess (lowered: user presses at ~9-10 BPM excess, threshold was 11-13)
            "plateau_peak_recede_bpm": 4.0,    # don't arm if BPM has fallen this far from recent peak (catches post-peak false arms)
            "arousal_threshold": 150.0,
            "arousal_surge_multiplier": 2.0,   # early trigger during BUILDING (raised from 1.5)
            "arousal_surge_min_excess": 12.0,  # minimum excess BPM required for early surge trigger
            "arousal_surge_min_building_s": 15.0,  # minimum time in BUILDING before surge can fire
            "plateau_dwell_guard_s": 2.0,      # minimum time in PLATEAU before any trigger fires
            "arousal_decay_halflife": 12.0,
            "arousal_decay_halflife_fast": 4.0, # when excess < 2
            "velocity_trigger": 2.0,
            "velocity_fast_track": 1.5,        # BPM/sec to shorten building gate
            "velocity_emergency": 2.5,         # BPM/sec to emergency-arm
            "hrv_percentile": 0.25,
            "dropoff_seconds": 5.0,
            "random_delay_max": 0.0,
            "descent_veto_beats": 3,           # consecutive descending beats
        }

        # ---- Logging ----
        self.log_file = open("hrm_session.json", "a")
        self._log_event("session_start", {"config": self.cfg})

    def _log_event(self, event_type, data):
        entry = {
            "ts": datetime.now().isoformat(),
            "event": event_type,
            **data
        }
        self.log_file.write(json.dumps(entry) + "\n")
        self.log_file.flush()

    def on_space_press(self):
        """Called by keyboard listener when user presses space."""
        now = time.time()
        # Debounce: ignore OS key-repeat events within 0.5s of last accepted press
        if self.space_press_time is not None and (now - self.space_press_time) < 0.5:
            return
        self.user_pressed_space = True
        self.space_press_time = now
        self._log_event("user_ground_truth", {
            "state": self.state,
            "bpm": self.prev_bpm,
            "floor": self.baseline_bpm,
            "arousal": self.arousal,
            "excess": self.prev_bpm - self.baseline_bpm if self.prev_bpm else None,
        })
        print("\n>>> 👤 USER MARKED EDGE (Space) <<<")

    def _enter_state(self, new_state):
        if self.state != new_state:
            print(f"\n>>> STATE: {self.state} -> {new_state}")
            self._log_event("state_change", {
                "from": self.state,
                "to": new_state,
                "bpm": self.prev_bpm,
                "floor": self.baseline_bpm,
                "arousal": self.arousal,
            })
            self.state = new_state
            self.state_entry_time = time.time()

    def _update_baseline(self, rr_ms):
        if self.baseline_rr is None:
            self.baseline_rr = float(rr_ms)
            self.baseline_bpm = 60000.0 / self.baseline_rr
            return

        if self.state in ("BUILDING", "PLATEAU"):
            if rr_ms > self.baseline_rr:
                self.baseline_rr = self.baseline_rr * 0.98 + rr_ms * 0.02
                self.baseline_bpm = 60000.0 / self.baseline_rr
            return

        self.rr_history.append(rr_ms)
        n = len(self.rr_history)
        if n < 20:
            self.baseline_rr = sum(self.rr_history) / n
        else:
            sorted_rr = sorted(self.rr_history, reverse=True)
            calmest = sorted_rr[:max(1, n // 5)]
            self.baseline_rr = sum(calmest) / len(calmest)
        self.baseline_bpm = 60000.0 / self.baseline_rr

    def _compute_rmssd(self):
        if len(self.hrv_window_rr) < 10:
            return None
        rr = list(self.hrv_window_rr)
        diffs = [rr[i] - rr[i - 1] for i in range(1, len(rr))]
        sq = [d * d for d in diffs]
        return math.sqrt(sum(sq) / len(sq))

    def _is_hrv_suppressed(self, rmssd):
        if rmssd is None or len(self.hrv_history) < 30:
            return False
        sorted_hrv = sorted(self.hrv_history)
        idx = int(len(sorted_hrv) * self.cfg["hrv_percentile"])
        threshold = sorted_hrv[idx]
        return rmssd < threshold

    def _is_descending(self):
        """True if BPM has been descending for N consecutive beats."""
        n = self.cfg["descent_veto_beats"]
        if len(self.recent_bpm) < n + 1:
            return False
        recent = list(self.recent_bpm)[-n-1:]
        return all(recent[i] > recent[i+1] for i in range(n))

    def _update_arousal(self, bpm, excess_bpm, velocity, hrv_suppressed):
        now = time.time()
        if self.last_update_time is not None:
            dt = now - self.last_update_time
            # Fast decay when barely above floor (stimulation stopped)
            if excess_bpm < 2.0:
                halflife = self.cfg["arousal_decay_halflife_fast"]
            else:
                halflife = self.cfg["arousal_decay_halflife"]
            decay = 0.5 ** (dt / halflife)
            self.arousal *= decay
        self.last_update_time = now

        # Don't accumulate new arousal while cooling down — only decay.
        if self.state in ("STOPPED", "RECOVERING"):
            return

        self.arousal += excess_bpm * 1.0
        if velocity > 0.3:
            self.arousal += velocity * 4.0
        if hrv_suppressed:
            self.arousal += 3.0
        self.arousal = min(self.arousal, 500.0)

    def _get_hard_threshold(self):
        """Adaptive hard threshold: max of absolute minimum and % of session max."""
        calibrated = self.session_max_excess * self.cfg["plateau_hard_bpm_pct"]
        return self.baseline_bpm + max(self.cfg["plateau_hard_bpm_min"], calibrated)

    def _trigger_stop(self, now, reason):
        excess = self.prev_bpm - self.baseline_bpm if self.prev_bpm else 0
        self.session_max_excess = max(self.session_max_excess, excess)
        self.edges_this_session += 1

        self._enter_state("STOPPED")
        self.cooldown_start = None
        self.dropoff_start = None
        self.random_additional_delay = 0.0

        if self.cfg["random_delay_max"] > 0:
            self.random_additional_delay = random.random() * self.cfg["random_delay_max"]

        current_bpm = 60000.0 / self.recent_rr[-1] if self.recent_rr else 0.0
        rmssd_str = f"{self.last_rmssd:.1f}" if self.last_rmssd is not None else "N/A"
        user_lag = None
        if self.user_pressed_space and self.space_press_time:
            user_lag = now - self.space_press_time

        print("\n" + "=" * 50)
        print(f"🚨 EDGE DETECTED! STOPPING! 🚨")
        print(f"Reason: {reason}")
        print(f"Floor: {self.baseline_bpm:.1f} BPM | Current: {current_bpm:.1f} BPM")
        print(f"Arousal: {self.arousal:.1f} | RMSSD: {rmssd_str}")
        print(f"Session edges: {self.edges_this_session} | Max excess: {self.session_max_excess:.1f}")
        if user_lag is not None:
            print(f"User lag: {user_lag:+.1f}s ({'early' if user_lag < 0 else 'late'})")
        if self.random_additional_delay:
            print(f"Random delay: +{self.random_additional_delay:.1f}s")
        print("=" * 50 + "\n")

        self._log_event("edge_detected", {
            "reason": reason,
            "bpm": current_bpm,
            "floor": self.baseline_bpm,
            "excess": excess,
            "arousal": self.arousal,
            "rmssd": self.last_rmssd,
            "user_lag": user_lag,
            "session_max_excess": self.session_max_excess,
            "edges_this_session": self.edges_this_session,
        })

        # Reset ground truth flag after logging, but keep space_press_time so
        # the debounce window still blocks any key-repeat events that follow.
        self.user_pressed_space = False

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _run_state_machine(self, bpm, excess_bpm, velocity, rmssd, hrv_suppressed):
        now = time.time()

        # ---------------- IDLE ----------------
        if self.state == "IDLE":
            self.building_peak_bpm = 0.0  # reset so next BUILDING phase starts fresh
            if rmssd is not None:
                self.hrv_history.append(rmssd)

            if bpm > self.baseline_bpm + self.cfg["building_entry_bpm"]:
                self._enter_state("BUILDING")
                self.dropoff_start = None
            elif self.arousal > 50.0:
                self._enter_state("BUILDING")
                self.dropoff_start = None

        # ---------------- BUILDING ----------------
        elif self.state == "BUILDING":
            # Track the peak BPM reached this build phase
            if bpm > self.building_peak_bpm:
                self.building_peak_bpm = bpm

            # Dropoff detection
            if bpm <= self.baseline_bpm + self.cfg["floor_margin_bpm"]:
                if self.dropoff_start is None:
                    self.dropoff_start = now
                elif now - self.dropoff_start > self.cfg["dropoff_seconds"]:
                    print(f"  Dropoff: HR returned to floor ({bpm:.1f} BPM)")
                    self._enter_state("IDLE")
                    self.dropoff_start = None
                    return
            else:
                self.dropoff_start = None

            # Adaptive building gate based on velocity
            time_in_building = now - self.state_entry_time
            required_time = self.cfg["building_min_seconds"]

            if velocity > self.cfg["velocity_emergency"] and excess_bpm > 5.0:
                required_time = 3.0
                print(f"  ⚡ Emergency fast-track (vel={velocity:.1f})")
            elif velocity > self.cfg["velocity_fast_track"] and excess_bpm > 5.0:
                required_time = max(5.0, self.cfg["building_min_seconds"] * 0.5)
                print(f"  🚀 Fast-track (vel={velocity:.1f})")

            # Early arousal surge trigger — catch edges before arming,
            # but only after minimum build-up time and high enough excess.
            early_trigger = self.cfg["arousal_threshold"] * self.cfg["arousal_surge_multiplier"]
            if (self.arousal > early_trigger
                    and excess_bpm > self.cfg["arousal_surge_min_excess"]
                    and time_in_building >= self.cfg["arousal_surge_min_building_s"]):
                self._trigger_stop(now, "EARLY AROUSAL SURGE")
                return

            if time_in_building > required_time:
                # Descent veto: if BPM is descending when we try to arm, edge already passed
                if self._is_descending():
                    print(f"  ⬇️ Descent veto: edge likely already passed")
                    self._enter_state("IDLE")
                    return

                # Peak-recede veto: if BPM has already fallen significantly from its peak
                # this build, the HR spike is over — arming now would catch a false edge.
                recede = self.building_peak_bpm - bpm
                if recede > self.cfg["plateau_peak_recede_bpm"]:
                    print(f"  📉 Peak-recede veto: BPM fell {recede:.1f} from peak {self.building_peak_bpm:.1f}")
                    self._enter_state("IDLE")
                    return

                self._enter_state("PLATEAU")
                hard = self._get_hard_threshold()
                print(f"  ARMED. Floor: {self.baseline_bpm:.1f} | Hard trigger: {hard:.1f} | Arousal thresh: {self.cfg['arousal_threshold']:.0f}")

        # ---------------- PLATEAU ----------------
        elif self.state == "PLATEAU":
            time_in_plateau = now - self.state_entry_time
            if time_in_plateau < self.cfg["plateau_dwell_guard_s"]:
                return  # guard period: don't trigger on the very first beats after arming

            hard_threshold = self._get_hard_threshold()

            if bpm >= hard_threshold:
                self._trigger_stop(now, "HARD BPM THRESHOLD")
                return

            if self.arousal >= self.cfg["arousal_threshold"]:
                self._trigger_stop(now, "AROUSAL ACCUMULATOR")
                return

            if velocity >= self.cfg["velocity_trigger"] and excess_bpm > 3.0:
                self._trigger_stop(now, "RAPID ACCELERATION")
                return

            # Dropoff without edge
            if bpm <= self.baseline_bpm + self.cfg["floor_margin_bpm"]:
                if self.dropoff_start is None:
                    self.dropoff_start = now
                elif now - self.dropoff_start > self.cfg["dropoff_seconds"]:
                    print(f"  Dropoff from plateau without edge ({bpm:.1f} BPM)")
                    self._enter_state("IDLE")
                    self.dropoff_start = None
            else:
                self.dropoff_start = None

        # ---------------- STOPPED ----------------
        elif self.state == "STOPPED":
            if bpm > self.baseline_bpm + self.cfg["building_entry_bpm"]:
                if self.cooldown_start is not None:
                    print("  Anti-twitch: HR spiked during cooldown, resetting timer")
                self.cooldown_start = None
                self.dropoff_start = None
                return

            if bpm <= self.baseline_bpm + self.cfg["floor_margin_bpm"]:
                if self.cooldown_start is None:
                    self.cooldown_start = now
                    print(f"  Calming... cooldown started")

                elapsed = now - self.cooldown_start
                total_needed = self.cfg["cooldown_seconds"] + self.random_additional_delay

                if elapsed >= total_needed:
                    self._enter_state("RECOVERING")
                    self.cooldown_start = None
                    self.random_additional_delay = 0.0
                else:
                    print(f"  Cooldown: {elapsed:.1f}s / {total_needed:.1f}s")
            else:
                if self.cooldown_start is not None:
                    print("  HR rose during cooldown, pausing timer")
                self.cooldown_start = None

        # ---------------- RECOVERING ----------------
        elif self.state == "RECOVERING":
            time_in_state = now - self.state_entry_time
            if time_in_state >= self.cfg["holdoff_seconds"]:
                # Reset arousal so the next cycle starts from a clean baseline,
                # not with a residual hot value that would instantly re-trigger.
                self.arousal = 0.0
                # Clear the ground-truth flag so a space press from the previous
                # cycle doesn't trail the 👤 emoji into the next cycle's output.
                self.user_pressed_space = False
                self._enter_state("IDLE")
                print("  Ready for next cycle.")
                return

            if bpm > self.baseline_bpm + self.cfg["building_entry_bpm"]:
                print("  Anti-twitch: HR spiked during recovery, extending hold-off")
                self.state_entry_time = now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_rr(self, rr_ms, current_bpm):
        self.recent_rr.append(rr_ms)
        self.hrv_window_rr.append(rr_ms)
        self.warmup_count += 1

        if self.warmup_count < self.cfg["warmup_beats"]:
            print(f"Warming up... [{self.warmup_count}/{self.cfg['warmup_beats']}] (BPM: {current_bpm})")
            return

        self._update_baseline(rr_ms)
        if self.baseline_bpm is None:
            return

        bpm = 60000.0 / rr_ms
        excess_bpm = max(0.0, bpm - self.baseline_bpm)
        self.recent_bpm.append(bpm)

        if self.prev_bpm is not None:
            dt = rr_ms / 1000.0
            velocity = (bpm - self.prev_bpm) / dt if dt > 0 else 0.0
        else:
            velocity = 0.0
        self.prev_bpm = bpm

        rmssd = self._compute_rmssd()
        self.last_rmssd = rmssd
        hrv_suppressed = self._is_hrv_suppressed(rmssd)

        self._update_arousal(bpm, excess_bpm, velocity, hrv_suppressed)
        self._run_state_machine(bpm, excess_bpm, velocity, rmssd, hrv_suppressed)

        # Log every beat for replay analysis
        self._log_event("beat", {
            "bpm": bpm,
            "rr_ms": rr_ms,
            "floor": self.baseline_bpm,
            "excess": excess_bpm,
            "arousal": self.arousal,
            "velocity": velocity,
            "rmssd": rmssd,
            "hrv_suppressed": hrv_suppressed,
            "state": self.state,
        })

        if self.state in ("IDLE", "BUILDING", "PLATEAU"):
            status = (
                f"State: {self.state:8} | BPM: {bpm:5.1f} | "
                f"Floor: {self.baseline_bpm:5.1f} | Excess: {excess_bpm:4.1f} | "
                f"Arousal: {self.arousal:6.1f}"
            )
            if rmssd:
                status += f" | RMSSD: {rmssd:5.1f}"
            if hrv_suppressed:
                status += " [HRV↓]"
            if velocity > 0.5:
                status += f" | Vel: {velocity:+.1f}"
            if self.user_pressed_space:
                status += " 👤"
            print(status)

        elif self.state == "STOPPED":
            print(f"🔴 STOPPED | Current: {bpm:.1f} BPM | Floor: {self.baseline_bpm:.1f} BPM | Arousal: {self.arousal:.1f}")

        elif self.state == "RECOVERING":
            time_in_state = time.time() - self.state_entry_time
            remaining = max(0.0, self.cfg["holdoff_seconds"] - time_in_state)
            print(f"💚 RECOVERING | BPM: {bpm:.1f} | Arousal: {self.arousal:.1f} | Hold-off: {remaining:.1f}s left")

    def close(self):
        self._log_event("session_end", {
            "edges_this_session": self.edges_this_session,
            "session_max_excess": self.session_max_excess,
        })
        self.log_file.close()


# Initialize detector
detector = EdgeDetector()


def on_press(key):
    """Keyboard listener callback."""
    try:
        if key == keyboard.Key.space:
            detector.on_space_press()
    except Exception as e:
        print(f"Keyboard error: {e}")


# Start keyboard listener in background thread
keyboard_listener = keyboard.Listener(on_press=on_press)
keyboard_listener.start()


def notification_handler(sender, data):
    flags = data[0]
    is_16_bit_hr = (flags & 0x01)
    energy_expended_present = (flags & 0x08)
    rr_intervals_present = (flags & 0x10)

    offset = 1

    if is_16_bit_hr:
        heart_rate = int.from_bytes(data[offset:offset+2], byteorder='little')
        offset += 2
    else:
        heart_rate = data[offset]
        offset += 1

    if energy_expended_present:
        offset += 2

    if rr_intervals_present:
        while offset < len(data):
            raw_rr = int.from_bytes(data[offset:offset+2], byteorder='little')
            offset += 2
            rr_ms = (raw_rr / 1024.0) * 1000.0
            detector.process_rr(rr_ms, heart_rate)
    else:
        print(f"BPM: {heart_rate} (No RR data in this packet)")


async def run():
    print("Searching for Heart Rate Monitor...")
    print("Press SPACE when you feel you are on the edge.")

    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: HR_SERVICE_UUID in ad.service_uuids
    )

    if not device:
        print("No HRM belt found. Make sure it's on and not connected to another app.")
        return

    print(f"Connecting to {device.name or 'Unknown'} ({device.address})...")

    try:
        async with BleakClient(device) as client:
            print(f"Connected! Establishing baseline...\n")

            await client.start_notify(HR_MEASUREMENT_UUID, notification_handler)

            try:
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping...")
            finally:
                await client.stop_notify(HR_MEASUREMENT_UUID)
    finally:
        detector.close()
        keyboard_listener.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except Exception as e:
        detector.close()
        keyboard_listener.stop()
        raise