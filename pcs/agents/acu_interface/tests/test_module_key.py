"""The ``module`` key of the three field scans, and the pointing key each scan refuses.

``constant_el_scan`` (both forms), ``pong_scan`` and ``daisy_scan`` take an
optional ``module``, a Prime-Cam bay name or several, and plan the scan for that
position in the focal plane: the scan's own geometry keys say where the bay
looks, and the trajectory posted, the slew target and the Sun and limit checks
are the boresight's. ``source_scan`` is aimed with ``footprint`` instead, so each
builder refuses the other scans' pointing key before anything is planned. The
tests drive the real planners, recording their calls where they need them.
ocs-free: imports ``trajectory.py`` only.
"""

import inspect
import math
import warnings

import astropy.units as u
import numpy as np
import pytest
from astropy.time import Time

from fyst_trajectories import (
    boresight_to_detector,
    compute_focal_plane_rotation,
    get_fyst_site,
    get_primecam_offset,
)
from fyst_trajectories.coordinates import Coordinates
from fyst_trajectories.exceptions import (
    ElevationBoundsError,
    OffsetInversionError,
    PointingError,
    TargetNotObservableError,
)
from fyst_trajectories.planning import plan_constant_el_scan

from pcs.agents.acu_interface import trajectory as traj_mod
from pcs.agents.acu_interface.trajectory import (
    SCAN_DISPATCH_BUFFER_SEC,
    TrajectoryValidationError,
    build_constant_el_payload,
    build_daisy_payload,
    build_pong_payload,
    build_source_payload,
)

#: The night epoch of the constant-elevation tests (Sun at el -55 at FYST).
CE_EPOCH = Time("2026-06-15T02:00:00", scale="utc")

#: The epoch of the pong and daisy tests, at which their field is near el 45.
FIELD_EPOCH = Time("2026-06-15T13:00:00", scale="utc")

#: The field form's representative rising scan (``test_constant_el_scan.py``).
CE_FIELD_PARAMS = dict(
    ra_center=80.0, dec_center=-40.0, width=3.0, height=3.0,
    elevation=50.0, velocity=0.5, rising=True,
)

#: The window form's survey row (``test_constant_el_window_scan.py``).
CE_WINDOW_PARAMS = dict(
    az_min=26.0, az_max=154.0, elevation=50.0, velocity=0.8, duration=1300.0,
    az_accel=0.8, timestep=0.2,
)

#: The representative pong (``test_pong_scan.py``).
PONG_PARAMS = dict(
    ra_center=80.0, dec_center=-40.0, width=2.0, height=2.0,
    velocity=0.5, spacing=0.1, num_terms=4,
)

#: The representative daisy (``test_daisy_scan.py``).
DAISY_PARAMS = dict(
    ra=80.0, dec=-40.0, radius=0.5, velocity=0.3, turn_radius=0.2,
    avoidance_radius=0.0, start_acceleration=0.5, duration=120.0,
)

#: Each field scan: its builder, the planner it calls, its parameters, the
#: epoch and encoder pose its own tests build from, and the dispatch-delay bound
#: they pass.
CASES = {
    "ce_field": dict(
        builder=build_constant_el_payload, planner="plan_constant_el_scan",
        params=CE_FIELD_PARAMS, now=CE_EPOCH.unix, pose=(200.0, 60.0),
        max_delay=float("inf"), constant_el=True,
    ),
    "ce_window": dict(
        builder=build_constant_el_payload, planner="plan_constant_el_scan",
        params=CE_WINDOW_PARAMS, now=CE_EPOCH.unix, pose=(90.0, 50.0),
        max_delay=traj_mod.MAX_DISPATCH_DELAY_SEC, constant_el=True,
    ),
    "pong": dict(
        builder=build_pong_payload, planner="plan_pong_scan",
        params=PONG_PARAMS, now=FIELD_EPOCH.unix, pose=(200.0, 60.0),
        max_delay=float("inf"), constant_el=False,
    ),
    "daisy": dict(
        builder=build_daisy_payload, planner="plan_daisy_scan",
        params=DAISY_PARAMS, now=FIELD_EPOCH.unix, pose=(200.0, 60.0),
        max_delay=float("inf"), constant_el=False,
    ),
}

