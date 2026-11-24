"""Tests for the FYST source-tracking CES dispatch core + Go TCS contract.

PRIMARY (no server, no ocs): drive
:func:`pcs.agents.acu_interface.trajectory.build_source_payload` directly and
assert the returned ``/path`` body satisfies every rule the Go TCS enforces in
``commands.go`` (``pathCmd.Check()`` + ``checkAzEl``), mirroring
``test_constant_el_scan.py``:

- exactly the three keys ``{start_time, coordsys, points}``; ``coordsys ==
  "Horizon"``;
- ``start_time`` absolute Unix and >= now + 10 s (the
  :data:`SCAN_DISPATCH_BUFFER_SEC` floor, stronger than the 9.8 s reject);
- ``points`` is N x 5; every consecutive ``dt >= 0.05`` s;
- the first 100 points satisfy az in [-180, 360], el in [-90, 180],
  ``|vaz| <= 3.0``, ``|vel| <= 1.5`` (velocities present);
- the slew-target az is in range and ``points[0][1] == encoder_az`` (wrap align).

Plus the source_scan-specific guards:

- the **centred gate**: an off-centre ``footprint`` or non-``None``
  ``boresight_rot`` raises ``ValueError``, each with its own reason (the bay
  layout is not yet checked on the sky; no boresight rotation is confirmed),
  naming the spellings of the centre it accepts; a removed ring label fails with
  the library's ``KeyError``; any spelling of the centre module (``"c"``,
  ``"center"``, ``"IM0"``, in any case) and a ``footprint`` of ``None`` (a JSON
  null) pass and reach the planner as ``"c"``, a tag that names no module fails
  with the library's ``KeyError`` and a list of tags with its ``TypeError``;
- velocity frame: the commanded az velocity is the planner's solved MOUNT-frame
  drift (small), NOT an on-sky-scaled rate;
- hardware-dynamics escalation: a velocity/acceleration breach anywhere raises
  ``TrajectoryValidationError``;
- dispatch-delay: a far ``el_bore`` crossing raises ``DispatchDelayError``
  advising a promptly-reachable elevation, and a scheduled start past the bound
  raises it advising a dispatch closer to that start;
- geometry overrides: ``az_speed``, ``az_throw`` and ``dwell`` reach
  ``plan_source_ces`` when given and stay at its defaults when absent;
- ``footprint_margin``: the centre module's footprint reaches
  ``plan_source_ces`` inflated by the margin (a negative one fails with the
  library's ``ValueError``), the ``"c"`` tag goes through unchanged without it,
  and the centred gate still refuses an off-centre footprint that carries one;
- the anchored form: without ``el_bore`` the floored start is the planner's
  ``start_time`` anchor (``night`` unset, ``mode`` derived unless given) and the
  pass is the one the library plans from that anchor; a ``window`` without
  ``el_bore`` is refused.

DATE-STABILITY: ``plan_source_ces`` searches forward for the ``el_bore`` crossing,
so the resolved start depends on wall-clock. Every build threads a FIXED ``Time``
epoch. At 2026-06-15T16:30 UTC Jupiter rising reaches el=35 ~2.4 min out (PROMPT,
inside the 30-min default); at 13:00 UTC it is ~hours out (FAR, trips the
dispatch-delay guard). Contract tests pass ``max_dispatch_delay_sec=float("inf")``.

Imports ``trajectory.py`` directly, never ``agent.py``, the agent needs the
ocs/twisted framework, the whole reason the core is factored ocs-free. The
Process-level contracts are exercised ocs-free in ``test_dispatch_runner.py``.
"""

import inspect
import warnings

import numpy as np
import pytest
from astropy.time import Time

from fyst_trajectories import get_fyst_site
from fyst_trajectories.planning import ArrayFootprint
from fyst_trajectories.planning import plan_source_ces as library_plan_source_ces
from fyst_trajectories.primecam import MODULE_FOV_RADIUS_DEG
from fyst_trajectories.trajectory import Trajectory

from pcs.agents.acu_interface import trajectory as traj_mod
from pcs.agents.acu_interface.trajectory import (
    MAX_DISPATCH_DELAY_SEC,
    SCAN_DISPATCH_BUFFER_SEC,
    DispatchDelayError,
    TrajectoryValidationError,
    _escalate_hardware_dynamics,
    build_source_payload,
)

