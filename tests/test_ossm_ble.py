"""
tests/test_ossm_ble.py
----------------------
Tests for the stock-firmware OSSM driver.

Two layers:

  * the reconciler is exercised directly, because the ordering rules it encodes
    (mode entry before settings, geometry before speed, resend after a mode
    reset) are exactly the firmware-specific traps that a passing BLE write
    would not catch;
  * the connect/write/notify path runs for real against tests/fake_bleak.py.
"""

import pytest

from devices.ossm_ble import (
    COMMAND_CHAR,
    SERVICE_UUID,
    SPEED_KNOB_CHAR,
    OSSMBleDevice,
    clamp_pct,
    is_homed_state,
    sensation_from_wire,
    sensation_to_wire,
)
from fake_bleak import FakeBleakClient, FakeBleakScanner, install, wait_for

ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def device():
    dev = OSSMBleDevice()
    yield dev
    dev.disconnect()


@pytest.fixture
def linked(monkeypatch):
    """A driver connected to a fake device, sitting in menu.idle."""
    install(monkeypatch)
    dev = OSSMBleDevice()
    assert dev.connect(ADDRESS) is True
    client = FakeBleakClient.latest()
    assert wait_for(lambda: bool(client.notify_callbacks))
    client.push_state(state="menu.idle")
    yield dev, client
    dev.disconnect()


# ── Value coercion ────────────────────────────────────────────────────────────

def test_clamp_pct_produces_values_the_firmware_will_accept():
    # commands.hpp rejects anything that does not round-trip through String(int),
    # so floats have to be flattened here or the write is silently dropped.
    assert clamp_pct(42.6) == 43
    assert clamp_pct("17") == 17
    assert clamp_pct(-5) == 0
    assert clamp_pct(150) == 100
    assert clamp_pct(None) == 0
    assert clamp_pct("nonsense") == 0


def test_sensation_maps_signed_range_onto_the_unsigned_setting():
    # AIMO's intents carry StrokeEngine's signed sensation; the firmware takes
    # a percentage and re-expands it with calculateSensation().
    assert sensation_to_wire(-100) == 0
    assert sensation_to_wire(0) == 50
    assert sensation_to_wire(100) == 100
    assert sensation_to_wire(-30) == 35
    # Out of range must still land inside 0-100 or the device ignores the write.
    assert sensation_to_wire(-500) == 0
    assert sensation_to_wire(500) == 100


def test_sensation_wire_round_trip():
    for raw in (-100, -50, 0, 50, 100):
        assert sensation_from_wire(sensation_to_wire(raw)) == pytest.approx(raw)


def test_homed_state_classification():
    assert is_homed_state("menu.idle")
    assert is_homed_state("strokeEngine.idle")
    assert not is_homed_state("homing.forward")
    assert not is_homed_state("idle")
    assert not is_homed_state("error.idle")
    assert not is_homed_state("")


# ── Reconciler ────────────────────────────────────────────────────────────────

def plan(device, now=1000.0):
    with device._lock:
        return device._plan(now)


def test_mode_entry_is_requested_only_from_menu(device):
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 40})

    device._fw_state = "homing.forward"
    assert plan(device) == []

    device._fw_state = "strokeEngine.preflight"
    assert plan(device) == []

    device._fw_state = "menu.idle"
    assert plan(device) == ["go:strokeEngine"]


def test_another_play_mode_is_backed_out_of_first(device):
    # The dial may have been left in simplePenetration. Without stepping back
    # to the menu an AI session would sit there doing nothing.
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 40})
    device._fw_state = "simplePenetration.idle"
    assert plan(device, now=1000.0) == ["go:menu"]

    device._fw_state = "menu.idle"
    assert plan(device, now=1002.0) == ["go:strokeEngine"]


def test_preflight_is_never_backed_out_of(device):
    # Backing out here would cancel the gate the user is about to confirm.
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 40})
    device._fw_state = "strokeEngine.preflight"
    assert plan(device, now=1000.0) == []


def test_mode_request_is_rate_limited(device):
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 40})
    device._fw_state = "menu.idle"

    assert plan(device, now=1000.0) == ["go:strokeEngine"]
    # A second pass a moment later must not queue another ButtonPress.
    assert plan(device, now=1000.2) == []
    assert plan(device, now=1002.0) == ["go:strokeEngine"]


