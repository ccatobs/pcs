"""The Sun checks of a typed scan's build: the dwell at the slew target and every sample.

``trajectory._choose_slew_and_align``, the step every typed scan core uses to
pick its encoder slew target, must find the chosen azimuth wrap sun-safe over
the whole dwell from dispatch to the resolved start, since the dish holds the
scan's start position until ``start_time``: ``_dwell_check_times`` samples the
dwell at most ``SUN_DWELL_CHECK_STEP_SEC`` apart and hands the samples to
``fyst_trajectories.dispatch.choose_encoder_solution`` as an array obstime. The
slew path itself is not checked. The four builders must hand the step the
dispatch time.

Each builder also checks every sample of the built trajectory against the Sun
exclusion zone (``sun_exclusion_breach``), since the library's
``validate_sun_avoidance`` checks one about every 60 s and only warns.

ocs-free: imports ``trajectory.py`` only.
"""

import warnings

import numpy as np
import pytest
from astropy.coordinates import get_sun
from astropy.time import Time

from fyst_trajectories import get_fyst_site
from fyst_trajectories.coordinates import Coordinates
from fyst_trajectories.exceptions import PointingError
from fyst_trajectories.trajectory import Trajectory

from pcs.agents.acu_interface import trajectory as traj_mod
from pcs.agents.acu_interface.trajectory import (
    SUN_DWELL_CHECK_STEP_SEC,
    TrajectoryValidationError,
    _choose_slew_and_align,
    _dwell_check_times,
    build_constant_el_payload,
    build_daisy_payload,
    build_pong_payload,
    build_source_payload,
    sun_exclusion_breach,
)

# A fixed epoch so the resolved-start / now arithmetic is deterministic; the
# dwell-coverage logic is date-independent (it only compares the injected times).
FIXED_START = Time("2026-06-15T02:00:00", scale="utc")

#: A dwell of four check steps, so five check times.
DWELL_SEC = 4 * SUN_DWELL_CHECK_STEP_SEC


@pytest.fixture
def site():
    # Sun avoidance disabled: the default-Sun-model test wants a clearly
    # safe geometry, and the recording/injected-predicate tests supply their own
    # sun_safe so the site's own model is irrelevant to them.
    return get_fyst_site(sun_avoidance_enabled=False)


def _flat_traj(az_deg, el_deg, start):
    """A trivial constant az/el trajectory whose first sample is (az, el)."""
    t = np.arange(0.0, 2.0, 0.1)
    return Trajectory(
        times=t,
        az=np.full_like(t, az_deg),
        el=np.full_like(t, el_deg),
        az_vel=np.zeros_like(t),
        el_vel=np.zeros_like(t),
        start_time=start,
    )


def _recording_sun_safe():
    """A ``sun_safe`` that answers safe and records each time it is asked about."""
    queried = []

    def sun_safe(az_deg, el_deg, time):
        queried.append(float(time.unix))
        return True

    return queried, sun_safe


def test_dwell_check_times_span_endpoints_and_step_bound():
    """A dwell of four check steps gives one array-valued Time of five check
    times from now to start, no gap wider than the step."""
    now = float(FIXED_START.unix) - DWELL_SEC
    times = _dwell_check_times(now, FIXED_START)
    assert not times.isscalar
    unix = times.unix
    assert unix[0] == pytest.approx(now, abs=1e-3)
    assert unix[-1] == pytest.approx(FIXED_START.unix, abs=1e-3)
    assert len(times) == 5
    assert np.diff(unix).max() <= SUN_DWELL_CHECK_STEP_SEC + 1e-6