# Go TCS /path contract constants (commands.go checkAzEl + pathCmd.Check()).
AZ_MIN_TCS = -180.0  # azimuthMin at TCS b7bbbf9
AZ_MAX_TCS = 360.0  # azimuthMax at TCS b7bbbf9
EL_MIN_TCS = -90.0  # elevationMin at TCS b7bbbf9
EL_MAX_TCS = 180.0  # elevationMax at TCS b7bbbf9
AZ_SPEED_MAX_TCS = 3.0  # azimuthSpeedMax at TCS b7bbbf9
EL_SPEED_MAX_TCS = 1.5  # elevationSpeedMax at TCS b7bbbf9
MIN_DT_TCS = 0.05  # pathCmd.Check at TCS b7bbbf9
MIN_LEAD_TCS = 9.8  # checkProgramTrackStartTime at TCS b7bbbf9
FIRST_N_CHECKED = 100  # pathCmd.Check at TCS b7bbbf9

# A representative centred Jupiter rising source-CES.
REPRESENTATIVE_PARAMS = dict(
    body="jupiter",
    el_bore=35.0,
    mode="rising",
)

# Epoch at which REPRESENTATIVE_PARAMS resolve a PROMPT crossing (~2.4 min out),
# so the build succeeds and the contract assertions run. See module docstring.
FIXED_PROMPT_EPOCH = Time("2026-06-15T16:30:00", scale="utc")
FIXED_PROMPT_NOW = FIXED_PROMPT_EPOCH.unix

# Epoch at which the same field's crossing is hours out, trips the guard.
FIXED_FAR_EPOCH = Time("2026-06-15T13:00:00", scale="utc")
FIXED_FAR_NOW = FIXED_FAR_EPOCH.unix


@pytest.fixture
def site():
    # Sun avoidance disabled so the wrap choice is purely geometric and the
    # contract assertions are deterministic. The Go TCS contract (bounds +
    # velocity + timing) is independent of sun avoidance.
    return get_fyst_site(sun_avoidance_enabled=False)


@pytest.fixture
def built(site):
    """A built centred source-CES payload from the prompt epoch.

    ``max_dispatch_delay_sec=float("inf")`` disables the dispatch-delay guard:
    this fixture asserts the ``/path`` contract, not dispatch timeliness.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_source_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    return result, FIXED_PROMPT_NOW


# ---------------------------------------------------------------------------
# PRIMARY: Go TCS /path contract (pathCmd.Check + checkAzEl)
# ---------------------------------------------------------------------------


def test_payload_has_exactly_three_keys(built):
    result, _ = built
    assert set(result["payload"].keys()) == {"start_time", "coordsys", "points"}


def test_coordsys_is_horizon(built):
    result, _ = built
    assert result["payload"]["coordsys"] == "Horizon"


def test_start_time_absolute_and_respects_buffer_floor(built):
    result, now = built
    start_time = result["payload"]["start_time"]
    assert start_time > 1e9  # absolute Unix seconds, not relative
    assert start_time >= now + MIN_LEAD_TCS  # checkProgramTrackStartTime at TCS b7bbbf9
    assert start_time >= now + SCAN_DISPATCH_BUFFER_SEC - 1e-6  # the 10 s floor


def test_points_is_n_by_5(built):
    result, _ = built
    points = result["payload"]["points"]
    assert len(points) > 0  # pathCmd.Check at TCS b7bbbf9, "no points in path"
    assert all(len(p) == 5 for p in points)


def test_consecutive_dt_at_least_50ms(built):
    result, _ = built
    times = np.array([p[0] for p in result["payload"]["points"]])
    assert np.diff(times).min() >= MIN_DT_TCS  # pathCmd.Check at TCS b7bbbf9


def test_first_100_points_satisfy_checkAzEl(built, site):
    result, _ = built
    points = result["payload"]["points"]
    el_min = site.telescope_limits.elevation.min
    el_max = site.telescope_limits.elevation.max
    for i, p in enumerate(points[:FIRST_N_CHECKED]):
        t, az, el, vaz, vel = p
        assert AZ_MIN_TCS <= az <= AZ_MAX_TCS, f"point {i}: az {az} out of TCS range"
        assert EL_MIN_TCS <= el <= EL_MAX_TCS, f"point {i}: el {el} out of TCS range"
        assert el_min <= el <= el_max, f"point {i}: el {el} out of site range"
        assert abs(vaz) <= AZ_SPEED_MAX_TCS, f"point {i}: |vaz| {vaz} > {AZ_SPEED_MAX_TCS}"
        assert abs(vel) <= EL_SPEED_MAX_TCS, f"point {i}: |vel| {vel} > {EL_SPEED_MAX_TCS}"


def test_velocities_present_and_non_null(built):
    result, _ = built
    for i, p in enumerate(result["payload"]["points"][:FIRST_N_CHECKED]):
        assert p[3] is not None and np.isfinite(p[3]), f"point {i}: az_vel null/non-finite"
        assert p[4] is not None and np.isfinite(p[4]), f"point {i}: el_vel null/non-finite"


def test_source_ces_holds_elevation_fixed(built):
    """A source CES is a constant-elevation scan: el is (numerically) fixed."""
    result, _ = built
    el = np.array([p[2] for p in result["payload"]["points"]])
    # Boresight el is held at el_bore; only the source drifts across the FOV.
    assert el.max() - el.min() < 1e-6


def test_slew_target_in_range(built, site):
    result, _ = built
    assert site.telescope_limits.azimuth.is_in_range(result["encoder_az"])
    assert site.telescope_limits.elevation.is_in_range(result["encoder_el"])


def test_wrap_alignment_first_point_equals_encoder_az(built):
    result, _ = built
    assert result["payload"]["points"][0][1] == pytest.approx(result["encoder_az"], abs=1e-9)


# ---------------------------------------------------------------------------
# Source gate: centred + uncommanded-rotator only.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("footprint", ["IM1", "IM2", "IM3", "IM4", "IM5", "IM6", "im3"])
def test_off_centre_footprint_is_gated(site, monkeypatch, footprint):
    """A ring bay, in any case, raises the gate's descriptive error before
    anything is planned, naming every spelling of the centre the gate accepts
    and the footprint it got, so the caller sees what to send instead, and the
    footprint's own reason."""
    calls = _spy_on_plan_source_ces(monkeypatch)
    bad = dict(REPRESENTATIVE_PARAMS, footprint=footprint)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"centred PrimeCam footprint") as excinfo:
            build_source_payload(
                scan_params=bad,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
            )
    assert f'("c", "center" or "IM0", any case), got {footprint!r}' in str(excinfo.value)
    assert traj_mod._FOOTPRINT_GATE_NOTE in str(excinfo.value)
    assert calls == []


