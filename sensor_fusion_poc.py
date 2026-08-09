"""
sensor_fusion_poc.py
---------------------
Proof-of-concept dual live plot: heart rate (BLE) on top, girth/position
sensor (serial) on the bottom, sharing one timeline.

This is deliberately just the visualization/sync layer -- it proves the two
streams can be read concurrently and lined up on a common clock. Once the
timelines are validated against real hardware, a fused "arousal index" can
be layered on top the same way heart_rate_sensor.py layers a state machine
on top of raw BPM.

Girth sensor protocol (as observed over serial, one line per frame):
    Raw Frame: 0x2D830 | Position: 2912 | Status: 0x30

- Position is a raw counter value. The device is specified at 2 micrometer
  resolution per count; pass --um-per-count to convert to millimeters, or
  leave it off to plot raw counts (default -- no calibration is assumed).
- Status 0x30 is the value seen on ~98% of frames in the sample log and is
  treated as "valid". Any other status (e.g. the single 0x20 observed) is
  plotted as a hollow red marker instead of joined into the line, so bad
  frames are visible but don't corrupt the trace.

Usage:
    python sensor_fusion_poc.py --demo                       # no hardware needed
    python sensor_fusion_poc.py --serial-port /dev/ttyUSB0    # real girth sensor + real HRM
    python sensor_fusion_poc.py --serial-port COM5 --um-per-count 0.002
"""

import argparse
import asyncio
import random
import re
import threading
import time
from collections import deque

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

FRAME_RE = re.compile(
    r"Raw Frame:\s*(0x[0-9A-Fa-f]+)\s*\|\s*Position:\s*(-?\d+)\s*\|\s*Status:\s*(0x[0-9A-Fa-f]+)"
)
VALID_STATUS = "0x30"

WINDOW_SECONDS = 60
PLOT_INTERVAL_MS = 200


class SharedStream:
    """Thread-safe-enough ring buffer of (t, value) pairs for a live plot.

    CPython's GIL makes append/iterate races benign here (worst case is one
    torn read of the newest point on a redraw) -- fine for a POC, not a
    guarantee you'd want in something safety-critical.
    """

    def __init__(self, maxlen=5000):
        self.points = deque(maxlen=maxlen)   # (t, value)
        self.rejected = deque(maxlen=500)    # (t, value) -- bad-status frames

    def add(self, t, value):
        self.points.append((t, value))

    def add_rejected(self, t, value):
        self.rejected.append((t, value))

    def window(self, t0, seconds):
        return [(t, v) for t, v in self.points if t >= t0 - seconds]

    def window_rejected(self, t0, seconds):
        return [(t, v) for t, v in self.rejected if t >= t0 - seconds]


# ---------------------------------------------------------------------------
# Heart rate (BLE)
# ---------------------------------------------------------------------------

def hr_notification_handler(stream, start_time):
    def handler(_sender, data):
        flags = data[0]
        is_16_bit_hr = flags & 0x01
        offset = 1
        if is_16_bit_hr:
            bpm = int.from_bytes(data[offset:offset + 2], byteorder="little")
        else:
            bpm = data[offset]
        stream.add(time.time() - start_time, bpm)
    return handler


async def run_hr_ble(stream, start_time, stop_event):
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
        await client.start_notify(HR_MEASUREMENT_UUID, hr_notification_handler(stream, start_time))
        print("[HR] Streaming.")
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
        await client.stop_notify(HR_MEASUREMENT_UUID)


def hr_thread_main(stream, start_time, stop_event):
    try:
        asyncio.run(run_hr_ble(stream, start_time, stop_event))
    except Exception as exc:
        print(f"[HR] thread stopped: {exc}")


def hr_demo_thread_main(stream, start_time, stop_event):
    """Synthetic BPM: slow baseline drift plus occasional arousal-style ramps."""
    bpm = 68.0
    t_next_ramp = time.time() + random.uniform(8, 15)
    ramping = False
    while not stop_event.is_set():
        now = time.time()
        if not ramping and now >= t_next_ramp:
            ramping = True
        if ramping:
            bpm += random.uniform(0.3, 1.2)
            if bpm > 130 or random.random() < 0.02:
                ramping = False
                t_next_ramp = now + random.uniform(10, 20)
        else:
            bpm += random.uniform(-0.4, 0.3)
            bpm = max(60.0, bpm)
        stream.add(now - start_time, bpm + random.uniform(-1, 1))
        time.sleep(1.0)


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
            line = raw_line.decode("ascii", errors="ignore").strip()
            m = FRAME_RE.search(line)
            if not m:
                continue
            _raw_frame, position_str, status = m.groups()
            position = int(position_str)
            value = position * um_per_count if um_per_count else position
            t = time.time() - start_time
            if status == VALID_STATUS:
                stream.add(t, value)
            else:
                stream.add_rejected(t, value)


