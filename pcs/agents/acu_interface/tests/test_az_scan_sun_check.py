"""az_scan refuses, without sending it, an azimuth scan whose pattern passes
through the Sun zone or whose pattern keys are malformed.

The Go TCS's /azimuth-scan holds the dish at the range start until start_time and
then sweeps the range (NewAzimuthScanPattern at TCS b7bbbf9); neither it nor pcs
otherwise checks the pattern against the Sun, so the task checks the arc from now
to the pattern's end with trajectory.az_scan_sun_breach.
"""

import re
import time
import types

import numpy as np
import pytest
from astropy.time import Time

from fyst_trajectories import get_fyst_site
from fyst_trajectories.coordinates import Coordinates

from pcs.agents.acu_interface.tests.test_az_scan_start_time import (
    _AGENT,
    _FakeLock,
    _Log,
    _RecordingTCS,
)
from pcs.agents.acu_interface.trajectory import az_scan_duration_sec, az_scan_sun_breach

pytestmark = pytest.mark.skipif(_AGENT is None, reason="agent module not importable")

#: An azimuth scan commanded on 2026-11-07 at 13:03:54 UTC: its 1800 s pattern
#: from az 0 to 180 at el 50 passes through the Sun's position, which reaches
#: el 50 in the east at about 13:26 UTC that day at FYST.
SUN_CROSSING_SCAN = {
    "azimuth_range": [0.0, 180.0],
    "elevation": 50.0,
    "speed": 1.0,
    "num_scans": 5,
    "turnaround_time": 0.0,
    "start_time": 10.0,
}
SUN_CROSSING_COMMANDED = Time("2026-11-07T13:03:54", scale="utc").unix

#: The same scan commanded just after sunset, the evening before.
NIGHT_COMMANDED = Time("2026-11-06T22:47:42", scale="utc").unix

#: A narrow arc at el 50 the rising Sun approaches through 13:26 UTC on 2026-11-07.
EAST_ARC = {"azimuth_range": [85.0, 95.0], "elevation": 50.0, "speed": 1.0,
            "num_scans": 1, "turnaround_time": 0.0}
EAST_ARC_DURATION_SEC = 20.0

#: A narrow arc at el 50 in the west, whose pattern lasts 20 s; the Sun, near
#: transit, comes within the 45 deg radius of it at about 15:59:38 UTC on 2026-11-07.
WEST_ARC = {"azimuth_range": [250.0, 260.0], "elevation": 50.0, "speed": 1.0,
            "num_scans": 1, "turnaround_time": 0.0}
WEST_ARC_DURATION_SEC = 20.0

#: Commanded 135 s before that entry, so the pattern's nominal end (now plus 20 s)
#: falls 115 s before it.
WEST_ARC_COMMANDED = Time("2026-11-07T15:57:23", scale="utc").unix

_BREACH_TIME = re.compile(r"of the Sun at (\S+)Z \(")


@pytest.fixture
def run_az_scan(monkeypatch):
    """Run the real az_scan at a fixed wall clock; return its result and the recorder."""
    tcs = _RecordingTCS()
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system", lambda *a, **k: tcs)
    agent = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    agent.log = _Log()
    agent.azel_lock = _FakeLock()
    agent.acu_conf = {
        "base_url": "http://tcs.invalid",
        "certs": {"server_cert": "", "client_cert": "", "client_key": "", "verify": False},
    }

    def run(scan_params, now_unix):
        monkeypatch.setattr(_AGENT, "time",
                            types.SimpleNamespace(time=lambda: now_unix, sleep=time.sleep))
        return agent.az_scan(None, {"scan_params": dict(scan_params)}), tcs

    return run


def _breach_unix(message):
    return Time(_BREACH_TIME.search(message).group(1), scale="utc").unix


def test_a_sweep_through_the_sun_is_refused_and_not_sent(run_az_scan):
    (ok, msg), tcs = run_az_scan(SUN_CROSSING_SCAN, SUN_CROSSING_COMMANDED)
    assert ok is False
    assert tcs.scans == []
    assert "Sun" in msg and "not launched" in msg, msg
    assert SUN_CROSSING_COMMANDED < _breach_unix(msg) < SUN_CROSSING_COMMANDED + 1810.0


def test_the_same_sweep_at_night_is_sent_unchanged(run_az_scan):
    (ok, _), tcs = run_az_scan(SUN_CROSSING_SCAN, NIGHT_COMMANDED)
    assert ok is True
    assert tcs.scans == [SUN_CROSSING_SCAN]


def test_a_site_without_sun_avoidance_checks_nothing_and_sends(run_az_scan, monkeypatch):
    no_sun_site = get_fyst_site(sun_avoidance_enabled=False)
    monkeypatch.setattr(_AGENT, "get_fyst_site", lambda *a, **k: no_sun_site)
    (ok, _), tcs = run_az_scan(SUN_CROSSING_SCAN, SUN_CROSSING_COMMANDED)
    assert ok is True
    assert tcs.scans == [SUN_CROSSING_SCAN]


