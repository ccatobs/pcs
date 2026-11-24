"""The window form of the constant-elevation scan dispatch core.

With ``az_min`` and ``az_max``, ``build_constant_el_payload`` builds through the
window form of ``fyst_trajectories.planning.plan_constant_el_scan``: the dish
sweeps the window at one elevation for ``duration`` seconds from the floored start. The
tests drive the real planner, recording its calls where they need them.
ocs-free: imports ``trajectory.py`` only.
"""

import warnings

import numpy as np
import pytest
from astropy.time import Time

from fyst_trajectories import get_fyst_site
from fyst_trajectories.coordinates import Coordinates
from fyst_trajectories.planning import FieldRegion

from pcs.agents.acu_interface import trajectory as traj_mod
from pcs.agents.acu_interface.trajectory import (
    SCAN_DISPATCH_BUFFER_SEC,
    DispatchDelayError,
    TrajectoryValidationError,
    build_constant_el_payload,
    unknown_scan_param_keys,
)

#: A night epoch (Sun at el -55 at FYST).
FIXED_EPOCH = Time("2026-06-15T02:00:00", scale="utc")
FIXED_NOW = FIXED_EPOCH.unix

#: A daytime epoch at which the Sun stands at about el 50 in the east at FYST.
SUN_EPOCH = Time("2026-11-07T13:26:00", scale="utc")

#: A survey row with no sky box: a window, an elevation and a duration (s).
SURVEY_ROW = dict(
    az_min=26.0,
    az_max=154.0,
    elevation=50.0,
    velocity=0.8,
    duration=1300.0,
    az_accel=0.8,
    timestep=0.2,
)

#: The four field keys of the field form.
FIELD = dict(ra_center=80.0, dec_center=-40.0, width=3.0, height=3.0)

#: Azimuth half-width (deg) of the window centred on the Sun: wide enough that
#: the scan's start, at el 50, sits outside the 45 deg exclusion zone.
SUN_WINDOW_HALF_WIDTH = 90.0


@pytest.fixture
def site():
    # Sun avoidance disabled, so the geometry alone decides each build.
    return get_fyst_site(sun_avoidance_enabled=False)


@pytest.fixture
def planner_calls(monkeypatch):
    """Record each call of the planner, which still runs for real."""
    calls = []
    real = traj_mod.plan_constant_el_scan

    def recording(*args, **kwargs):
        block = real(*args, **kwargs)
        calls.append({"args": args, "kwargs": kwargs, "block": block})
        return block

    monkeypatch.setattr(traj_mod, "plan_constant_el_scan", recording)
    return calls