def test_settings_are_withheld_until_the_mode_is_entered(device):
    # Entering strokeEngine wipes settings, so sending them first is wasted.
    device.apply_ai_commands({"pattern": "deeper", "speed": 60, "depth": 80,
                              "base": 20, "intensity": 40})
    device._fw_state = "menu.idle"
    assert plan(device) == ["go:strokeEngine"]


def test_settings_apply_with_geometry_before_speed(device):
    device.apply_ai_commands({"pattern": "deeper", "speed": 60, "depth": 80,
                              "base": 20, "intensity": 40})
    device._fw_state = "strokeEngine.idle"

    assert plan(device) == [
        "set:pattern:4",       # deeper
        "set:depth:80",
        "set:stroke:60",       # depth - base
        "set:sensation:70",    # (40 + 100) / 2
        "set:speed:60",
    ]


def test_settings_are_withheld_during_preflight(device):
    # Completing preflight runs resetSettingsStrokeEngine(), so anything sent
    # while the gate is up would be discarded.
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 50})
    device.ingest_state({"state": "strokeEngine.preflight", "sessionId": "s1"})
    assert plan(device) == []
    assert device._state.extra["in_stroke_engine"] is False

    device.ingest_state({"state": "strokeEngine.idle", "sessionId": "s1"})
    assert "set:speed:50" in plan(device)


def test_unchanged_settings_are_not_resent(device):
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 30})
    device._fw_state = "strokeEngine.idle"
    assert plan(device)
    assert plan(device) == []

    device.apply_ai_commands({"speed": 55})
    assert plan(device) == ["set:speed:55"]


def test_entering_stroke_engine_reapplies_everything(device):
    device.apply_ai_commands({"pattern": "insist", "speed": 30, "depth": 70,
                              "base": 10})
    device._fw_state = "strokeEngine.idle"
    assert plan(device)
    assert plan(device) == []

    # resetSettingsStrokeEngine() runs on the way in, so a menu round trip
    # invalidates every value we thought was live on the device.
    device.ingest_state({"state": "menu.idle", "sessionId": "s1"})
    device.ingest_state({"state": "strokeEngine.idle", "sessionId": "s2"})
    assert plan(device) == [
        "set:pattern:6",
        "set:depth:70",
        "set:stroke:60",
        "set:sensation:50",
        "set:speed:30",
    ]


def test_new_session_id_reapplies_everything(device):
    device.apply_ai_commands({"pattern": "robo_stroke", "speed": 25})
    device.ingest_state({"state": "strokeEngine.idle", "sessionId": "s1"})
    assert plan(device)
    assert plan(device) == []

    device.ingest_state({"state": "strokeEngine.idle", "sessionId": "s2"})
    # depth/stroke/sensation were never set by the intent, so they are still the
    # firmware's own post-reset defaults.
    assert plan(device) == [
        "set:pattern:2",
        "set:depth:10",
        "set:stroke:50",
        "set:sensation:50",
        "set:speed:25",
    ]


def test_stop_leaves_the_engine_exactly_once(device):
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 40})
    device._fw_state = "strokeEngine.idle"
    plan(device)

    device.apply_ai_commands({"pattern": "stop"})
    assert plan(device) == ["set:speed:0", "go:menu"]
    # The device has not reported the new state yet; do not pile on.
    assert plan(device) == []

    device.ingest_state({"state": "menu.idle", "sessionId": "s1"})
    assert plan(device) == []


def test_reported_settings_do_not_count_as_applied(device):
    # The device echoes its own settings back. Those must not be mistaken for
    # confirmation of ours, or a knob-limited speed would never be re-sent.
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 80})
    device.ingest_state({"state": "strokeEngine.idle", "sessionId": "s1",
                         "speed": 80, "depth": 50, "stroke": 50,
                         "sensation": 50, "pattern": 0})
    assert "set:speed:80" in plan(device)


# ── Command vocabulary ────────────────────────────────────────────────────────

def test_apply_ai_commands_derives_stroke_from_depth_and_base(device):
    device.apply_ai_commands({"depth": 90, "base": 25})
    assert device._desired["depth"] == 90
    assert device._desired["stroke"] == 65

    # A later depth-only update keeps the previous base.
    device.apply_ai_commands({"depth": 60})
    assert device._desired["stroke"] == 35