#: Two ring bays on opposite sides of the focal plane, neither on the elevation axis.
RING_BAYS = ["IM3", "IM6"]

#: Agreement (deg) of a boresight sample projected onto its bay with the sample
#: the scan without the key posts: the planner's inverse iteration exits near
#: 1e-12 deg, so this is loose by three orders and still far below a pointing error.
PROJECTION_TOL_DEG = 1e-9

#: Spread (deg) the boresight elevation of a constant-elevation scan may show
#: under a bay: the builder's velocity escalation relies on a fixed elevation.
CONSTANT_EL_TOL_DEG = 1e-9

#: Shift (deg) of the slew target's elevation that a ring bay off the
#: elevation axis exceeds by far (its bay sits 1.78 deg from the boresight).
MIN_RING_EL_SHIFT_DEG = 0.1

#: Agreement (deg) of an azimuth the slew is chosen for with the posted one,
#: which the builder moves by whole turns afterwards: a few ulps of 360.
AZ_WRAP_TOL_DEG = 1e-9


@pytest.fixture
def site():
    # Sun avoidance disabled, so the geometry alone decides each build.
    return get_fyst_site(sun_avoidance_enabled=False)


def _spy(monkeypatch, name):
    """Record the arguments of each call of ``name`` by parameter name; it still runs for real.

    A call is recorded before it runs, so one that raises is recorded too.
    """
    calls = []
    real = getattr(traj_mod, name)
    signature = inspect.signature(real)

    def recording(*args, **kwargs):
        calls.append(dict(signature.bind(*args, **kwargs).arguments))
        return real(*args, **kwargs)

    monkeypatch.setattr(traj_mod, name, recording)
    return calls


def _build(case, site, params, *, now=None):
    spec = CASES[case]
    current_az, current_el = spec["pose"]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return spec["builder"](
            scan_params=params,
            current_az=current_az,
            current_el=current_el,
            site=site,
            now_unix=spec["now"] if now is None else now,
            max_dispatch_delay_sec=spec["max_delay"],
        )


def _with(case, **keys):
    return dict(CASES[case]["params"], **keys)


def _points(result):
    return np.asarray(result["payload"]["points"], dtype=float)


def _turns_apart(a, b):
    """Return ``a - b`` (deg) less the nearest whole number of turns."""
    return (a - b + 180.0) % 360.0 - 180.0


# ---------------------------------------------------------------------------
# tracking_offset: the value of the key, resolved through the library.
# ---------------------------------------------------------------------------


def test_no_key_is_no_offset():
    assert traj_mod.tracking_offset(None) is None


@pytest.mark.parametrize("module", ["IM5", "im5", ["IM5"]])
def test_one_bay_in_any_case_is_that_bays_own_offset(module):
    assert traj_mod.tracking_offset(module) is get_primecam_offset("IM5")


@pytest.mark.parametrize(
    "module",
    ["IM0", "c", "center", "IM1,IM4", "IM2,IM5", "IM3,IM6", "IM1,IM3,IM5", "IM2,IM4,IM6", "all"],
)
def test_a_position_on_the_optical_axis_is_no_offset(module):
    """The on-axis bay in any spelling, an opposite pair and the whole array
    average to the boresight, which builds exactly as no key."""
    assert traj_mod.tracking_offset(module) is None