@pytest.mark.parametrize("change, named", [
    ({"num_scans": None}, "num_scans is missing"),
    ({"azimuth_range": [0.0]}, "azimuth_range: 1 values"),
    ({"speed": "fast"}, "speed: "),
    ({"elevation": float("nan")}, "elevation: not finite"),
    ({"num_scans": 2.5}, "num_scans: not a whole number"),
], ids=["num_scans-missing", "one-azimuth", "speed-text", "elevation-nan", "num_scans-fraction"])
def test_a_malformed_pattern_is_refused_and_not_sent(run_az_scan, change, named):
    scan = {key: value for key, value in dict(SUN_CROSSING_SCAN, **change).items()
            if value is not None}
    (ok, msg), tcs = run_az_scan(scan, NIGHT_COMMANDED)
    assert ok is False
    assert tcs.scans == []
    assert "not launched" in msg, msg
    assert named in msg, msg


def test_a_relative_start_is_read_from_now_and_an_absolute_one_from_itself(run_az_scan):
    """The Sun nears the arc throughout, so the closest approach is the pattern's
    end: the relative start's end follows the clock, the absolute one's its value."""
    now = Time("2026-11-07T13:00:00", scale="utc").unix
    absolute = Time("2026-11-07T13:15:00", scale="utc").unix

    (ok, msg), _ = run_az_scan(dict(EAST_ARC, start_time=600.0), now)
    assert ok is False
    assert _breach_unix(msg) == pytest.approx(now + 600.0 + EAST_ARC_DURATION_SEC, abs=0.01)

    (ok, msg), _ = run_az_scan(dict(EAST_ARC, start_time=absolute), now)
    assert ok is False
    assert _breach_unix(msg) == pytest.approx(absolute + EAST_ARC_DURATION_SEC, abs=0.01)


def test_a_zero_start_allows_for_the_tcs_move(run_az_scan):
    """A 0 starts after the TCS's own move, so the check runs to SLEW_TIMEOUT_SEC
    past now plus the pattern; the Sun enters the zone 115 s after the pattern's
    nominal end here. A relative 10 s start ends 105 s before it and is sent."""
    nominal = az_scan_sun_breach(
        **{k: WEST_ARC[k] for k in ("azimuth_range", "elevation")},
        start_unix=WEST_ARC_COMMANDED, duration_sec=WEST_ARC_DURATION_SEC,
        now_unix=WEST_ARC_COMMANDED, site=get_fyst_site())
    assert nominal is None

    (ok, msg), tcs = run_az_scan(dict(WEST_ARC, start_time=0.0), WEST_ARC_COMMANDED)
    assert ok is False and tcs.scans == [], msg
    assert _breach_unix(msg) == pytest.approx(
        WEST_ARC_COMMANDED + _AGENT.SLEW_TIMEOUT_SEC + WEST_ARC_DURATION_SEC, abs=0.01)

    (ok, _), tcs = run_az_scan(dict(WEST_ARC, start_time=10.0), WEST_ARC_COMMANDED)
    assert ok is True and len(tcs.scans) == 1


def test_the_breach_check_finds_the_sun_entering_after_the_start():
    site = get_fyst_site()
    start = Time("2026-11-07T12:00:00", scale="utc").unix
    arc = {"azimuth_range": [250.0, 270.0], "elevation": 50.0}
    assert az_scan_sun_breach(**arc, start_unix=start, duration_sec=0.0, now_unix=start,
                              site=site) is None

    breach = az_scan_sun_breach(**arc, start_unix=start, duration_sec=8 * 3600.0,
                                now_unix=start, site=site)
    assert breach is not None
    assert Time(breach["time"].rstrip("Z"), scale="utc").unix > start
    assert breach["separation"] <= breach["radius"] == site.sun_avoidance.exclusion_radius


def test_the_sun_on_the_arc_is_at_zero_separation():
    site = get_fyst_site()
    when = Time("2026-11-07T13:26:00", scale="utc")
    sun_az, sun_el = Coordinates(site).get_sun_altaz(when)
    sun_az, sun_el = float(np.asarray(sun_az)), float(np.asarray(sun_el))
    breach = az_scan_sun_breach(
        azimuth_range=[sun_az - 5.0, sun_az + 5.0], elevation=sun_el,
        start_unix=when.unix, duration_sec=0.0, now_unix=when.unix, site=site)
    assert breach is not None
    assert breach["separation"] < 1e-6
    assert breach["az"] == pytest.approx(sun_az)


@pytest.mark.parametrize("azimuth_range, speed, num_scans, turnaround_time, expected", [
    ([0.0, 180.0], 1.0, 5, 0.0, 1800.0),
    ([26.0, 154.0], 1.0, 5, 2.0, 1300.0),
    ([140.0, 100.0], 0.5, 2, 2.0, 328.0),
])
def test_the_duration_follows_the_tcs_azimuth_scan_pattern(azimuth_range, speed, num_scans,
                                                           turnaround_time, expected):
    assert az_scan_duration_sec(azimuth_range, speed, num_scans, turnaround_time) == expected