def test_stop_pattern_zeroes_speed(device):
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 70})
    assert device._want_running is True
    device.apply_ai_commands({"pattern": "stop"})
    assert device._want_running is False
    assert device._desired["speed"] == 0


def test_streaming_commands_are_dropped(device):
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 40})
    device._fw_state = "strokeEngine.idle"
    plan(device)

    device.send_command({"cmd": "stream", "pct": 80, "duration": 200})
    device.send_command({"cmd": "moveTo", "pct": 20, "speedPct": 90})
    # Nothing to reconcile: the stock firmware's streaming mode is not driven
    # from here, so these must not turn into motion commands.
    assert plan(device) == []


def test_set_zero_is_a_no_op(device):
    device.send_command({"cmd": "setZero"})
    device._fw_state = "strokeEngine.idle"
    device._want_running = True
    assert "go:menu" not in plan(device)


def test_send_command_setters_feed_desired_state(device):
    device.send_command({"cmd": "setDepthPct", "value": 70})
    device.send_command({"cmd": "setStrokePct", "value": 40})
    device.send_command({"cmd": "setSensation", "value": -60})
    device.send_command({"cmd": "setPattern", "value": 3})
    device.send_command({"cmd": "setSpeedPct", "value": 45})
    device.send_command({"cmd": "startPattern"})

    assert device._desired == {"speed": 45, "stroke": 40, "depth": 70,
                               "sensation": 20, "pattern": 3}
    assert device._want_running is True


def test_pattern_index_is_clamped_to_the_firmware_enum(device):
    device.send_command({"cmd": "setPattern", "value": 99})
    assert device._desired["pattern"] == 6
    device.send_command({"cmd": "setPattern", "value": -3})
    assert device._desired["pattern"] == 0


# ── Simulated position ────────────────────────────────────────────────────────

def running_state(device, speed=50, depth=60, stroke=50, pattern=0):
    device.ingest_state({
        "state": "strokeEngine.idle", "sessionId": "s1", "speed": speed,
        "depth": depth, "stroke": stroke, "sensation": 50, "pattern": pattern,
        "position": 0.0,
    })


def sample_positions(device, frames=400, step=0.02, start=1000.0):
    positions = []
    for i in range(frames):
        device.tick(now=start + i * step)
        positions.append(device._pos)
    return positions


def test_simulated_position_oscillates_between_base_and_depth(device):
    running_state(device, speed=60, depth=60, stroke=50)
    positions = sample_positions(device)

    # base = depth - stroke = 10, depth = 60.
    assert min(positions) < 20
    assert max(positions) > 50
    assert all(0.0 <= p <= 100.0 for p in positions)


def test_simulation_is_idle_when_the_device_reports_zero_speed(device):
    # Speed is the post-knob figure, so this also covers "knob turned down".
    running_state(device, speed=0)
    positions = sample_positions(device, frames=50)
    assert set(positions) == {0.0}
    assert device._state.extra["running"] is False


def test_simulation_stops_outside_the_stroke_engine(device):
    running_state(device, speed=60)
    sample_positions(device, frames=20)

    device.ingest_state({"state": "menu.idle", "sessionId": "s1", "speed": 60})
    frozen = device._pos
    sample_positions(device, frames=20, start=2000.0)
    assert device._pos == frozen


def test_pattern_change_restarts_the_simulated_stroke_index(device):
    running_state(device, speed=60, pattern=0)
    sample_positions(device, frames=40)
    assert device._pattern_index > 0

    running_state(device, speed=60, pattern=4)
    assert device._pattern_name == "deeper"
    assert device._pattern_index == 0


def test_position_updates_reach_listeners(device):
    seen = []
    device.add_listener(seen.append)
    running_state(device, speed=60)
    sample_positions(device, frames=100)

    positions = [msg for msg in seen if msg.get("type") == "position"]
    assert positions
    assert positions[-1]["simulated"] is True
    assert positions[-1]["running"] is True


# ── Transport ─────────────────────────────────────────────────────────────────

def test_connect_subscribes_to_state_and_sets_the_knob_policy(monkeypatch):
    install(monkeypatch)
    device = OSSMBleDevice(speed_knob_as_limit=True)
    try:
        assert device.connect(ADDRESS) is True
        client = FakeBleakClient.latest()
        assert wait_for(lambda: bool(client.notify_callbacks))
        assert wait_for(lambda: client.writes_to(SPEED_KNOB_CHAR) == ["true"])
    finally:
        device.disconnect()