@pytest.mark.parametrize("module", [["IM5", "IM6"], "IM5,IM6", "IM5, IM5, IM6"])
def test_several_bays_are_their_mean_position_in_arcmin(module):
    """IM5 and IM6 mirror each other in cross-elevation, so their mean sits on
    the elevation axis at their shared dy. A mean in degrees is the same point,
    but passed as ``dy``, which is in arcmin, it would land 60 times closer to
    the boresight. A repeated bay counts once."""
    im5, im6 = get_primecam_offset("IM5"), get_primecam_offset("IM6")
    offset = traj_mod.tracking_offset(module)
    assert offset.dx == math.fsum([im5.dx, im6.dx]) / 2 == 0.0
    assert offset.dy == math.fsum([im5.dy, im6.dy]) / 2 == im5.dy
    assert abs(offset.dy) > 60.0  # arcmin; a mean of dy_deg would be about 1.5


def test_an_asymmetric_pair_is_the_mean_of_its_bay_centres_in_arcmin():
    im4, im5 = get_primecam_offset("IM4"), get_primecam_offset("IM5")
    offset = traj_mod.tracking_offset("IM4,IM5")
    assert offset.dx == math.fsum([im4.dx, im5.dx]) / 2
    assert offset.dy == math.fsum([im4.dy, im5.dy]) / 2
    assert offset.dx_deg == pytest.approx((im4.dx_deg + im5.dx_deg) / 2, abs=1e-12)
    assert offset.instrument_rotation == 0.0


@pytest.mark.parametrize(
    "module, error, match",
    [
        ("IM9", KeyError, "Unknown PrimeCam module"),
        ("i1", KeyError, "no longer defined"),
        ("", ValueError, "resolved to no modules"),
        ([], ValueError, "resolved to no modules"),
        (5, TypeError, "must be a str or sequence"),
        (True, TypeError, "must be a str or sequence"),
        ({}, TypeError, "must be a str or sequence"),
        (["IM5", 5], TypeError, "must be a str or sequence"),
    ],
    ids=["unknown", "retired-i1", "empty-str", "empty-list", "int", "bool", "dict",
         "int-in-list"],
)
def test_a_value_that_names_no_bay_raises_the_library_error(module, error, match):
    """The library's own refusal, unchanged: an empty value is refused, never
    read as no key."""
    with pytest.raises(error, match=match):
        traj_mod.tracking_offset(module)


# ---------------------------------------------------------------------------
# The builders: no key, None and IM0 build alike; a ring bay builds the
# boresight that puts that bay on the scan.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(CASES))
def test_no_key_none_and_the_on_axis_bay_build_the_same_scan(site, monkeypatch, case):
    calls = _spy(monkeypatch, CASES[case]["planner"])
    bare = _build(case, site, CASES[case]["params"])
    for params in (_with(case, module=None), _with(case, module="IM0")):
        result = _build(case, site, params)
        assert result["encoder_az"] == bare["encoder_az"]
        assert result["encoder_el"] == bare["encoder_el"]
        assert result["payload"] == bare["payload"]
    assert len(calls) == 3
    for kwargs in calls:
        assert "detector_offset" in kwargs
        assert kwargs["detector_offset"] is None


@pytest.fixture(scope="module")
def built():
    """Each case built without the key and under each ring bay, once."""
    site = get_fyst_site(sun_avoidance_enabled=False)
    results = {}
    for case in CASES:
        results[(case, None)] = _build(case, site, CASES[case]["params"])
        for bay in RING_BAYS:
            results[(case, bay)] = _build(case, site, _with(case, module=bay))
    return results


@pytest.mark.parametrize("bay", RING_BAYS)
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_ring_bay_reaches_the_planner_as_its_own_offset(site, monkeypatch, case, bay):
    calls = _spy(monkeypatch, CASES[case]["planner"])
    _build(case, site, _with(case, module=bay))
    (kwargs,) = calls
    assert kwargs["detector_offset"] is get_primecam_offset(bay)


@pytest.mark.parametrize("bay", RING_BAYS)
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_ring_bay_keeps_the_start_time_and_moves_the_trajectory(built, case, bay):
    bare, aimed = built[(case, None)], built[(case, bay)]
    assert aimed["payload"]["start_time"] == bare["payload"]["start_time"]
    assert aimed["payload"]["points"] != bare["payload"]["points"]


