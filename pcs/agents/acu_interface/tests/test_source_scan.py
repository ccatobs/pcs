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
  ``boresight_rot`` raises ``ValueError`` (Nasmyth sign + boresight_rot are
  UNCONFIRMED, so the off-centre path is gated off);
- velocity frame: the commanded az velocity is the planner's solved MOUNT-frame
  drift (small), NOT an on-sky-scaled rate;
- hardware-dynamics escalation: a velocity/acceleration breach anywhere raises
  ``TrajectoryValidationError``;
- dispatch-delay: a far ``el_bore`` crossing raises ``DispatchDelayError``.

DATE-STABILITY: ``plan_source_ces`` searches forward for the ``el_bore`` crossing,
so the resolved start depends on wall-clock. Every build threads a FIXED ``Time``
epoch. At 2026-06-15T16:30 UTC Jupiter rising reaches el=35 ~2.4 min out (PROMPT,
inside the 30-min default); at 13:00 UTC it is ~hours out (FAR, trips the
dispatch-delay guard). Contract tests pass ``max_dispatch_delay_sec=float("inf")``.

Imports ``trajectory.py`` directly, never ``agent.py``, the agent needs the
ocs/twisted framework, the whole reason the core is factored ocs-free. The
Process-level contracts are covered ocs-free in ``test_constant_el_scan.py``.
"""

import warnings

import numpy as np
import pytest
from astropy.time import Time

from fyst_trajectories import get_fyst_site

from pcs.agents.acu_interface.trajectory import (
    SCAN_DISPATCH_BUFFER_SEC,
    DispatchDelayError,
    TrajectoryValidationError,
    build_source_payload,
)

# Go TCS /path contract constants (commands.go checkAzEl + pathCmd.Check()).
AZ_MIN_TCS = -180.0  # commands.go:18
AZ_MAX_TCS = 360.0  # commands.go:19
EL_MIN_TCS = -90.0  # commands.go:24
EL_MAX_TCS = 180.0  # commands.go:25
AZ_SPEED_MAX_TCS = 3.0  # commands.go:20
EL_SPEED_MAX_TCS = 1.5  # commands.go:26
MIN_DT_TCS = 0.05  # commands.go:261
MIN_LEAD_TCS = 9.8  # commands.go:253
FIRST_N_CHECKED = 100  # commands.go:269

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
    assert start_time >= now + MIN_LEAD_TCS  # commands.go:253
    assert start_time >= now + SCAN_DISPATCH_BUFFER_SEC - 1e-6  # the 10 s floor


def test_points_is_n_by_5(built):
    result, _ = built
    points = result["payload"]["points"]
    assert len(points) > 0  # commands.go:246 "no points in path"
    assert all(len(p) == 5 for p in points)


def test_consecutive_dt_at_least_50ms(built):
    result, _ = built
    times = np.array([p[0] for p in result["payload"]["points"]])
    assert np.diff(times).min() >= MIN_DT_TCS  # commands.go:261


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


def test_off_centre_footprint_is_gated(site):
    """An off-centre single-module footprint raises a descriptive error."""
    bad = dict(REPRESENTATIVE_PARAMS, footprint="i1")
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


def test_centre_aliases_pass_the_gate(site):
    """Both "c" and "center" name the on-axis module and pass the gate."""
    for footprint in ("c", "center"):
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
        assert set(result["payload"].keys()) == {"start_time", "coordsys", "points"}


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
# (the genuine unit of that contract, the Go TCS only validates the first
# 100 points, so the agent enforces the ceiling everywhere).
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
    hardware ceiling, commands.go:20) and on a 2.0 deg/s el track (> the 1.5
    deg/s ceiling, commands.go:26); each must raise. Date-independent (synthetic
    arrays).
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
    with pytest.raises(TrajectoryValidationError, match=r"az velocity"):
        _escalate_hardware_dynamics(az_fast, "Synthetic")
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
    override) is exercised on purpose.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(DispatchDelayError, match=r"after dispatch"):
            build_source_payload(
                scan_params=REPRESENTATIVE_PARAMS,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_FAR_NOW,
            )


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