def test_slew_target_sun_checked_across_whole_dwell(site):
    """A recording ``sun_safe`` is queried at BOTH dwell endpoints and at the
    expected intermediate samples, not only at the resolved start.

    az=120 has a single in-range encoder image at FYST, so the predicate is
    invoked exactly once per dwell check time; a dwell of four steps is five
    check times (now, start, and 3 intermediates).
    """
    now = float(FIXED_START.unix) - DWELL_SEC
    traj = _flat_traj(120.0, 45.0, FIXED_START)
    queried, recording_sun_safe = _recording_sun_safe()

    enc_az, enc_el, aligned = _choose_slew_and_align(
        traj,
        current_az=120.0,
        current_el=45.0,
        start=FIXED_START,
        site=site,
        sun_safe=recording_sun_safe,
        now_unix=now,
    )

    unique_times = sorted(set(round(t, 3) for t in queried))
    assert round(now, 3) in unique_times, "dwell START (now) was not sun-checked"
    assert round(float(FIXED_START.unix), 3) in unique_times, "resolved start not checked"
    assert len(unique_times) == 5
    # The chosen wrap is the (single) in-range image; alignment holds.
    assert aligned.az[0] == pytest.approx(enc_az, abs=1e-9)


def test_wrap_unsafe_at_now_is_rejected(site):
    """A wrap safe at the resolved start but not earlier in the dwell is refused
    with ``PointingError``, which a check at the start alone would miss."""
    now = float(FIXED_START.unix) - DWELL_SEC
    traj = _flat_traj(120.0, 45.0, FIXED_START)

    def unsafe_before_start(az_deg, el_deg, time):
        return float(time.unix) >= float(FIXED_START.unix) - 1.0

    with pytest.raises(PointingError, match=r"sun-safe azimuth wrap"):
        _choose_slew_and_align(
            traj,
            current_az=120.0,
            current_el=45.0,
            start=FIXED_START,
            site=site,
            sun_safe=unsafe_before_start,
            now_unix=now,
        )


def test_start_at_or_before_now_is_single_time(site):
    """With no dwell (start at or before now) the Sun is checked once, at
    ``start``."""
    # start at now, and start slightly before now: both are "no dwell".
    for now in (float(FIXED_START.unix), float(FIXED_START.unix) + 50.0):
        times = _dwell_check_times(now, FIXED_START)
        assert len(times) == 1
        assert times[0].unix == pytest.approx(FIXED_START.unix, abs=1e-3)

    traj = _flat_traj(120.0, 45.0, FIXED_START)
    queried, recording_sun_safe = _recording_sun_safe()

    _choose_slew_and_align(
        traj,
        current_az=120.0,
        current_el=45.0,
        start=FIXED_START,
        site=site,
        sun_safe=recording_sun_safe,
        now_unix=float(FIXED_START.unix),  # start == now, no dwell
    )
    # Single candidate az, single check time -> exactly one predicate call.
    assert len(queried) == 1
    assert queried[0] == pytest.approx(FIXED_START.unix, abs=1e-3)


def test_the_default_sun_model_picks_a_wrap(site):
    """With ``sun_safe=None`` the wrap pick works end to end: the library binds
    its default scalar Sun model (always safe on this sun-avoidance-disabled
    site) and applies it across the dwell.
    """
    now = float(FIXED_START.unix) - DWELL_SEC
    traj = _flat_traj(120.0, 45.0, FIXED_START)
    enc_az, enc_el, aligned = _choose_slew_and_align(
        traj,
        current_az=120.0,
        current_el=45.0,
        start=FIXED_START,
        site=site,
        sun_safe=None,
        now_unix=now,
    )
    assert enc_el == pytest.approx(45.0)
    assert aligned.az[0] == pytest.approx(enc_az, abs=1e-9)


def test_wrap_choice_respects_full_trajectory_span(site):
    """The wrap pick must keep the WHOLE trajectory az span inside the limits.

    A north-crossing scan built in the [-20, 10] frame has encoder images of its
    first sample at -20 and 340. From current az 300 the minimum-slew image is
    340, but shifting the whole trajectory there puts its span at [340, 370],
    past the +360 limit. Forwarding the span to ``choose_encoder_solution``
    makes the 340 wrap inadmissible, so the -20 wrap (which fits whole) is
    chosen despite the longer slew; a first-sample-only choice would pick 340.
    """
    t = np.arange(0.0, 2.0, 0.1)
    traj = Trajectory(
        times=t,
        az=np.linspace(-20.0, 10.0, t.size),
        el=np.full_like(t, 45.0),
        az_vel=np.zeros_like(t),
        el_vel=np.zeros_like(t),
        start_time=FIXED_START,
    )
    enc_az, _enc_el, aligned = _choose_slew_and_align(
        traj,
        current_az=300.0,
        current_el=45.0,
        start=FIXED_START,
        site=site,
        sun_safe=None,
        now_unix=float(FIXED_START.unix),
    )
    assert enc_az == pytest.approx(-20.0)
    assert aligned.az[0] == pytest.approx(-20.0)
    assert float(aligned.az.max()) <= 360.0