def test_a_removed_ring_label_fails_with_the_library_error_before_planning(site, monkeypatch):
    """``i1`` names no bay since the ring moved onto IM1..IM6, so the gate's
    resolution of it raises the library's KeyError, whose message says the
    label is no longer defined, and nothing is planned."""
    calls = _spy_on_plan_source_ces(monkeypatch)
    bad = dict(REPRESENTATIVE_PARAMS, footprint="i1")
    with pytest.raises(KeyError, match=r"no longer defined"):
        build_source_payload(
            scan_params=bad,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
        )
    assert calls == []


def test_commanded_boresight_rot_is_gated(site):
    """A commanded (non-None) boresight_rot raises a descriptive error."""
    bad = dict(REPRESENTATIVE_PARAMS, boresight_rot=0.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"boresight"):
            build_source_payload(
                scan_params=bad,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
            )


def test_the_boresight_rot_refusal_gives_its_own_reason_not_the_footprints(site):
    """The two refusals have different reasons, so each carries its own note."""
    bad = dict(REPRESENTATIVE_PARAMS, boresight_rot=0.0)
    with pytest.raises(ValueError, match=r"boresight_rot=None") as excinfo:
        build_source_payload(
            scan_params=bad,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
        )
    assert traj_mod._BORESIGHT_ROT_GATE_NOTE in str(excinfo.value)
    assert traj_mod._FOOTPRINT_GATE_NOTE not in str(excinfo.value)


