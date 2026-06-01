import bisect
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class ScriptAction:
    at: int
    pos: float


class FunscriptPlayer:
    def __init__(self, send_command_fn: Callable[[dict], None],
                 on_state_change: Optional[Callable] = None):
        self.send_command = send_command_fn
        self.on_state_change = on_state_change
        self.actions: List[ScriptAction] = []
        self._start_time: Optional[float] = None
        self._paused_offset_ms: float = 0.0
        self._running = False
        self._current_index: int = 0
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._last_filepath: Optional[str] = None
        self.latency_ms: float = 0.0
        self.invert: bool = False

    def _extract_actions(self, data: dict) -> list:
        """Extract stroke actions from funscript, handling multi-axis format."""
        raw = data.get("actions")
        if raw and len(raw) > 0:
            return raw

        axes = data.get("axes", [])
        if axes and len(axes) > 0:
            for axis in axes:
                axis_actions = axis.get("actions", [])
                if axis_actions:
                    return axis_actions
        return []

    def load_data(self, data: dict, filepath: Optional[str] = None) -> dict:
        raw_actions = self._extract_actions(data)
        if not raw_actions:
            raise ValueError("No actions found in funscript")

        self.actions = sorted(
            [ScriptAction(int(a["at"]), float(a["pos"]))
             for a in raw_actions
             if isinstance(a, dict) and "at" in a and "pos" in a],
            key=lambda x: x.at
        )
        self._current_index = 0
        self._paused_offset_ms = 0.0
        self._start_time = None
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._last_filepath = filepath

        duration_ms = self.actions[-1].at if self.actions else 0
        return {
            "actions": len(self.actions),
            "duration_ms": duration_ms,
            "duration_str": self._format_time(duration_ms),
        }

    def load_file(self, filepath: str) -> dict:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return self.load_data(data, filepath)

    def set_config(self, latency_ms: Optional[float] = None,
                   invert: Optional[bool] = None) -> dict:
        """Update runtime playback configuration."""
        if latency_ms is not None:
            self.latency_ms = float(latency_ms)
        if invert is not None:
            self.invert = bool(invert)
        return self.get_config()

    def get_config(self) -> dict:
        return {"latency_ms": self.latency_ms, "invert": self.invert}

    def start(self, offset_ms: int = 0) -> bool:
        if not self.actions:
            return False
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._start_time = time.monotonic() * 1000 - offset_ms
            self._paused_offset_ms = 0.0
            self._running = True
            self._current_index = bisect.bisect_left(
                [a.at for a in self.actions], offset_ms
            )
        self._notify("playing", offset_ms)
        self._schedule_next()
        return True

    def pause(self):
        with self._lock:
            if not self._running:
                return
            if self._start_time:
                self._paused_offset_ms = time.monotonic() * 1000 - self._start_time
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self.send_command({"cmd": "stop"})
        self._notify("paused", int(self._paused_offset_ms))

    def resume(self):
        if self._running:
            return False
        return self.start(int(self._paused_offset_ms))

    def seek(self, target_ms: int):
        with self._lock:
            self._current_index = bisect.bisect_left(
                [a.at for a in self.actions], target_ms
            )
            if self._running and self._start_time:
                self._start_time = time.monotonic() * 1000 - target_ms
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
            else:
                # If paused (or never started), update the resume offset so
                # resume() continues from the seeked position.
                self._paused_offset_ms = float(target_ms)
        self._notify("seeked", target_ms)
        if self._running:
            self._schedule_next()

    def stop(self):
        with self._lock:
            self._running = False
            self._current_index = 0
            self._paused_offset_ms = 0.0
            self._start_time = None
            if self._timer:
                self._timer.cancel()
                self._timer = None
        self.send_command({"cmd": "stop"})
        self._notify("stopped", 0)

    def get_status(self) -> dict:
        with self._lock:
            elapsed = 0.0
            if self._start_time:
                if self._running:
                    elapsed = time.monotonic() * 1000 - self._start_time
                else:
                    elapsed = self._paused_offset_ms

            total_duration = self.actions[-1].at if self.actions else 0

            current_pos = 0.0
            if self.actions:
                idx = bisect.bisect_right([a.at for a in self.actions], elapsed) - 1
                if idx >= 0 and idx < len(self.actions) - 1:
                    a1, a2 = self.actions[idx], self.actions[idx + 1]
                    dt = a2.at - a1.at
                    t = (elapsed - a1.at) / dt if dt > 0 else 0
                    current_pos = a1.pos + t * (a2.pos - a1.pos)
                elif idx >= len(self.actions) - 1:
                    current_pos = self.actions[-1].pos
                elif idx < 0:
                    current_pos = self.actions[0].pos

            if self.invert:
                current_pos = 100.0 - current_pos

            next_action = None
            if self._current_index < len(self.actions):
                na = self.actions[self._current_index]
                pos = na.pos
                if self.invert:
                    pos = 100.0 - pos
                next_action = {"at": na.at, "pos": pos}

            return {
                "loaded": len(self.actions) > 0,
                "running": self._running,
                "elapsed_ms": int(elapsed),
                "total_ms": total_duration,
                "progress_pct": round((elapsed / total_duration) * 100, 1) if total_duration else 0.0,
                "current_index": self._current_index,
                "total_actions": len(self.actions),
                "current_position": round(current_pos, 2),
                "next_action": next_action,
                "filepath": self._last_filepath,
                "latency_ms": self.latency_ms,
                "invert": self.invert,
            }

    def _schedule_next(self):
        with self._lock:
            if not self._running or self._current_index >= len(self.actions):
                if self._current_index >= len(self.actions):
                    self._running = False
                    self._notify("finished", 0)
                return
            action = self.actions[self._current_index]
            start_time = self._start_time  # snapshot under lock

        elapsed = time.monotonic() * 1000 - start_time
        delay_ms = max(0.0, action.at - elapsed + self.latency_ms)

        self._timer = threading.Timer(delay_ms / 1000.0, self._execute_action, args=[action])
        self._timer.daemon = True
        self._timer.start()

    def _execute_action(self, action: ScriptAction):
        with self._lock:
            if not self._running:
                return

            duration_ms = 0
            target_pos = float(action.pos)
            if self._current_index + 1 < len(self.actions):
                next_a = self.actions[self._current_index + 1]
                duration_ms = next_a.at - action.at
                target_pos = float(next_a.pos)

            self._current_index += 1

        pos = target_pos
        if self.invert:
            pos = 100.0 - pos

        self._schedule_next()

        if duration_ms > 0:
            self.send_command({
                "cmd": "stream",
                "pct": pos,
                "duration": int(duration_ms)
            })

        self._notify("action", {"pos": pos, "duration_ms": duration_ms})

    def _format_time(self, ms: int) -> str:
        s = ms // 1000
        return f"{s//60:02d}:{s%60:02d}"

    def _notify(self, state: str, value):
        if self.on_state_change:
            try:
                self.on_state_change(state, value)
            except Exception:
                pass