# ---------------------------------------------------------------------------
# The builders hand the slew-target step the dispatch time.
# ---------------------------------------------------------------------------

# Per builder: scan parameters whose resolved start falls minutes after the
# dispatch, the dispatch epoch, and the scheduled start's lead after it (None to
# dispatch as soon as the buffer allows). The constant-el crossing and the
# source pass resolve minutes out; the pong and the daisy are scheduled 10 min
# out.
_BUILDER_CASES = {
    "constant_el": (
        build_constant_el_payload,
        dict(ra_center=80.0, dec_center=-40.0, width=3.0, height=3.0, elevation=50.0,
             velocity=0.5, rising=True),
        Time("2026-06-15T13:00:00", scale="utc"),
        None,
    ),
    "source": (
        build_source_payload,
        dict(body="jupiter", el_bore=37.0, mode="rising"),
        Time("2026-06-15T16:30:00", scale="utc"),
        None,
    ),
    "pong": (
        build_pong_payload,
        dict(ra_center=80.0, dec_center=-40.0, width=2.0, height=2.0, velocity=0.5,
             spacing=0.1, num_terms=4),
        Time("2026-06-15T13:00:00", scale="utc"),
        600.0,
    ),
    "daisy": (
        build_daisy_payload,
        dict(ra=80.0, dec=-40.0, radius=0.5, velocity=0.3, turn_radius=0.2,
             avoidance_radius=0.0, start_acceleration=0.5, duration=120.0),
        Time("2026-06-15T13:00:00", scale="utc"),
        600.0,
    ),
}


def _build(case, site, **extra):
    builder, params, epoch, lead = _BUILDER_CASES[case]
    now = float(epoch.unix)
    return builder(
        scan_params=params,
        current_az=200.0,
        current_el=60.0,
        site=site,
        scheduled_t0_unix=None if lead is None else now + lead,
        now_unix=now,
        **extra,
    )


@pytest.mark.parametrize("case", sorted(_BUILDER_CASES))
def test_builder_checks_the_sun_from_dispatch_to_the_start(site, case):
    """Each builder checks the Sun over the whole time the dish holds the start
    pose: the earliest check is at the dispatch ``now_unix``, minutes before the
    resolved start, the latest at the posted start, and no two checks are more
    than ``SUN_DWELL_CHECK_STEP_SEC`` apart. A builder that handed the step the
    trajectory's start in place of the dispatch time would check the start
    alone and miss the Sun crossing the held pose before it."""
    _builder, _params, epoch, _lead = _BUILDER_CASES[case]
    now = float(epoch.unix)
    queried, recording_sun_safe = _recording_sun_safe()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _build(case, site, sun_safe=recording_sun_safe)  # default max_dispatch_delay_sec
    start = result["payload"]["start_time"]
    assert start - now > SUN_DWELL_CHECK_STEP_SEC, "the case must hold the pose for several minutes"
    times = np.array(sorted(set(round(t, 3) for t in queried)))
    assert times[0] == pytest.approx(now, abs=1e-3), "the Sun check does not start at dispatch"
    assert times[-1] == pytest.approx(start, abs=1e-3), "the Sun check does not end at the start"
    assert np.diff(times).max() <= SUN_DWELL_CHECK_STEP_SEC + 1e-3


# ---------------------------------------------------------------------------
# The trajectory's own Sun check: the builders refuse a trajectory with any
# sample inside the exclusion zone (sun_exclusion_breach), since the library's
# validate_sun_avoidance checks one about every 60 s and only warns.
# ---------------------------------------------------------------------------

_BREACH = {"time": "2026-06-15T13:10:00Z", "az": 120.0, "el": 45.0, "separation": 40.0,
           "radius": 45.0, "count": 7}


def _breach_found(monkeypatch, breach):
    """Make the builders' every-sample Sun check report ``breach`` (``None`` for none)."""
    monkeypatch.setattr(traj_mod, "sun_exclusion_breach", lambda traj, site: breach)