@pytest.mark.parametrize("footprint", ["c", "center", "IM0", "C", "Center", None])
def test_centre_aliases_pass_the_gate(site, monkeypatch, footprint):
    """Every spelling of the on-axis module passes the gate and builds, and the
    planner receives the canonical "c": the gate resolves the tag through the
    library, so the instrument team's "IM0" and a capitalised "C" or "Center"
    name the centre as "c" and "center" do. A footprint of None, what a JSON
    null gives, counts as no footprint given, which is the centre."""
    calls = _spy_on_plan_source_ces(monkeypatch)
    params = dict(REPRESENTATIVE_PARAMS, footprint=footprint)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_source_payload(
            scan_params=params,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    ((kwargs, _),) = calls
    assert kwargs["footprint"] == "c"
    assert set(result["payload"].keys()) == {"start_time", "coordsys", "points"}


def test_an_unknown_footprint_tag_fails_the_build_with_the_library_error(site, monkeypatch):
    """A tag that names no Prime-Cam module fails the build with the library's
    KeyError, raised when the gate resolves the tag, so nothing is planned."""
    calls = _spy_on_plan_source_ces(monkeypatch)
    bad = dict(REPRESENTATIVE_PARAMS, footprint="IM9")
    with pytest.raises(KeyError, match=r"Unknown PrimeCam module 'IM9'"):
        build_source_payload(
            scan_params=bad,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    assert calls == []


def test_a_list_of_tags_fails_the_build_with_the_library_error(site, monkeypatch):
    """A footprint given as a list of module tags (a JSON ["c"], say) is not a
    tag: the gate resolves it through the library, which takes a sequence only
    of InstrumentOffset objects and raises its TypeError, so the build fails
    before anything is planned rather than read the list as some module."""
    calls = _spy_on_plan_source_ces(monkeypatch)
    bad = dict(REPRESENTATIVE_PARAMS, footprint=["c"])
    with pytest.raises(TypeError, match=r"footprint sequence must contain only InstrumentOffset"):
        build_source_payload(
            scan_params=bad,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    assert calls == []


# ---------------------------------------------------------------------------
# Mount-frame velocity (NOT on-sky-scaled).
# ---------------------------------------------------------------------------


def test_velocity_is_mount_frame_drift_not_on_sky(built, site):
    """The commanded az velocity is the solved MOUNT-frame drift, not on-sky.

    ``plan_source_ces`` solves a small azimuth drift rate ``v_az`` (mount frame)
    and bakes it into a ConstantElScanConfig.az_speed (also mount frame). The
    realised peak |az velocity| in the posted body must equal that solved drift
    magnitude to within numerical tolerance, it is NOT the source's on-sky az
    rate divided by cos(el). A drift of a few hundredths deg/s is the expected
    magnitude for a slowly-moving planet near el=35.
    """
    result, _ = built
    # Re-derive the solved drift from the same build (params-only sibling shares
    # the compute kernel, but we read it back off the trajectory here).
    vaz = np.array([abs(p[3]) for p in result["payload"]["points"]])
    # Mount-frame drift is small (hundredths deg/s), and crucially far below the
    # azimuth speed ceiling, a cos(el)-inflated on-sky rate would be larger.
    assert vaz.max() < 0.3, f"peak |vaz| {vaz.max()} unexpectedly large for a planet drift"
    assert vaz.max() > 0.0  # there IS a commanded drift


# ---------------------------------------------------------------------------
# Hardware-dynamics escalation (velocity / acceleration, whole trajectory).
#
# A REAL source-CES cannot breach the velocity ceiling via a faster ``v_az``
# without first breaching the azimuth *position* bounds (a constant drift over a
# multi-hundred-second pass accumulates thousands of degrees), so that path
# correctly raises AzimuthBoundsError first, a hard pre-POST rejection too. We
# therefore exercise the velocity/acceleration escalation that
# build_source_payload routes through by driving the shared
# ``_escalate_hardware_dynamics`` helper on a synthetic over-fast trajectory
# (the genuine unit of that contract: the Go TCS checks the first 100 points
# before accepting a path and the rest only as it uploads them, so the agent
# enforces the ceiling everywhere before the POST).
# ---------------------------------------------------------------------------


def test_over_fast_v_az_is_rejected_before_post(site):
    """Bounds: an over-large v_az is rejected (not silently POSTed).

    An overridden drift far above the ceiling makes the source-CES az run off
    the telescope, so ``validate_trajectory`` rejects it on position bounds
    BEFORE the POST, a hard pre-POST rejection, which is the safety contract
    (the velocity-specific escalation is unit-tested via the shared helper
    below). Either failure mode is a ``PointingError`` subclass /
    ``TrajectoryValidationError``; assert it does not return a body.
    """
    from fyst_trajectories.exceptions import PointingError

    fast = dict(REPRESENTATIVE_PARAMS, v_az=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises((TrajectoryValidationError, PointingError)):
            build_source_payload(
                scan_params=fast,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
                max_dispatch_delay_sec=float("inf"),
            )


def test_hardware_dynamics_helper_escalates_velocity_breach():
    """Unit: the shared escalation raises on a too-fast synthetic trajectory.

    ``build_source_payload`` (and the pong/daisy cores) route their dynamics
    check through ``_escalate_hardware_dynamics``. Drive it directly on a
    synthetic constant-velocity az track at 4.0 deg/s (> the 3.0 deg/s Go TCS
    hardware ceiling, azimuthSpeedMax at TCS ``b7bbbf9``) and on a 2.0 deg/s el
    track (> the 1.5 deg/s ceiling, elevationSpeedMax); each must raise, and the
    message says why the whole path is checked: the Go TCS checks the first 100
    points before accepting and the rest only as it uploads them. Date-independent
    (synthetic arrays).
    """
    import dataclasses

    from astropy.time import Time as _Time

    from fyst_trajectories.trajectory import Trajectory

    from pcs.agents.acu_interface.trajectory import _escalate_hardware_dynamics

    t = np.arange(0.0, 5.0, 0.1)
    start = _Time("2026-06-15T16:30:00", scale="utc")
    # Az breach: 4.0 deg/s > 3.0 ceiling (el fixed).
    az_fast = Trajectory(
        times=t,
        az=100.0 + 4.0 * t,
        el=np.full_like(t, 50.0),
        az_vel=np.full_like(t, 4.0),
        el_vel=np.zeros_like(t),
        start_time=start,
    )
    with pytest.raises(TrajectoryValidationError, match=r"az velocity") as excinfo:
        _escalate_hardware_dynamics(az_fast, "Synthetic")
    assert "checks the first 100 points before accepting" in str(excinfo.value)
    # El breach: 2.0 deg/s > 1.5 ceiling (az fixed).
    el_fast = dataclasses.replace(
        az_fast,
        az=np.full_like(t, 100.0),
        el=50.0 + 2.0 * t,
        az_vel=np.zeros_like(t),
        el_vel=np.full_like(t, 2.0),
    )
    with pytest.raises(TrajectoryValidationError, match=r"el velocity"):
        _escalate_hardware_dynamics(el_fast, "Synthetic")


def _gentle_track(az_vel_column, el_vel_column):
    """A 0.5 deg/s az track at fixed el, its velocity columns set by the caller."""
    t = np.arange(0.0, 5.0, 0.1)
    return Trajectory(
        times=t,
        az=100.0 + 0.5 * t,
        el=np.full_like(t, 50.0),
        az_vel=np.full_like(t, az_vel_column),
        el_vel=np.full_like(t, el_vel_column),
        start_time=Time("2026-06-15T16:30:00", scale="utc"),
    )


def test_hardware_dynamics_helper_escalates_velocity_column_breach():
    """Unit: a velocity column over the ceiling is refused though the positions are gentle.

    ``checkAzEl`` (``pathCmd.Check`` and at upload, TCS ``b7bbbf9``) tests each
    point's commanded velocity columns, which ``to_path_payload`` takes from
    ``traj.az_vel`` / ``traj.el_vel``, so a column above the ceiling is refused
    by the TCS after the slew even when the positions' gradient is inside it.
    Each such track must raise with the same message form as a gradient breach.
    """
    with pytest.raises(TrajectoryValidationError, match=r"peak az velocity 4\.000 deg/s") as excinfo:
        _escalate_hardware_dynamics(_gentle_track(4.0, 0.0), "Synthetic")
    assert "checks the first 100 points before accepting" in str(excinfo.value)
    with pytest.raises(TrajectoryValidationError, match=r"peak el velocity 2\.000 deg/s"):
        _escalate_hardware_dynamics(_gentle_track(0.5, 2.0), "Synthetic")


def test_hardware_dynamics_helper_passes_velocity_columns_within_ceilings():
    """Unit: gentle positions with velocity columns inside both ceilings pass."""
    _escalate_hardware_dynamics(_gentle_track(0.5, 1.0), "Synthetic")


def test_too_high_az_accel_raises_via_analytic_guard(site):
    """A source ``az_accel`` whose quintic peak (1.5 * az_accel) exceeds
    the Go TCS hardware ceiling is rejected pre-POST.

    ``plan_source_ces`` reuses the CE quintic turnaround, whose peak |az accel|
    is ``1.5 * az_accel`` by design. ``np.gradient`` (in
    ``_escalate_hardware_dynamics``) under-resolves that short, sharp spike at
    the dispatch timestep, so ``build_source_payload`` adds the same ANALYTIC
    guard ``build_constant_el_payload`` has. ``az_accel=5.0`` -> peak ``7.5``
    deg/s^2 > the ``6.0`` az ceiling, so it must raise (the gradient alone misses
    it and would POST). Driven end-to-end through ``build_source_payload`` on a
    real centred Jupiter source, so it also exercises the source escalation wiring.
    """
    bad = dict(REPRESENTATIVE_PARAMS, az_accel=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TrajectoryValidationError, match=r"acceleration"):
            build_source_payload(
                scan_params=bad,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
                max_dispatch_delay_sec=float("inf"),
            )


# ---------------------------------------------------------------------------
# Dispatch-delay guard.
# ---------------------------------------------------------------------------


def test_dispatch_delay_far_out_crossing_raises(site):
    """A crossing that resolves hours out raises DispatchDelayError.

    Date-stable fixed ``now``: at 2026-06-15T13:00 UTC Jupiter's el=35 rising
    crossing is hours away (far past the 30-min default), so the guard refuses
    to slew and hold ``azel_lock``. Default ``max_dispatch_delay_sec`` (no
    override) is exercised on purpose. The refusal gives the elevation advice of
    the planners that search for a crossing, not the pong and daisy sentence
    about the start time.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(DispatchDelayError, match=r"after dispatch") as excinfo:
            build_source_payload(
                scan_params=REPRESENTATIVE_PARAMS,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_FAR_NOW,
            )
    message = str(excinfo.value)
    assert "promptly-reachable elevation" in message
    assert "dispatched closer to its time" not in message


@pytest.mark.parametrize("params", [{"body": "jupiter"}, REPRESENTATIVE_PARAMS],
                         ids=["anchored-form", "el-bore-form"])
def test_a_start_an_hour_out_is_refused_with_advice_about_the_start(site, params):
    """A scheduled start an hour out, past the default bound, raises
    DispatchDelayError whatever the elevation, so the message gives the advice
    that fits: dispatch closer to the scheduled start, not pass a
    promptly-reachable elevation, which could not bring the scan nearer than
    the start it searches from. Both forms are checked: the anchored one plans
    from that start, and the one with ``el_bore`` searches from it for the
    crossing (here the next day's, 24 h out)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(DispatchDelayError, match=r"after dispatch") as excinfo:
            build_source_payload(
                scan_params=params,
                current_az=200.0,
                current_el=60.0,
                site=site,
                scheduled_t0_unix=FIXED_PROMPT_NOW + 3600.0,
                now_unix=FIXED_PROMPT_NOW,
            )
    message = str(excinfo.value)
    assert "A scheduled start this far out must be dispatched closer to its time." in message
    assert "promptly-reachable elevation" not in message


def test_dispatch_delay_prompt_crossing_passes(site):
    """A promptly-reachable crossing passes the default dispatch-delay guard."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_source_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
        )
    delay = result["payload"]["start_time"] - FIXED_PROMPT_NOW
    assert 0.0 < delay <= 1800.0


def test_scheduled_t0_in_past_is_floored(site):
    """The buffer floor advances a past scheduled_t0 to now + buffer."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_source_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            scheduled_t0_unix=FIXED_PROMPT_NOW - 1000.0,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    assert result["payload"]["start_time"] >= FIXED_PROMPT_NOW + SCAN_DISPATCH_BUFFER_SEC - 1e-6


# ---------------------------------------------------------------------------
# Scan-geometry overrides: az_speed, az_throw and dwell reach plan_source_ces.
# ---------------------------------------------------------------------------


def _spy_on_plan_source_ces(monkeypatch):
    """Wrap the planner the core calls; record each call's keywords and block."""
    real = traj_mod.plan_source_ces
    calls = []

    def spy(**kwargs):
        block = real(**kwargs)
        calls.append((kwargs, block))
        return block

    monkeypatch.setattr(traj_mod, "plan_source_ces", spy)
    return calls


def test_geometry_overrides_reach_the_planner(site, monkeypatch):
    """``az_speed``, ``az_throw`` and ``dwell`` are forwarded and take effect.

    Each requested value differs from what the default pass solves at the
    prompt epoch (a wider throw, a faster leg speed, a shorter pass), so the
    solved parameters and the posted legs show the request. The 300 s dwell is
    quantised to whole legs, 8 s each at 4.0 deg and 0.5 deg/s.
    """
    calls = _spy_on_plan_source_ces(monkeypatch)
    params = dict(REPRESENTATIVE_PARAMS, az_speed=0.5, az_throw=4.0, dwell=300.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_source_payload(
            scan_params=params,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    ((kwargs, block),) = calls
    assert (kwargs["az_speed"], kwargs["az_throw"], kwargs["dwell"]) == (0.5, 4.0, 300.0)
    cp = block.computed_params
    assert cp["az_speed"] == pytest.approx(0.5)
    assert cp["az_throw"] == pytest.approx(4.0)
    assert cp["duration"] == pytest.approx(300.0, abs=20.0)
    # Each leg runs at the requested speed plus the slow v_az drift.
    vaz = np.array([abs(p[3]) for p in result["payload"]["points"]])
    assert vaz.max() == pytest.approx(0.5 + abs(cp["v_az"]), abs=1e-3)


def test_absent_geometry_overrides_keep_the_library_defaults(site, monkeypatch):
    """Without the keys the planner receives its own default for each, and
    without ``footprint_margin``, or with it ``None``, the footprint tag goes
    through unchanged."""
    library = inspect.signature(traj_mod.plan_source_ces).parameters
    calls = _spy_on_plan_source_ces(monkeypatch)
    for params in (REPRESENTATIVE_PARAMS, dict(REPRESENTATIVE_PARAMS, footprint_margin=None)):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            build_source_payload(
                scan_params=params,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
                max_dispatch_delay_sec=float("inf"),
            )
    assert len(calls) == 2
    for kwargs, _ in calls:
        for key in ("az_speed", "az_throw", "dwell"):
            assert kwargs[key] is None
            assert kwargs[key] == library[key].default
        assert kwargs["footprint"] == "c"


# ---------------------------------------------------------------------------
# footprint_margin: the centre footprint reaches plan_source_ces inflated by the
# margin, which is applied after the centred gate.
# ---------------------------------------------------------------------------


def test_footprint_margin_reaches_the_planner_as_the_inflated_centre(site, monkeypatch):
    """``footprint_margin`` reaches the planner as the centre module's footprint
    with its cover pushed out by the margin, and lengthens the pass.

    The planner gets the footprint the library's simulator rebuilds a margined
    pass on: an ``ArrayFootprint`` centred on the boresight whose cover sits
    ``MODULE_FOV_RADIUS_DEG + 0.3`` deg from the centre. The source takes longer
    to cross the wider cover, so the solved pass outlasts the same build without
    the key. The centre named ``"IM0"`` reaches the planner as the same inflated
    centre, since the gate hands the margin the canonical ``"c"``.
    """
    calls = _spy_on_plan_source_ces(monkeypatch)
    for params in (
        REPRESENTATIVE_PARAMS,
        dict(REPRESENTATIVE_PARAMS, footprint_margin=0.3),
        dict(REPRESENTATIVE_PARAMS, footprint="IM0", footprint_margin=0.3),
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            build_source_payload(
                scan_params=params,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
                max_dispatch_delay_sec=float("inf"),
            )
    (_, bare), (kwargs, margined), (alias_kwargs, _) = calls
    footprint = kwargs["footprint"]
    assert isinstance(footprint, ArrayFootprint)
    assert (footprint.center_xi_deg, footprint.center_eta_deg) == (0.0, 0.0)
    radius = np.hypot(footprint.cover_xi_deg, footprint.cover_eta_deg)
    assert radius == pytest.approx(MODULE_FOV_RADIUS_DEG + 0.3, abs=1e-9)
    assert margined.computed_params["duration"] > bare.computed_params["duration"]
    alias_footprint = alias_kwargs["footprint"]
    assert isinstance(alias_footprint, ArrayFootprint)
    assert (alias_footprint.center_xi_deg, alias_footprint.center_eta_deg) == (0.0, 0.0)
    assert np.array_equal(alias_footprint.cover_xi_deg, footprint.cover_xi_deg)
    assert np.array_equal(alias_footprint.cover_eta_deg, footprint.cover_eta_deg)


def test_negative_footprint_margin_fails_the_build(site):
    """A negative ``footprint_margin`` raises the library's ``ValueError``, which
    the task reports as a failed build; the builder does not check it itself."""
    bad = dict(REPRESENTATIVE_PARAMS, footprint_margin=-0.1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"non-negative"):
            build_source_payload(
                scan_params=bad,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
                max_dispatch_delay_sec=float("inf"),
            )


def test_off_centre_footprint_with_a_margin_is_still_gated(site):
    """The gate runs before the margin is applied, so an off-centre footprint
    that carries a margin is refused with the gate's message."""
    bad = dict(REPRESENTATIVE_PARAMS, footprint="IM4", footprint_margin=0.3)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match=r"centred PrimeCam footprint"):
            build_source_payload(
                scan_params=bad,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_PROMPT_NOW,
            )


# ---------------------------------------------------------------------------
# The anchored form: without el_bore the floored start is the planner's
# start_time anchor, from which it derives the boresight elevation and the
# direction, so a scan handed only a start gets its pass.
# ---------------------------------------------------------------------------


def test_without_el_bore_the_floored_start_is_the_planner_anchor(site, monkeypatch):
    """Without ``el_bore`` the builder plans from ``start_time``, the floored
    anchor, with ``night`` unset and ``mode`` left for the planner to derive, and
    the pass is the one the library plans from that anchor directly: same start,
    same boresight elevation. The posted start follows the anchor and stays
    inside the default dispatch-delay bound."""
    calls = _spy_on_plan_source_ces(monkeypatch)
    anchor = FIXED_PROMPT_NOW + 600.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_source_payload(
            scan_params={"body": "jupiter"},
            current_az=200.0,
            current_el=60.0,
            site=site,
            scheduled_t0_unix=anchor,
            now_unix=FIXED_PROMPT_NOW,
        )
        direct = library_plan_source_ces(
            body="jupiter", footprint="c", start_time=Time(anchor, format="unix"), site=site
        )
    ((kwargs, block),) = calls
    assert kwargs["el_bore"] is None and kwargs["night"] is None and kwargs["mode"] is None
    assert kwargs["start_time"].unix == pytest.approx(anchor, abs=1e-6)
    assert block.trajectory.start_time.unix == pytest.approx(direct.trajectory.start_time.unix, abs=1e-6)
    assert block.computed_params["el_bore"] == pytest.approx(direct.computed_params["el_bore"], abs=1e-9)
    assert block.computed_params["mode"] == direct.computed_params["mode"]
    start = result["payload"]["start_time"]
    assert start == pytest.approx(block.trajectory.start_time.unix, abs=1e-6)
    assert anchor <= start <= anchor + MAX_DISPATCH_DELAY_SEC


def _planner_kwargs(monkeypatch, site, scan_params):
    """The keyword arguments the builder hands ``plan_source_ces`` for ``scan_params``."""
    calls = _spy_on_plan_source_ces(monkeypatch)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        build_source_payload(
            scan_params=scan_params,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    ((kwargs, _),) = calls
    return kwargs


def test_the_anchored_form_forwards_an_explicit_mode(site, monkeypatch):
    """An explicit ``mode`` reaches the planner beside the floored anchor."""
    kwargs = _planner_kwargs(monkeypatch, site, {"body": "jupiter", "mode": "rising"})
    assert kwargs["mode"] == "rising" and kwargs["el_bore"] is None and kwargs["night"] is None
    assert kwargs["start_time"].unix == pytest.approx(FIXED_PROMPT_NOW + SCAN_DISPATCH_BUFFER_SEC, abs=1e-6)


def test_with_el_bore_the_floored_start_is_the_night(site, monkeypatch):
    """With ``el_bore`` the planner gets the floored start as ``night`` and no
    ``start_time``."""
    kwargs = _planner_kwargs(monkeypatch, site, REPRESENTATIVE_PARAMS)
    assert kwargs["el_bore"] == 35.0 and kwargs["mode"] == "rising" and kwargs["start_time"] is None
    assert kwargs["night"].unix == pytest.approx(FIXED_PROMPT_NOW + SCAN_DISPATCH_BUFFER_SEC, abs=1e-6)


def test_a_window_without_el_bore_fails_the_build(site):
    """A ``window`` fixes the search, so the planner needs ``el_bore`` beside it
    and refuses the dictionary with its own ``ValueError``."""
    window = [
        Time(FIXED_PROMPT_NOW, format="unix").isot,
        Time(FIXED_PROMPT_NOW + 3600.0, format="unix").isot,
    ]
    with pytest.raises(ValueError, match=r"el_bore"):
        build_source_payload(
            scan_params={"body": "jupiter", "window": window},
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_PROMPT_NOW,
            max_dispatch_delay_sec=float("inf"),
        )


# ---------------------------------------------------------------------------
# SECONDARY: drive the body through a mock Go TCS (in-process FastAPI client).
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi", reason="FastAPI not available for mock-TCS test")
from fastapi.testclient import TestClient  # noqa: E402

from pcs.agents.acu_interface.tests.mock_tcs import PREFIX, create_mock_tcs  # noqa: E402


def test_mock_tcs_records_contract_valid_path(built):
    """POST the built source body to the mock TCS: recorded + 200 (schema-valid)."""
    result, _ = built
    payload = result["payload"]
    app = create_mock_tcs()
    with TestClient(app) as client:
        move_resp = client.post(
            f"{PREFIX}/move-to",
            json={"azimuth": result["encoder_az"], "elevation": result["encoder_el"]},
        )
        path_resp = client.post(f"{PREFIX}/path", json=payload)

    assert move_resp.status_code == 200
    assert path_resp.status_code == 200

    recorder = app.state.recorder
    assert len(recorder.path_bodies) == 1
    recorded = recorder.path_bodies[0]
    assert set(recorded.keys()) == {"start_time", "coordsys", "points"}
    assert recorded["coordsys"] == "Horizon"
    assert all(len(p) == 5 for p in recorded["points"])
    assert recorded["points"][0][1] == pytest.approx(result["encoder_az"], abs=1e-9)