def girth_demo_thread_main(stream, start_time, stop_event, um_per_count):
    """Synthetic girth trace: noisy baseline around 3000 counts."""
    position = 3000.0
    while not stop_event.is_set():
        now = time.time()
        position += random.uniform(-15, 15)
        position = max(2000.0, min(4200.0, position))
        value = position * um_per_count if um_per_count else position
        stream.add(now - start_time, value)
        if random.random() < 0.02:
            stream.add_rejected(now - start_time, value)  # occasional bad-status frame
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Live plot
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--demo", action="store_true", help="synthesize both streams, no hardware required")
    parser.add_argument("--serial-port", default=None, help="serial port for the girth sensor, e.g. /dev/ttyUSB0 or COM5")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--um-per-count", type=float, default=None, help="convert raw Position counts to mm (device spec: 0.002)")
    parser.add_argument("--window", type=float, default=WINDOW_SECONDS, help="seconds of history shown on screen")
    args = parser.parse_args()

    if not args.demo and not args.serial_port:
        parser.error("--serial-port is required unless --demo is set")

    hr_stream = SharedStream()
    girth_stream = SharedStream()
    stop_event = threading.Event()
    start_time = time.time()

    if args.demo:
        threads = [
            threading.Thread(target=hr_demo_thread_main, args=(hr_stream, start_time, stop_event), daemon=True),
            threading.Thread(target=girth_demo_thread_main, args=(girth_stream, start_time, stop_event, args.um_per_count), daemon=True),
        ]
    else:
        threads = [
            threading.Thread(target=hr_thread_main, args=(hr_stream, start_time, stop_event), daemon=True),
            threading.Thread(
                target=girth_thread_main,
                args=(girth_stream, start_time, stop_event, args.serial_port, args.baud, args.um_per_count),
                daemon=True,
            ),
        ]
    for t in threads:
        t.start()

    fig, (ax_hr, ax_girth) = plt.subplots(2, 1, sharex=True, figsize=(10, 6))
    fig.suptitle("Heart Rate + Girth Sensor Fusion (POC)")

    (hr_line,) = ax_hr.plot([], [], color="tab:red", lw=1.5)
    ax_hr.set_ylabel("BPM")
    ax_hr.grid(alpha=0.3)
    hr_text = ax_hr.text(0.99, 0.92, "", transform=ax_hr.transAxes, ha="right", va="top")

    girth_unit = "mm" if args.um_per_count else "counts"
    (girth_line,) = ax_girth.plot([], [], color="tab:blue", lw=1.5)
    girth_bad = ax_girth.scatter([], [], color="red", marker="x", s=30, zorder=3, label="bad status")
    ax_girth.set_ylabel(f"Position ({girth_unit})")
    ax_girth.set_xlabel("Time (s)")
    ax_girth.grid(alpha=0.3)
    ax_girth.legend(loc="upper right", fontsize=8)
    girth_text = ax_girth.text(0.99, 0.92, "", transform=ax_girth.transAxes, ha="right", va="top")

    def update(_frame):
        now = time.time() - start_time

        hr_pts = hr_stream.window(now, args.window)
        if hr_pts:
            xs, ys = zip(*hr_pts)
            hr_line.set_data(xs, ys)
            ax_hr.set_ylim(min(ys) - 5, max(ys) + 5)
            hr_text.set_text(f"{ys[-1]:.0f} BPM")

        girth_pts = girth_stream.window(now, args.window)
        bad_pts = girth_stream.window_rejected(now, args.window)
        if girth_pts:
            xs, ys = zip(*girth_pts)
            girth_line.set_data(xs, ys)
            pad = max(1.0, (max(ys) - min(ys)) * 0.1)
            ax_girth.set_ylim(min(ys) - pad, max(ys) + pad)
            girth_text.set_text(f"{ys[-1]:.1f} {girth_unit}")
        girth_bad.set_offsets(np.array(bad_pts) if bad_pts else np.empty((0, 2)))

        ax_hr.set_xlim(max(0, now - args.window), max(args.window, now))
        return hr_line, girth_line, girth_bad, hr_text, girth_text

    anim = FuncAnimation(fig, update, interval=PLOT_INTERVAL_MS, blit=False, cache_frame_data=False)

    try:
        plt.tight_layout()
        plt.show()
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