@pytest.mark.parametrize("case", sorted(_BUILDER_CASES))
def test_a_trajectory_inside_the_sun_exclusion_zone_fails_the_build(site, monkeypatch, case):
    """Every builder turns a sample inside the zone, as the every-sample check
    reports it, into a failed build, a ``TrajectoryValidationError`` that names
    the zone and the first sample inside, so the path is never POSTed."""
    _breach_found(monkeypatch, _BREACH)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TrajectoryValidationError, match="Sun exclusion zone") as info:
            _build(case, site)
    assert "First sample inside: boresight az=120.00 deg, el=45.00 deg at 2026-06-15T13:10:00Z" in str(info.value)
    assert "40.0 deg from the Sun (exclusion radius 45.0 deg); 7 of" in str(info.value)


@pytest.mark.parametrize("case", sorted(_BUILDER_CASES))
def test_a_clear_trajectory_builds(site, monkeypatch, case):
    """With no sample inside the zone the build goes through."""
    _breach_found(monkeypatch, None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = _build(case, site)
    assert result["payload"]["points"]


def test_the_every_sample_check_catches_a_short_excursion_between_subsamples():
    """A trajectory that sits far from the Sun but dips into the zone for five
    seconds, thirty seconds in, is caught by the every-sample check with the
    excursion's first sample and its sample count; the library's own check
    samples about every 60 s and can miss it. On a site without Sun avoidance
    the check reports nothing."""
    site = get_fyst_site()
    start = Time("2026-06-15T16:30:00", scale="utc")  # the Sun about 44 deg up at the site
    sun_az, sun_el = Coordinates(site).get_sun_altaz(start)
    t = np.arange(0.0, 120.0, 0.1)
    az = np.full_like(t, float(sun_az) + 180.0)  # opposite the Sun in azimuth: clear
    el = np.full_like(t, float(sun_el))
    dip = (t >= 30.0) & (t < 35.0)
    az[dip] = float(sun_az)
    traj = Trajectory(times=t, az=az, el=el, az_vel=np.zeros_like(t), el_vel=np.zeros_like(t),
                      start_time=start)
    breach = sun_exclusion_breach(traj, site)
    assert breach is not None
    assert breach["count"] == int(dip.sum()) == 50
    assert breach["time"] == "2026-06-15T16:30:30Z"
    # The dip sits at the Sun's position at the START; thirty seconds later the Sun
    # has moved about 0.1 deg, which is what the separation reports.
    assert breach["separation"] < 0.5 and breach["radius"] == 45.0
    assert sun_exclusion_breach(traj, get_fyst_site(sun_avoidance_enabled=False)) is None
    az[dip] = float(sun_az) + 180.0
    clear = Trajectory(times=t, az=az, el=el, az_vel=np.zeros_like(t), el_vel=np.zeros_like(t),
                       start_time=start)
    assert sun_exclusion_breach(clear, site) is None


def test_a_pong_aimed_at_the_sun_is_refused_by_the_site_zone():
    """End to end with the library's own check: a pong field at the Sun's
    position near local noon (the Sun about 44 deg up at the site), with the
    slew target's own check bypassed so the trajectory check decides, is refused
    on the site with its 45 deg zone, naming the first sample inside; the same
    field builds on a site without Sun avoidance."""
    epoch = Time("2026-06-15T16:30:00", scale="utc")
    sun = get_sun(epoch)
    params = dict(ra_center=float(sun.ra.deg), dec_center=float(sun.dec.deg), width=2.0,
                  height=2.0, velocity=0.5, spacing=0.1, num_terms=4)
    kwargs = dict(scan_params=params, current_az=200.0, current_el=60.0,
                  sun_safe=lambda az_deg, el_deg, time: True, now_unix=float(epoch.unix))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TrajectoryValidationError, match="Sun exclusion zone") as info:
            build_pong_payload(site=get_fyst_site(), **kwargs)
        result = build_pong_payload(site=get_fyst_site(sun_avoidance_enabled=False), **kwargs)
    assert "First sample inside" in str(info.value)
    assert result["payload"]["points"]