def _build(scan_params, site, *, now=FIXED_NOW, scheduled_t0_unix=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return build_constant_el_payload(
            scan_params=scan_params,
            current_az=90.0,
            current_el=50.0,
            site=site,
            scheduled_t0_unix=scheduled_t0_unix,
            now_unix=now,
        )


def _leg_sec():
    return (SURVEY_ROW["az_max"] - SURVEY_ROW["az_min"]) / SURVEY_ROW["velocity"]


def test_a_survey_row_without_a_field_sweeps_its_window(site, planner_calls):
    result = _build(SURVEY_ROW, site)
    payload = result["payload"]
    points = np.asarray(payload["points"])
    # The library's turnaround overshoots each end by 5 v^2 / (8 a) and lasts 2 v / a.
    overshoot = 5.0 * SURVEY_ROW["velocity"] ** 2 / (8.0 * SURVEY_ROW["az_accel"])

    assert points[:, 1].min() >= SURVEY_ROW["az_min"] - overshoot - 1e-9
    assert points[:, 1].max() <= SURVEY_ROW["az_max"] + overshoot + 1e-9
    assert points[:, 1].min() <= SURVEY_ROW["az_min"]
    assert points[:, 1].max() >= SURVEY_ROW["az_max"]
    assert np.all(points[:, 2] == SURVEY_ROW["elevation"])
    assert points[0][1] == result["encoder_az"]
    assert payload["start_time"] == FIXED_NOW + SCAN_DISPATCH_BUFFER_SEC

    (call,) = planner_calls
    block = call["block"]
    timestep = call["kwargs"]["timestep"]
    length = points[-1][0] - points[0][0]
    assert abs(length - block.duration) <= timestep
    turnaround = 2.0 * SURVEY_ROW["velocity"] / SURVEY_ROW["az_accel"]
    assert abs(block.duration - SURVEY_ROW["duration"]) <= _leg_sec() + turnaround


def test_a_scheduled_start_well_ahead_is_kept_literally(site):
    scheduled = FIXED_NOW + 600.0
    result = _build(SURVEY_ROW, site, scheduled_t0_unix=scheduled)
    assert result["payload"]["start_time"] == scheduled


def test_the_window_form_forwards_the_window_and_its_defaults(site, planner_calls):
    _build(SURVEY_ROW, site)
    (call,) = planner_calls
    assert call["args"] == (None,)
    kwargs = call["kwargs"]
    assert kwargs["az_window"] == (SURVEY_ROW["az_min"], SURVEY_ROW["az_max"])
    assert kwargs["duration"] == SURVEY_ROW["duration"]
    assert kwargs["elevation"] == SURVEY_ROW["elevation"]
    assert kwargs["velocity"] == SURVEY_ROW["velocity"]
    assert kwargs["az_accel"] == SURVEY_ROW["az_accel"]
    assert kwargs["timestep"] == SURVEY_ROW["timestep"]
    assert kwargs["az_padding"] == 0.0
    assert kwargs["start_time"].unix == FIXED_NOW + SCAN_DISPATCH_BUFFER_SEC


def test_an_explicit_az_padding_is_forwarded(site, planner_calls):
    _build(dict(SURVEY_ROW, az_padding=1.0), site)
    (call,) = planner_calls
    assert call["kwargs"]["az_padding"] == 1.0


def test_a_far_scheduled_start_is_refused_naming_the_window(site):
    with pytest.raises(DispatchDelayError, match="window"):
        _build(SURVEY_ROW, site, scheduled_t0_unix=FIXED_NOW + 7200.0)


def test_a_duration_shorter_than_one_leg_is_refused(site, planner_calls):
    """A duration given in hours (12 for a 12 h row) would otherwise run one leg."""
    with pytest.raises(ValueError, match="shorter than one leg") as excinfo:
        _build(dict(SURVEY_ROW, duration=12), site)
    assert "duration 12 s" in str(excinfo.value)
    assert f"({_leg_sec():.1f} s)" in str(excinfo.value)
    assert planner_calls == []


def test_a_duration_of_exactly_one_leg_builds(site, planner_calls):
    _build(dict(SURVEY_ROW, duration=_leg_sec()), site)
    (call,) = planner_calls
    assert call["kwargs"]["duration"] == _leg_sec()


#: A 40 deg window swept at 1 deg/s: one leg is 40 s.
ONE_LEG_ROW = dict(elevation=50.0, velocity=1.0, duration=40.0, az_accel=1.0, timestep=0.2)
ONE_LEG_WIDTH = 40.0


@pytest.mark.parametrize(
    "az_min, rounds",
    [(93.3, "over"), (20.0, "exact"), (60.1, "under")],
)
def test_a_duration_of_one_leg_builds_however_the_width_rounds(site, planner_calls, az_min, rounds):
    width = (az_min + ONE_LEG_WIDTH) - az_min
    assert {"over": width > ONE_LEG_WIDTH, "exact": width == ONE_LEG_WIDTH,
            "under": width < ONE_LEG_WIDTH}[rounds]
    _build(dict(ONE_LEG_ROW, az_min=az_min, az_max=az_min + ONE_LEG_WIDTH), site)
    (call,) = planner_calls
    turnaround = 2.0 * ONE_LEG_ROW["velocity"] / ONE_LEG_ROW["az_accel"]
    assert call["block"].duration <= ONE_LEG_ROW["duration"] + turnaround


def test_a_duration_just_short_of_one_leg_is_still_refused(site, planner_calls):
    short = ONE_LEG_ROW["duration"] * (1.0 - 1e-6)
    with pytest.raises(ValueError, match="shorter than one leg"):
        _build(dict(ONE_LEG_ROW, az_min=20.0, az_max=20.0 + ONE_LEG_WIDTH, duration=short), site)
    assert planner_calls == []


def test_all_four_field_keys_are_forwarded_as_a_field_region(site, planner_calls):
    _build(dict(SURVEY_ROW, **FIELD), site)
    (call,) = planner_calls
    (field,) = call["args"]
    assert isinstance(field, FieldRegion)
    assert (field.ra_center, field.dec_center, field.width, field.height) == (
        FIELD["ra_center"], FIELD["dec_center"], FIELD["width"], FIELD["height"])


@pytest.mark.parametrize("missing", sorted(FIELD))
def test_a_partial_field_is_refused_naming_the_four_keys(site, planner_calls, missing):
    partial = {key: value for key, value in FIELD.items() if key != missing}
    with pytest.raises(ValueError) as excinfo:
        _build(dict(SURVEY_ROW, **partial), site)
    for key in FIELD:
        assert key in str(excinfo.value)
    assert planner_calls == []


@pytest.mark.parametrize("dropped, kept", [("az_max", "az_min"), ("az_min", "az_max")])
def test_half_a_window_is_refused_naming_both_keys(site, dropped, kept):
    params = {key: value for key, value in SURVEY_ROW.items() if key != dropped}
    # With the field keys the rest is a buildable crossing-form dictionary, so a missing
    # check would ignore the half window rather than fail elsewhere.
    params.update(FIELD)
    with pytest.raises(ValueError, match="az_min and az_max") as excinfo:
        _build(params, site)
    assert f"got only {kept}" in str(excinfo.value)


def test_an_lsa_window_beside_the_window_is_refused(site, planner_calls):
    with pytest.raises(ValueError, match="lsa_window"):
        _build(dict(SURVEY_ROW, lsa_window=[60.0, 100.0]), site)
    assert planner_calls == []


def test_a_window_without_duration_is_refused_naming_it(site, planner_calls):
    params = {key: value for key, value in SURVEY_ROW.items() if key != "duration"}
    with pytest.raises(ValueError, match="duration"):
        _build(params, site)
    assert planner_calls == []


def test_crossing_search_keys_are_not_forwarded_and_are_named(site, planner_calls):
    """The window form runs no crossing search, so the four keys that steer it
    are not forwarded and are named as ignored, like a key no form reads."""
    params = dict(SURVEY_ROW, rising=False, angle=30.0, max_search_hours=2.0, step_seconds=10.0)
    _build(params, site)
    (call,) = planner_calls
    for key in ("rising", "angle", "max_search_hours", "step_seconds"):
        assert key not in call["kwargs"]
    assert unknown_scan_param_keys("constant_el_scan", params) == [
        "angle", "max_search_hours", "rising", "step_seconds"]


def test_a_crossing_search_key_given_as_none_is_not_named():
    params = dict(SURVEY_ROW, rising=None, angle=None, max_search_hours=None, step_seconds=None)
    assert unknown_scan_param_keys("constant_el_scan", params) == []


def _sun_window():
    """A window at the Sun's elevation, centred on its azimuth, at the scan start."""
    start = Time(SUN_EPOCH.unix + SCAN_DISPATCH_BUFFER_SEC, format="unix")
    sun_az, sun_el = Coordinates(get_fyst_site()).get_sun_altaz(start)
    sun_az, sun_el = float(np.asarray(sun_az)), float(np.asarray(sun_el))
    return dict(
        az_min=sun_az - SUN_WINDOW_HALF_WIDTH,
        az_max=sun_az + SUN_WINDOW_HALF_WIDTH,
        elevation=sun_el,
        velocity=1.0,
        duration=600.0,
        az_accel=1.0,
    )


def test_a_window_across_the_sun_is_refused():
    with pytest.raises(TrajectoryValidationError, match="Sun exclusion zone") as excinfo:
        _build(_sun_window(), get_fyst_site(), now=SUN_EPOCH.unix)
    assert "Constant-el window" in str(excinfo.value)


def test_the_same_window_builds_with_sun_avoidance_disabled(site):
    result = _build(_sun_window(), site, now=SUN_EPOCH.unix)
    assert result["payload"]["start_time"] == SUN_EPOCH.unix + SCAN_DISPATCH_BUFFER_SEC