def test_knob_policy_can_be_handed_to_the_host(monkeypatch):
    install(monkeypatch)
    device = OSSMBleDevice(speed_knob_as_limit=False)
    try:
        assert device.connect(ADDRESS) is True
        client = FakeBleakClient.latest()
        assert wait_for(lambda: client.writes_to(SPEED_KNOB_CHAR) == ["false"])
    finally:
        device.disconnect()


def test_connect_without_an_address_fails_fast(monkeypatch):
    install(monkeypatch)
    device = OSSMBleDevice()
    assert device.connect("") is False
    assert FakeBleakClient.instance_count() == 0


def test_full_session_reaches_the_device_in_order(linked):
    device, client = linked
    assert wait_for(lambda: device._state.extra.get("homed") is True)

    device.apply_ai_commands({"pattern": "teasing_and_pounding", "speed": 55,
                              "depth": 75, "base": 15, "intensity": -20})

    assert wait_for(lambda: "go:strokeEngine" in client.commands())
    # Only the mode change so far — settings would be wiped by the reset.
    assert client.commands() == ["go:strokeEngine"]

    client.push_state(state="strokeEngine.idle", session_id="session-2")
    assert wait_for(lambda: "set:speed:55" in client.commands())

    assert client.commands() == [
        "go:strokeEngine",
        "set:pattern:1",
        "set:depth:75",
        "set:stroke:60",
        "set:sensation:40",
        "set:speed:55",
    ]


def test_emergency_stop_jumps_the_queue(linked):
    device, client = linked
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 80})
    client.push_state(state="strokeEngine.idle", session_id="session-2")
    assert wait_for(lambda: "set:speed:80" in client.commands())
    client.clear_writes()

    device.emergency_stop()
    assert wait_for(lambda: client.commands()[:2] == ["set:speed:0", "go:menu"])
    assert device._state.emergency_stopped is True
    assert device._sim_running is False


def test_reconnect_reapplies_settings(monkeypatch):
    install(monkeypatch)
    device = OSSMBleDevice()
    try:
        assert device.connect(ADDRESS) is True
        first = FakeBleakClient.latest()
        assert wait_for(lambda: bool(first.notify_callbacks))
        first.push_state(state="strokeEngine.idle")
        device.apply_ai_commands({"pattern": "simple_stroke", "speed": 35})
        assert wait_for(lambda: "set:speed:35" in first.commands())

        # Drop the link; the reconnect loop builds a fresh client that knows
        # nothing about what the device holds.
        first.is_connected = False
        assert wait_for(lambda: FakeBleakClient.instance_count() > 1, timeout=6)
        second = FakeBleakClient.latest()
        assert wait_for(lambda: bool(second.notify_callbacks), timeout=6)

        second.push_state(state="strokeEngine.idle", session_id="session-9")
        assert wait_for(lambda: "set:speed:35" in second.commands())
    finally:
        device.disconnect()


def test_scan_matches_service_uuid_and_name(monkeypatch):
    install(monkeypatch)
    FakeBleakScanner.set_discovered([
        ("AA:00:00:00:00:01", "OSSM", [SERVICE_UUID]),
        ("AA:00:00:00:00:02", "Kitchen Scale", []),
        # Renamed device, matched on the advertised name.
        ("AA:00:00:00:00:03", "myOSSM", []),
        # Renamed past recognition, matched on the service UUID.
        ("AA:00:00:00:00:04", "Bench", [SERVICE_UUID]),
    ])

    found = {entry["address"] for entry in OSSMBleDevice.scan(timeout=0.01)}
    assert found == {"AA:00:00:00:00:01", "AA:00:00:00:00:03",
                     "AA:00:00:00:00:04"}


def test_write_failure_drops_the_link_instead_of_going_mute(linked):
    device, client = linked
    client.write_error = RuntimeError("gatt write failed")
    device.apply_ai_commands({"pattern": "simple_stroke", "speed": 40})

    # Holding a connection open that swallows every command would look healthy
    # in the UI while the machine ignored the session, so the link is dropped
    # and the reconnect loop builds a fresh one.
    assert wait_for(lambda: FakeBleakClient.instance_count() > 1, timeout=6)
    replacement = FakeBleakClient.latest()
    assert replacement is not client
    assert wait_for(lambda: bool(replacement.notify_callbacks), timeout=6)