@pytest.mark.parametrize("bay", RING_BAYS)
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_boresight_projected_onto_the_bay_is_the_scan_without_the_key(
        built, site, case, bay):
    """Forward-projected with the focal-plane rotation at the boresight's own
    elevation, every posted sample lands on the bay's scan, which is the scan
    the same parameters post without the key."""
    bare, aimed = _points(built[(case, None)]), _points(built[(case, bay)])
    assert aimed.shape == bare.shape
    assert np.array_equal(aimed[:, 0], bare[:, 0])
    offset = get_primecam_offset(bay)
    az, el = aimed[:, 1], aimed[:, 2]
    rotation = compute_focal_plane_rotation(el, site=site, offset=offset)
    det_az, det_el = boresight_to_detector(az, el, offset, rotation)
    az_residual = (np.asarray(det_az) - bare[:, 1] + 180.0) % 360.0 - 180.0
    assert np.max(np.abs(np.asarray(det_el) - bare[:, 2])) < PROJECTION_TOL_DEG
    assert np.max(np.abs(az_residual)) < PROJECTION_TOL_DEG


@pytest.mark.parametrize("bay", RING_BAYS)
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_slew_target_is_the_boresights_first_sample(built, case, bay):
    bare, aimed = built[(case, None)], built[(case, bay)]
    first = aimed["payload"]["points"][0]
    assert aimed["encoder_az"] == first[1]
    assert aimed["encoder_el"] == first[2]
    assert abs(aimed["encoder_el"] - bare["encoder_el"]) > MIN_RING_EL_SHIFT_DEG


@pytest.mark.parametrize("bay", RING_BAYS)
@pytest.mark.parametrize("case", sorted(CASES))
def test_the_dwell_check_and_wrap_choice_read_the_boresights_start_and_span(
        site, monkeypatch, case, bay):
    """The pose the dish holds before the scan, checked against the Sun, and
    the span the wrap must keep in range are the posted boresight's, up to the
    whole turns the builder adds after the choice."""
    calls = _spy(monkeypatch, "choose_encoder_solution")
    _build(case, site, CASES[case]["params"])
    aimed = _build(case, site, _with(case, module=bay))
    bare_goal, goal = calls
    points = _points(aimed)
    az = points[:, 1]
    span_min, span_max = goal["goal_az_span"]
    assert goal["goal_el"] == points[0, 2]
    assert abs(_turns_apart(goal["goal_az"], az[0])) < AZ_WRAP_TOL_DEG
    assert abs(_turns_apart(span_min, az.min())) < AZ_WRAP_TOL_DEG
    assert abs((span_max - span_min) - (az.max() - az.min())) < AZ_WRAP_TOL_DEG
    assert abs(goal["goal_el"] - bare_goal["goal_el"]) > MIN_RING_EL_SHIFT_DEG


@pytest.mark.parametrize("bay", RING_BAYS)
@pytest.mark.parametrize("case", [c for c in sorted(CASES) if CASES[c]["constant_el"]])
def test_a_constant_elevation_scan_under_a_bay_holds_one_boresight_elevation(built, case, bay):
    el = _points(built[(case, bay)])[:, 2]
    assert el.max() - el.min() < CONSTANT_EL_TOL_DEG


@pytest.mark.parametrize("module", [["IM5", "IM6"], "IM5,IM6"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_several_bays_reach_the_planner_as_their_mean_position(site, monkeypatch, case, module):
    calls = _spy(monkeypatch, CASES[case]["planner"])
    _build(case, site, _with(case, module=module))
    (kwargs,) = calls
    offset = kwargs["detector_offset"]
    assert offset.dx == 0.0
    assert offset.dy == get_primecam_offset("IM5").dy


def test_a_bay_with_an_lsa_window_reaches_the_planner_without_rising(site, monkeypatch):
    """The field form's LSA-windowed scan (``test_constant_el_scan.py``) under a bay."""
    calls = _spy(monkeypatch, "plan_constant_el_scan")
    base = {k: v for k, v in CE_FIELD_PARAMS.items() if k != "rising"}
    for extra in ({}, {"rising": True}):
        _build("ce_field", site, dict(base, lsa_window=[227.6, 232.6], module="IM3", **extra))
    for kwargs in calls:
        assert kwargs["detector_offset"] is get_primecam_offset("IM3")
        assert kwargs["rising"] is None
        assert kwargs["lsa_window"] == (227.6, 232.6)
    assert len(calls) == 2


def test_a_window_row_with_a_bay_and_the_field_keys_builds(site, monkeypatch):
    calls = _spy(monkeypatch, "plan_constant_el_scan")
    field = dict(ra_center=80.0, dec_center=-40.0, width=3.0, height=3.0)
    _build("ce_window", site, _with("ce_window", module="IM3", **field))
    (kwargs,) = calls
    assert kwargs["field"] == traj_mod.FieldRegion(**field)
    assert kwargs["detector_offset"] is get_primecam_offset("IM3")


# ---------------------------------------------------------------------------
# The Sun: every sample of the boresight is checked, not the bay's.
# ---------------------------------------------------------------------------

#: A daytime epoch at which the Sun stands at about el 50 in the east at FYST.
SUN_EPOCH = Time("2026-11-07T13:26:00", scale="utc")

#: Exclusion and warning radii (deg) of the Sun tests' site: between the two
#: sweeps' closest approaches below (each test asserts it).
SUN_TEST_EXCLUSION_DEG = 0.8
SUN_TEST_WARNING_DEG = 1.0

#: The Sun tests' bay and how far (deg) its window sits above the Sun: IM3 at
#: el 50 holds the boresight about 1.7 deg below the bay, so with the window
#: this far up the boresight sweeps across the Sun and the bay passes over it.
SUN_BAY = "IM3"
SUN_LIFT_DEG = 1.7

#: Azimuth half-width (deg) of the window centred on the Sun: one leg of 40 s at
#: 1 deg/s, so the Sun is met once, mid-sweep, well after the scan starts.
SUN_WINDOW_HALF_WIDTH = 20.0


def _sun_row(lift, *, from_sun=-SUN_WINDOW_HALF_WIDTH):
    """The Sun tests' window row, ``from_sun`` deg of azimuth from the Sun to ``az_min``."""
    start = Time(SUN_EPOCH.unix + SCAN_DISPATCH_BUFFER_SEC, format="unix")
    sun_az, sun_el = Coordinates(get_fyst_site()).get_sun_altaz(start)
    sun_az, sun_el = float(np.asarray(sun_az)), float(np.asarray(sun_el))
    return dict(
        az_min=sun_az + from_sun,
        az_max=sun_az + from_sun + 2.0 * SUN_WINDOW_HALF_WIDTH,
        elevation=sun_el + lift,
        velocity=1.0,
        duration=2.0 * SUN_WINDOW_HALF_WIDTH,
        az_accel=1.0,
    )


def _closest_approach(trajectory, site):
    """Return the smallest separation (deg) from the Sun of a trajectory and its sample index."""
    coords = Coordinates(site)
    sun_az, sun_el = coords.get_sun_altaz(trajectory.start_time + trajectory.times * u.s)
    separation = np.asarray(coords.angular_separation(
        trajectory.az, trajectory.el, np.asarray(sun_az), np.asarray(sun_el)))
    index = int(np.argmin(separation))
    return float(separation[index]), index


def _plan_sun_row(row, site):
    """The boresight's trajectory under the bay and the bay's own, as the planner gives them."""
    kwargs = dict(
        az_window=(row["az_min"], row["az_max"]), elevation=row["elevation"],
        velocity=row["velocity"], duration=row["duration"], site=site,
        start_time=Time(SUN_EPOCH.unix + SCAN_DISPATCH_BUFFER_SEC, format="unix"),
        az_accel=row["az_accel"],
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        boresight = plan_constant_el_scan(
            None, detector_offset=get_primecam_offset(SUN_BAY), **kwargs).trajectory
        bay = plan_constant_el_scan(None, **kwargs).trajectory
    return boresight, bay


def _sweeps(row, site):
    """The closest approaches of the boresight under the bay and of the bay's own sweep."""
    boresight, bay = _plan_sun_row(row, site)
    return _closest_approach(boresight, site), _closest_approach(bay, site)


def _start_pose_separations(row, site):
    """The Sun's separations (deg) from the boresight's and the bay's start poses
    at both ends of the dwell, from the dispatch to the scan's start."""
    coords = Coordinates(site)
    dwell = Time([SUN_EPOCH.unix, SUN_EPOCH.unix + SCAN_DISPATCH_BUFFER_SEC], format="unix")
    sun_az, sun_el = (np.asarray(v) for v in coords.get_sun_altaz(dwell))
    return tuple(
        np.asarray(coords.angular_separation(traj.az[0], traj.el[0], sun_az, sun_el))
        for traj in _plan_sun_row(row, site)
    )


@pytest.fixture
def sun_site():
    return get_fyst_site(
        sun_exclusion_radius=SUN_TEST_EXCLUSION_DEG, sun_warning_radius=SUN_TEST_WARNING_DEG)


def _build_sun_row(row, site):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return build_constant_el_payload(
            scan_params=row, current_az=row["az_min"], current_el=row["elevation"],
            site=site, now_unix=SUN_EPOCH.unix)


def test_a_boresight_across_the_sun_is_refused_while_the_bay_passes_clear(sun_site):
    row = _sun_row(SUN_LIFT_DEG)
    (bore_sep, bore_index), (bay_sep, _) = _sweeps(row, sun_site)
    assert bore_sep < SUN_TEST_EXCLUSION_DEG < bay_sep
    assert bore_index > 0  # mid-sweep: the dwell at the start is clear
    with pytest.raises(TrajectoryValidationError, match="Sun exclusion zone"):
        _build_sun_row(dict(row, module=SUN_BAY), sun_site)
    _build_sun_row(row, sun_site)


def test_a_bay_across_the_sun_builds_while_the_boresight_passes_clear(sun_site):
    row = _sun_row(0.0)
    (bore_sep, _), (bay_sep, bay_index) = _sweeps(row, sun_site)
    assert bay_sep < SUN_TEST_EXCLUSION_DEG < bore_sep
    assert bay_index > 0
    _build_sun_row(dict(row, module=SUN_BAY), sun_site)
    with pytest.raises(TrajectoryValidationError, match="Sun exclusion zone"):
        _build_sun_row(row, sun_site)


# The two dwell tests start the window at the Sun's azimuth and sweep away from
# it, so it is the pose held before the scan that meets the Sun. The dwell check
# is choose_encoder_solution's: it finds no sun-safe wrap for the start pose and
# raises a PointingError, which a TrajectoryValidationError from the sample check
# is not.


def test_a_boresight_start_pose_in_the_sun_is_refused_by_the_dwell_check(sun_site):
    row = _sun_row(SUN_LIFT_DEG, from_sun=0.0)
    boresight, bay = _start_pose_separations(row, sun_site)
    assert boresight.max() < SUN_TEST_EXCLUSION_DEG < bay.min()
    with pytest.raises(PointingError, match="No sun-safe azimuth wrap"):
        _build_sun_row(dict(row, module=SUN_BAY), sun_site)
    _build_sun_row(row, sun_site)


def test_a_bay_start_pose_in_the_sun_builds_past_the_dwell_check(sun_site):
    row = _sun_row(0.0, from_sun=0.0)
    boresight, bay = _start_pose_separations(row, sun_site)
    assert bay.max() < SUN_TEST_EXCLUSION_DEG < boresight.min()
    _build_sun_row(dict(row, module=SUN_BAY), sun_site)
    with pytest.raises(PointingError, match="No sun-safe azimuth wrap"):
        _build_sun_row(row, sun_site)


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_sun_check_reads_the_posted_boresight_trajectory(site, monkeypatch, case):
    seen = []
    real = traj_mod.sun_exclusion_breach

    def recording(traj, site):
        seen.append(traj)
        return real(traj, site)

    monkeypatch.setattr(traj_mod, "sun_exclusion_breach", recording)
    bare = _build(case, site, CASES[case]["params"])
    result = _build(case, site, _with(case, module=RING_BAYS[0]))
    points = _points(result)
    traj = seen[-1]
    assert np.array_equal(np.asarray(traj.az), points[:, 1])
    assert np.array_equal(np.asarray(traj.el), points[:, 2])
    assert abs(float(traj.el[0]) - bare["encoder_el"]) > MIN_RING_EL_SHIFT_DEG


# ---------------------------------------------------------------------------
# The telescope limits: the scan as given and the boresight must both be inside.
# ---------------------------------------------------------------------------


def test_a_row_whose_boresight_falls_below_the_elevation_limit_is_refused(site):
    """At el 20.5 IM3 holds the boresight at el 18.75, under the 20 deg limit;
    the same row without the key builds."""
    row = dict(CE_WINDOW_PARAMS, elevation=20.5)
    with pytest.raises(ElevationBoundsError, match=r"18\.75"):
        _build("ce_window", site, dict(row, module="IM3"))
    _build("ce_window", site, row)


def test_a_row_below_the_limit_is_refused_though_its_boresight_would_be_inside(
        site, monkeypatch):
    """At el 19.5 IM6 would hold the boresight near el 21.3, inside the limits,
    but the library checks the bay's own track first and refuses the row."""
    calls = _spy(monkeypatch, "plan_constant_el_scan")
    with pytest.raises(ElevationBoundsError, match=r"19\.50"):
        _build("ce_window", site, dict(CE_WINDOW_PARAMS, elevation=19.5, module="IM6"))
    (kwargs,) = calls
    assert kwargs["detector_offset"] is get_primecam_offset("IM6")


#: A rising epoch of the pong and daisy field at which each pattern clears the
#: 20 deg limit by about half a degree, so IM3, above the boresight, puts the
#: boresight below it.
LOW_FIELD_EPOCH = Time("2026-06-16T10:52:00", scale="utc")

#: Six minutes earlier, when each pattern itself dips below the limit.
BELOW_FIELD_EPOCH = Time("2026-06-16T10:46:00", scale="utc")


@pytest.mark.parametrize("case", ["pong", "daisy"])
def test_a_field_whose_boresight_falls_below_the_limit_under_a_bay_is_refused(site, case):
    now = LOW_FIELD_EPOCH.unix - SCAN_DISPATCH_BUFFER_SEC
    _build(case, site, CASES[case]["params"], now=now)
    with pytest.raises(ElevationBoundsError):
        _build(case, site, _with(case, module="IM3"), now=now)


@pytest.mark.parametrize("case", ["pong", "daisy"])
def test_a_field_below_the_limit_is_refused_whatever_bay_is_named(site, case):
    """IM6, below the boresight, would lift the boresight inside the limits, but
    the library checks the pattern as given before it applies the bay."""
    now = BELOW_FIELD_EPOCH.unix - SCAN_DISPATCH_BUFFER_SEC
    with pytest.raises(TargetNotObservableError):
        _build(case, site, _with(case, module="IM6"), now=now)


@pytest.mark.parametrize("bay", RING_BAYS)
def test_a_bay_whose_boresight_cannot_be_solved_is_refused_by_the_library(site, bay):
    """At el 88.5 the inverse that places a ring bay on the row does not
    converge, and the library's OffsetInversionError, a PointingError, reaches
    the caller."""
    with pytest.raises(OffsetInversionError):
        _build("ce_window", site, dict(CE_WINDOW_PARAMS, elevation=88.5, module=bay))


# ---------------------------------------------------------------------------
# Refusals before anything is planned.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, error",
    [("IM9", KeyError), ("i1", KeyError), ("", ValueError), ([], ValueError), (5, TypeError)],
    ids=["unknown", "retired-i1", "empty-str", "empty-list", "int"],
)
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_module_that_names_no_bay_is_refused_before_planning(
        site, monkeypatch, case, module, error):
    calls = _spy(monkeypatch, CASES[case]["planner"])
    with pytest.raises(error):
        _build(case, site, _with(case, module=module))
    assert calls == []


@pytest.mark.parametrize("footprint", ["IM5", "c", "", False])
@pytest.mark.parametrize("case", sorted(CASES))
def test_a_footprint_on_a_field_scan_is_refused_naming_module(
        site, monkeypatch, case, footprint):
    calls = _spy(monkeypatch, CASES[case]["planner"])
    with pytest.raises(ValueError, match="module") as excinfo:
        _build(case, site, _with(case, footprint=footprint))
    assert repr(footprint) in str(excinfo.value)
    assert calls == []


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_null_footprint_on_a_field_scan_is_no_footprint(site, case):
    result = _build(case, site, _with(case, footprint=None))
    assert result["payload"] == _build(case, site, CASES[case]["params"])["payload"]


#: The source tests' centred Jupiter pass and its prompt epoch (``test_source_scan.py``).
SOURCE_PARAMS = dict(body="jupiter", el_bore=35.0, mode="rising")
SOURCE_NOW = Time("2026-06-15T16:30:00", scale="utc").unix


def _build_source(params, site):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return build_source_payload(
            scan_params=params, current_az=200.0, current_el=60.0, site=site,
            now_unix=SOURCE_NOW, max_dispatch_delay_sec=float("inf"))


@pytest.mark.parametrize("module", ["IM5", "", False])
def test_a_module_on_a_source_scan_is_refused_naming_footprint(site, monkeypatch, module):
    calls = _spy(monkeypatch, "plan_source_ces")
    with pytest.raises(ValueError, match="footprint") as excinfo:
        _build_source(dict(SOURCE_PARAMS, module=module), site)
    assert repr(module) in str(excinfo.value)
    assert "source_scan" in str(excinfo.value)
    assert calls == []


def test_a_null_module_on_a_source_scan_builds_as_before(site):
    result = _build_source(dict(SOURCE_PARAMS, module=None), site)
    assert result["payload"] == _build_source(SOURCE_PARAMS, site)["payload"]


#: The constant-elevation planner's own defaults, which an absent option must reach it as.
CE_PLANNER_DEFAULTS = {
    name: parameter.default
    for name, parameter in inspect.signature(plan_constant_el_scan).parameters.items()
}

#: The options either form of the builder fills in when the caller leaves them out.
CE_DEFAULTED_OPTIONS = (
    "rising", "angle", "max_search_hours", "step_seconds", "az_padding", "az_accel", "timestep",
)


@pytest.mark.parametrize("case", ["ce_field", "ce_window"])
def test_an_absent_option_reaches_the_planner_as_the_planners_default(site, monkeypatch, case):
    calls = _spy(monkeypatch, "plan_constant_el_scan")
    params = {k: v for k, v in CASES[case]["params"].items() if k not in CE_DEFAULTED_OPTIONS}
    _build(case, site, params)
    (kwargs,) = calls
    for name in CE_DEFAULTED_OPTIONS:
        if name in kwargs:
            assert kwargs[name] == CE_PLANNER_DEFAULTS[name], name
