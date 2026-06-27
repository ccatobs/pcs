"""Tests for the FYST constant-elevation scan dispatch core + Go TCS contract.

PRIMARY (no server, no ocs): drive
:func:`pcs.agents.acu_interface.trajectory.build_constant_el_payload` directly and
assert the returned ``/path`` body satisfies *every* rule the Go TCS enforces in
``commands.go`` (``pathCmd.Check()`` + ``checkAzEl``):

- exactly the three keys ``{start_time, coordsys, points}``; ``coordsys ==
  "Horizon"``;
- ``start_time`` absolute Unix and >= now + 9.8 s (and >= now + 10 s, the
  :data:`SCAN_DISPATCH_BUFFER_SEC` floor);
- ``points`` is N x 5; every consecutive ``dt >= 0.05`` s;
- the first 100 points satisfy az in [-180, 360], el in [el_min, el_max],
  ``|vaz| <= 3.0``, ``|vel| <= 1.5``, velocities present (cols 3/4 non-null);
- the slew-target az is in range and ``points[0][1] == encoder_az`` (wrap align);
- a too-fast velocity raises (hardware-dynamics escalation).

Imports ``trajectory.py`` directly, never ``agent.py``, because the agent needs
the ``ocs`` / ``twisted`` framework, the whole reason the core is factored ocs-free.

SECONDARY (mock TCS, in-process): POST a built body through FastAPI's
``TestClient`` against :mod:`mock_tcs` and assert it is recorded + returns 200.
Skipped if FastAPI is unavailable; no live server (context-managed transport).

Dispatch-core guards covered here:

- acceleration: the quintic turnaround's peak az accel is ``1.5 * az_accel`` by
  design; a breach of the Go TCS hardware ceiling (6.0 deg/s^2) escalates to
  ``TrajectoryValidationError``.
- dispatch delay: a below-horizon field resolves a crossing many hours out;
  ``build_constant_el_payload`` rejects a resolved start more than
  ``max_dispatch_delay_sec`` out with ``DispatchDelayError`` (date-stable fixed
  ``Time``). The contract tests pass ``max_dispatch_delay_sec=float("inf")`` so
  the guard does not fire on REPRESENTATIVE_PARAMS (which resolve ~hours out).

Process-level contracts (NOT here, they need the ocs/twisted framework; the
DECISION logic is unit-tested ocs-free above and the runner is exercised in
``test_dispatch_runner.py``):

- completion: ``/path`` is fire-and-forget (HTTP 200 == accepted, not done; Go
  never calls back). The Process detects completion via the stack-drained signal
  (free == 9999 + near-zero velocities), backstopped by the absolute scan end
  time, so it releases ``azel_lock`` instead of spinning. ``mock_tcs`` exposes a
  drained ``GET /acu/status`` for a Process-level harness.
- position-unknown refusal: refuses to dispatch (False, RETRYABLE) when no live
  encoder position is available rather than guessing a wrong-wrap ``current_az``.
  The ``get_status()`` ``SystemExit`` (aculib raises it on ConnectionError) is
  trapped.
- slew-arrival gate: polls the broadcast until the dish reaches the slew target
  before POSTing ``/path`` (a premature POST returns 503), and fails on a non-200
  ``/path`` response.
"""

import time
import warnings

import numpy as np
import pytest
from astropy.time import Time

from fyst_trajectories import get_fyst_site
from fyst_trajectories.exceptions import ElevationBoundsError

from pcs.agents.acu_interface.trajectory import (
    MAX_REFLOOR_DRIFT_SEC,
    SCAN_DISPATCH_BUFFER_SEC,
    TCS_PROGRAM_TRACK_DRAINED,
    DispatchDelayError,
    ScanCompletionLatch,
    TrajectoryValidationError,
    build_constant_el_payload,
    refloor_drift_seconds,
    refloor_payload_start_time,
    tcs_response_status,
)

# ---------------------------------------------------------------------------
# Go TCS /path contract constants, transcribed from
# telescope-control-system/commands.go (checkAzEl + pathCmd.Check()).
# ---------------------------------------------------------------------------
AZ_MIN_TCS = -180.0  # commands.go:18 azimuthMin
AZ_MAX_TCS = 360.0  # commands.go:19 azimuthMax
EL_MIN_TCS = -90.0  # commands.go:24 elevationMin
EL_MAX_TCS = 180.0  # commands.go:25 elevationMax
AZ_SPEED_MAX_TCS = 3.0  # commands.go:20 azimuthSpeedMax
EL_SPEED_MAX_TCS = 1.5  # commands.go:26 elevationSpeedMax
MIN_DT_TCS = 0.05  # commands.go:261 minimum sample interval
MIN_LEAD_TCS = 9.8  # commands.go:253 program track starts too soon (< 9.8 s)
FIRST_N_CHECKED = 100  # commands.go:269 first 100 coordinates checked


# A representative rising constant-el scan well inside the FYST limits.
REPRESENTATIVE_PARAMS = dict(
    ra_center=80.0,
    dec_center=-40.0,
    width=3.0,
    height=3.0,
    elevation=50.0,
    velocity=0.5,  # azimuth-coordinate deg/s (mount frame)
    rising=True,
)

# DATE-STABILITY: the contract builds all plan REPRESENTATIVE_PARAMS (or
# distinct-geometry fields) whose el=50 crossings exist only at certain wall-clock
# times; on a date with no crossing in the 12 h search ``plan_constant_el_scan``
# raises ValueError before any assertion runs. Thread ONE fixed epoch through every
# build so the suite is deterministic regardless of run date.
#
# 2026-06-15T02:00 UTC is the single epoch at which *all* the contract builds
# resolve: REPRESENTATIVE_PARAMS (rising), the setting RA=0/Dec=-60 field in
# ``test_wrap_alignment_applies_nonzero_shift``, AND the ``now+3600`` anchor in
# ``test_scheduled_t0_is_a_search_anchor...`` (a near-term-crossing epoch like
# 13:00 breaks the +3600 case: consecutive rising crossings are ~a sidereal day
# apart). Contract tests pass ``max_dispatch_delay_sec=float("inf")`` so the
# crossing being hours out here is irrelevant; they assert the ``/path`` body,
# not timeliness; the dispatch-delay tests keep their own near/far epochs.
FIXED_EPOCH = Time("2026-06-15T02:00:00", scale="utc")
FIXED_NOW = FIXED_EPOCH.unix


@pytest.fixture
def site():
    # Sun avoidance disabled so the wrap choice is purely geometric and the
    # contract assertions are deterministic regardless of run date. The Go TCS
    # contract (bounds + velocity + timing) is independent of sun avoidance.
    return get_fyst_site(sun_avoidance_enabled=False)


@pytest.fixture
def built(site):
    """A built payload from representative params and a current az/el.

    Silences the advisory acceleration ``PointingWarning`` at the turnaround (the
    quintic peak accel is ``1.5 * az_accel`` by design, a real commanded value,
    not a sampling artifact); velocity escalation and the analytic accel guard are
    tested separately. Uses :data:`FIXED_NOW` for date-stability, with
    ``max_dispatch_delay_sec=float("inf")`` so the ``/path``-contract assertions do
    not couple to whether the crossing falls inside the default window.
    """
    now = FIXED_NOW
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_constant_el_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=now,
            max_dispatch_delay_sec=float("inf"),
        )
    return result, now


# ---------------------------------------------------------------------------
# PRIMARY: Go TCS /path contract (pathCmd.Check + checkAzEl)
# ---------------------------------------------------------------------------


def test_payload_has_exactly_three_keys(built):
    result, _ = built
    payload = result["payload"]
    # DisallowUnknownFields on the Go TCS receiver: exactly these three keys.
    assert set(payload.keys()) == {"start_time", "coordsys", "points"}


def test_coordsys_is_horizon(built):
    result, _ = built
    assert result["payload"]["coordsys"] == "Horizon"


def test_start_time_absolute_and_at_least_9p8s_out(built):
    result, now = built
    start_time = result["payload"]["start_time"]
    # Absolute Unix seconds (not relative): jsontime() in commands.go treats
    # values < 100000 as relative-to-now, so an absolute stamp must be large.
    assert start_time > 1e9
    # commands.go:253: rejected if < 9.8 s in the future.
    assert start_time >= now + MIN_LEAD_TCS


def test_start_time_respects_buffer_floor(built):
    result, now = built
    # SCAN_DISPATCH_BUFFER_SEC (10 s) floor, stronger than the 9.8 s reject.
    assert result["payload"]["start_time"] >= now + SCAN_DISPATCH_BUFFER_SEC - 1e-6


def test_points_is_n_by_5(built):
    result, _ = built
    points = result["payload"]["points"]
    assert len(points) > 0  # commands.go:246 "no points in path"
    assert all(len(p) == 5 for p in points)


def test_consecutive_dt_at_least_50ms(built):
    result, _ = built
    times = np.array([p[0] for p in result["payload"]["points"]])
    dt = np.diff(times)
    # commands.go:261: any pair closer than 0.05 s is rejected.
    assert dt.min() >= MIN_DT_TCS


def test_first_100_points_satisfy_checkAzEl(built, site):
    result, _ = built
    points = result["payload"]["points"]
    el_min = site.telescope_limits.elevation.min
    el_max = site.telescope_limits.elevation.max
    for i, p in enumerate(points[:FIRST_N_CHECKED]):
        t, az, el, vaz, vel = p
        # Position: Go TCS hardware bounds (checkAzEl).
        assert AZ_MIN_TCS <= az <= AZ_MAX_TCS, f"point {i}: az {az} out of TCS range"
        assert EL_MIN_TCS <= el <= EL_MAX_TCS, f"point {i}: el {el} out of TCS range"
        # Planning elevation also sits inside the (tighter) site limits.
        assert el_min <= el <= el_max, f"point {i}: el {el} out of site range"
        # Velocity: the quantity checkAzEl enforces.
        assert abs(vaz) <= AZ_SPEED_MAX_TCS, f"point {i}: |vaz| {vaz} > {AZ_SPEED_MAX_TCS}"
        assert abs(vel) <= EL_SPEED_MAX_TCS, f"point {i}: |vel| {vel} > {EL_SPEED_MAX_TCS}"


def test_velocities_present_and_non_null(built):
    result, _ = built
    points = result["payload"]["points"]
    for i, p in enumerate(points[:FIRST_N_CHECKED]):
        # Cols 3/4 must exist and be real numbers (the Go TCS path iterator
        # reads AzVel/ElVel from these columns).
        assert p[3] is not None and np.isfinite(p[3]), f"point {i}: az_vel null/non-finite"
        assert p[4] is not None and np.isfinite(p[4]), f"point {i}: el_vel null/non-finite"


def test_slew_target_in_range(built, site):
    result, _ = built
    az_limits = site.telescope_limits.azimuth
    el_limits = site.telescope_limits.elevation
    assert az_limits.is_in_range(result["encoder_az"])
    assert el_limits.is_in_range(result["encoder_el"])


def test_wrap_alignment_first_point_equals_encoder_az(built):
    result, _ = built
    # The posted trajectory and the slew target must share one az wrap.
    assert result["payload"]["points"][0][1] == pytest.approx(result["encoder_az"], abs=1e-9)


def test_wrap_alignment_applies_nonzero_shift(site):
    """Wrap alignment must hold even when the chosen wrap differs from the planner's.

    A setting scan of RA=0, Dec=-60 deg starts near az 190 deg, which has two
    in-range encoder images in [-180, 360]: 190 and -170. With the dish near
    -170, ``choose_encoder_solution`` picks the -170 wrap (nearer slew), so the
    whole trajectory must be shifted by -360 deg to start there. This exercises
    the non-trivial branch of the alignment that the representative fixture
    (shift == 0) does not.

    Date-stable: the setting RA=0, Dec=-60 crossing resolves at the shared
    :data:`FIXED_NOW` epoch.
    """
    now = FIXED_NOW
    params = dict(
        ra_center=0.0, dec_center=-60.0, width=3.0, height=3.0,
        elevation=50.0, velocity=0.5, rising=False,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_constant_el_payload(
            scan_params=params, current_az=-170.0, current_el=60.0,
            site=site, now_unix=now, max_dispatch_delay_sec=float("inf"),
        )
    enc_az = result["encoder_az"]
    points = result["payload"]["points"]
    # The chosen wrap is the negative one (nearer the dish at -170).
    assert enc_az < 0.0
    # First point is aligned to it ...
    assert points[0][1] == pytest.approx(enc_az, abs=1e-9)
    # ... and the *whole* shifted trajectory still satisfies the TCS az bounds.
    az = np.array([p[1] for p in points])
    assert az.min() >= AZ_MIN_TCS
    assert az.max() <= AZ_MAX_TCS


def test_too_fast_velocity_raises_h3(site):
    fast = dict(REPRESENTATIVE_PARAMS, velocity=4.0)  # > az speed limit 3.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TrajectoryValidationError, match="velocity"):
            build_constant_el_payload(
                scan_params=fast,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_NOW,
                max_dispatch_delay_sec=float("inf"),
            )


def test_too_high_accel_raises(site):
    """An az_accel whose quintic peak (1.5x) exceeds the HW ceiling raises.

    az_accel=5.0 -> peak 7.5 deg/s^2 > the Go TCS hardware ceiling of 6.0
    (commands.go:21). The guard is analytic (1.5 * az_accel), not np.gradient.
    """
    bad = dict(REPRESENTATIVE_PARAMS, az_accel=5.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TrajectoryValidationError, match=r"accel"):
            build_constant_el_payload(
                scan_params=bad,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_NOW,
                max_dispatch_delay_sec=float("inf"),
            )


def test_accel_at_hardware_boundary_passes(site):
    """az_accel=4.0 -> peak exactly 6.0 deg/s^2 passes (strict ``>`` threshold).

    Mirrors checkAzEl's ``> max`` convention for velocity: a value exactly at
    the ceiling is accepted.
    """
    ok = dict(REPRESENTATIVE_PARAMS, az_accel=4.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_constant_el_payload(
            scan_params=ok,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    assert set(result["payload"].keys()) == {"start_time", "coordsys", "points"}


def test_scheduled_t0_in_past_is_floored(site):
    now = FIXED_NOW
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_constant_el_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            scheduled_t0_unix=now - 1000.0,
            now_unix=now,
            max_dispatch_delay_sec=float("inf"),
        )
    assert result["payload"]["start_time"] >= now + SCAN_DISPATCH_BUFFER_SEC - 1e-6


def test_scheduled_t0_is_a_search_anchor_not_a_literal_start(site):
    """``scheduled_t0_unix`` is the planner's *search anchor*, not the literal start.

    ``plan_constant_el_scan`` searches forward from ``start_time`` for the
    field's elevation crossing and uses that crossing as the trajectory start
    (matching the library's documented ``start_time`` contract). So the posted
    ``start_time`` is the resolved crossing, always at or after the anchor,
    never before it, and a later anchor must not yield an earlier start
    (monotonicity). The Go-TCS-relevant invariant (start >= now + buffer) is
    asserted by the buffer-floor test and holds because crossing >= anchor >=
    now + buffer.

    Date-stable: both anchors (now+30, now+3600) resolve crossings at the shared
    :data:`FIXED_NOW` epoch.
    """
    now = FIXED_NOW
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        early = build_constant_el_payload(
            scan_params=REPRESENTATIVE_PARAMS, current_az=200.0, current_el=60.0,
            site=site, scheduled_t0_unix=now + 30.0, now_unix=now,
            max_dispatch_delay_sec=float("inf"),
        )["payload"]["start_time"]
        later = build_constant_el_payload(
            scan_params=REPRESENTATIVE_PARAMS, current_az=200.0, current_el=60.0,
            site=site, scheduled_t0_unix=now + 3600.0, now_unix=now,
            max_dispatch_delay_sec=float("inf"),
        )["payload"]["start_time"]
    # Resolved crossing is at/after the anchor, and a later anchor never moves
    # the start earlier.
    assert early >= now + 30.0 - 1e-6
    assert later >= early - 1e-6


def test_dispatch_delay_far_out_field_raises(site):
    """A field whose elevation crossing resolves hours out raises DispatchDelayError.

    Uses a FIXED ``now`` (not ``time.time()``) so the test is date-stable. At
    2026-06-15T02:00 UTC, REPRESENTATIVE_PARAMS (RA=80, Dec=-40, el=50, rising)
    resolve a crossing ~11 h out (far past the 30-min default), so the guard
    refuses to slew and hold ``azel_lock``. Default ``max_dispatch_delay_sec``
    (no override) is exercised on purpose.
    """
    now = Time("2026-06-15T02:00:00", scale="utc").unix
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(DispatchDelayError, match=r"after dispatch"):
            build_constant_el_payload(
                scan_params=REPRESENTATIVE_PARAMS,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=now,
            )


def test_dispatch_delay_prompt_field_passes(site):
    """A promptly-reachable crossing passes the default dispatch-delay guard.

    Date-stable fixed ``now``: at 2026-06-15T13:00 UTC the same field resolves a
    crossing ~16 min out (inside the 30-min default), so the build succeeds
    with no ``max_dispatch_delay_sec`` override.
    """
    now = Time("2026-06-15T13:00:00", scale="utc").unix
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_constant_el_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=now,
        )
    # Resolved start is at/after now and within the 30-min default window.
    delay = result["payload"]["start_time"] - now
    assert 0.0 < delay <= 1800.0


def test_velocity_passed_through_mount_frame_not_cos_el(site):
    """The commanded az velocity is the scan velocity, not cos(el)-scaled.

    At el=50 deg, cos(el)~0.64. A cos(el)-scaled value would read ~0.32 deg/s;
    the mount-frame pass-through must read ~0.50 (the requested velocity).
    """
    now = FIXED_NOW
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_constant_el_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=now,
            max_dispatch_delay_sec=float("inf"),
        )
    vaz = np.array([abs(p[3]) for p in result["payload"]["points"]])
    assert vaz.max() == pytest.approx(REPRESENTATIVE_PARAMS["velocity"], abs=1e-6)


def test_goal_elevation_out_of_range_raises(site):
    """An el below the FYST el_min (20) is rejected as a bounds breach.

    Pinned to the FIXED_NOW epoch (rising). On an *unfavourable* date the el=10
    field has no crossing in the 12 h search and ``plan_constant_el_scan`` would
    raise a transient ``ValueError``, a DIFFERENT failure that would let a real
    below-el_min regression slip through. At FIXED_NOW the el=10 crossing exists,
    so the build proceeds to ``validate_trajectory`` and the trajectory is
    rejected for leaving the elevation limits. Assert that specific path
    (``ElevationBoundsError``, a ``PointingError``, with a limits message),
    not merely "something raised".
    """
    bad = dict(REPRESENTATIVE_PARAMS, elevation=10.0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ElevationBoundsError, match=r"exceeds limits"):
            build_constant_el_payload(
                scan_params=bad,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_NOW,
            )


# ---------------------------------------------------------------------------
# ScanCompletionLatch: the stack-drained completion decision (driven ocs-free).
# The headline test reproduces the OLD false-positive (an empty/drained stack read
# on the first poll after POST, before the scan ran) and confirms the latch
# suppresses it.
# ---------------------------------------------------------------------------


def test_latch_drained_from_start_does_not_false_complete():
    """Regression: a drained reading on the first poll must NOT report complete.

    Reproduces the bug the old ``free >= 9999`` check had: right after POST the
    Go TCS stack can read drained (the empty PRE-upload stack is free == 10000,
    and a transient free == 9999 can be seen before the upload goroutine pushes
    points), with both axes still stopped. The old check would have fired
    "complete" on poll #1 and released ``azel_lock`` for a scan that never ran.
    The latch refuses any drained reading until the scan is observed RUNNING.
    """
    start = 10_000.0  # scan starts well in the future relative to the polls
    latch = ScanCompletionLatch(start)
    # Poll #1: empty pre-upload stack (10000), axes stopped, well before start.
    assert latch.update(free=10_000, vaz=0.0, vel=0.0, now_unix=start - 100.0) is False
    # Poll #2: even a strict-9999 reading is not honored pre-arm.
    assert latch.update(free=9999, vaz=0.0, vel=0.0, now_unix=start - 99.0) is False
    assert latch.running_observed is False


def test_latch_arms_on_running_then_completes_on_drain():
    """Once the scan is observed running (non-empty stack), a later drain completes."""
    start = 10_000.0
    latch = ScanCompletionLatch(start)
    # Non-empty stack with az moving -> arms the latch, not yet complete.
    assert latch.update(free=5000, vaz=0.4, vel=0.0, now_unix=start - 50.0) is False
    assert latch.running_observed is True
    # Now strictly drained + axes stopped -> complete.
    done = latch.update(
        free=TCS_PROGRAM_TRACK_DRAINED, vaz=0.0, vel=0.0, now_unix=start - 40.0
    )
    assert done is True


def test_latch_arms_on_program_track_mode():
    """ProgramTrack mode (any axis) arms the latch even if the stack never reads non-empty."""
    start = 10_000.0
    latch = ScanCompletionLatch(start)
    # Drained read but ProgramTrack reported -> armed, and (drained + stopped) completes.
    done = latch.update(
        free=9999,
        vaz=0.0,
        vel=0.0,
        now_unix=start - 10.0,
        az_mode="ProgramTrack",
        el_mode="Stop",
    )
    assert done is True
    assert latch.running_observed is True


def test_latch_arms_on_wallclock_past_start():
    """Reaching the scan start_time arms the latch (covers a silent status stream)."""
    start = 10_000.0
    latch = ScanCompletionLatch(start)
    # Drained read at/after start_time -> armed via wall-clock, completes.
    assert latch.update(free=9999, vaz=0.0, vel=0.0, now_unix=start + 1.0) is True


def test_latch_drained_but_axis_moving_not_complete():
    """An armed, drained stack with an axis still slewing is NOT complete."""
    start = 10_000.0
    latch = ScanCompletionLatch(start)
    latch.update(free=3000, vaz=0.5, vel=0.0, now_unix=start - 20.0)  # arm
    # Stack drained but az still above the speed tolerance -> keep waiting.
    assert latch.update(free=9999, vaz=0.5, vel=0.0, now_unix=start - 10.0) is False


def test_latch_missing_fields_not_complete():
    """A status read missing the free-stack key never reports complete."""
    start = 10_000.0
    latch = ScanCompletionLatch(start)
    latch.update(free=3000, vaz=0.4, vel=0.0, now_unix=start + 1.0)  # arm
    assert latch.update(free=None, vaz=0.0, vel=0.0, now_unix=start + 2.0) is False


# ---------------------------------------------------------------------------
# tcs_response_status: 503-safe HTTP status read. aculib.post() returns {} (not
# a Response) on a 503; this maps it to a graceful "scan not launched" instead of
# an AttributeError on {}.status_code. Driven directly (ocs-free).
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def test_tcs_response_status_503_returns_none():
    """A 503 short-circuits to {} in aculib.post(); the helper maps it to None != 200."""
    assert tcs_response_status({}) is None  # the literal {} aculib returns on 503
    assert tcs_response_status(None) is None
    # The Process guard is ``code != 200``; None is correctly treated as rejected.
    assert tcs_response_status({}) != 200


def test_tcs_response_status_reads_real_response():
    assert tcs_response_status(_FakeResponse(200)) == 200
    assert tcs_response_status(_FakeResponse(503)) == 503


# ---------------------------------------------------------------------------
# refloor_payload_start_time: re-apply the dispatch floor after the slew.
# ---------------------------------------------------------------------------


def test_refloor_advances_a_stale_start_time():
    """A near-now start that the slew ate into is advanced to now + buffer."""
    # start_time only just above the original floor; a long slew has since run.
    payload = {
        "start_time": 1000.0 + SCAN_DISPATCH_BUFFER_SEC,
        "coordsys": "Horizon",
        "points": [[0.0, 100.0, 50.0, 0.5, 0.0]],
    }
    now_after_slew = 1000.0 + 150.0  # 150 s of slewing elapsed
    refloor_payload_start_time(payload, now_after_slew)
    assert payload["start_time"] == pytest.approx(now_after_slew + SCAN_DISPATCH_BUFFER_SEC)


def test_refloor_never_moves_a_future_start_earlier():
    """A start comfortably in the future is left untouched (floor never regresses it)."""
    payload = {
        "start_time": 5000.0,
        "coordsys": "Horizon",
        "points": [[0.0, 100.0, 50.0, 0.5, 0.0]],
    }
    refloor_payload_start_time(payload, 1000.0)  # now + buffer == 1010 << 5000
    assert payload["start_time"] == 5000.0


# ---------------------------------------------------------------------------
# refloor_drift_seconds: how far the re-floor advanced start_time (= the
# sidereal staleness of the baked az/el track). The runner refuses to POST when
# this exceeds MAX_REFLOOR_DRIFT_SEC.
# ---------------------------------------------------------------------------


def test_refloor_drift_seconds_reports_a_stale_advance():
    """A near-floor start that a long slew ate into reports the full advance.

    Mirrors ``test_refloor_advances_a_stale_start_time``: build a start just
    above the original floor, re-floor with a now that ran 150 s later, and check
    the drift equals how far start_time moved (~140 s)."""
    original = 1000.0 + SCAN_DISPATCH_BUFFER_SEC
    payload = {
        "start_time": original,
        "coordsys": "Horizon",
        "points": [[0.0, 100.0, 50.0, 0.5, 0.0]],
    }
    now_after_slew = 1000.0 + 150.0  # 150 s of slewing elapsed
    refloor_payload_start_time(payload, now_after_slew)
    drift = refloor_drift_seconds(original, payload)
    # start_time went from (1000+buffer) to (now_after_slew+buffer); the delta is
    # exactly now_after_slew - 1000 == 150 s.
    assert drift == pytest.approx(150.0)


def test_refloor_drift_seconds_zero_for_a_future_start():
    """A comfortably-future start is untouched by the floor, so drift is 0."""
    original = 5000.0
    payload = {
        "start_time": original,
        "coordsys": "Horizon",
        "points": [[0.0, 100.0, 50.0, 0.5, 0.0]],
    }
    refloor_payload_start_time(payload, 1000.0)  # now + buffer << 5000, no-op
    assert refloor_drift_seconds(original, payload) == 0.0


def test_refloor_drift_seconds_never_negative():
    """The re-floor never regresses start_time, so drift is clamped at >= 0 even
    if asked about an original that is somehow already past the (untouched) start."""
    payload = {"start_time": 5000.0, "coordsys": "Horizon", "points": [[0.0, 1.0, 2.0, 0.0, 0.0]]}
    # original AFTER the (unchanged) start: a degenerate caller; result floors at 0.
    assert refloor_drift_seconds(6000.0, payload) == 0.0


def test_refloor_drift_seconds_boundary_around_the_cap():
    """Drift straddling MAX_REFLOOR_DRIFT_SEC: just-under is tolerated, just-over
    is what the runner rejects. Pins that the helper measures the quantity the
    cap is compared against (drift > cap -> refuse)."""
    eps = 0.5
    base = 1000.0
    # A payload whose start sits cap-eps above the original -> drift == cap-eps.
    under = {"start_time": base + MAX_REFLOOR_DRIFT_SEC - eps, "coordsys": "Horizon",
             "points": [[0.0, 1.0, 2.0, 0.0, 0.0]]}
    assert refloor_drift_seconds(base, under) == pytest.approx(MAX_REFLOOR_DRIFT_SEC - eps)
    assert refloor_drift_seconds(base, under) <= MAX_REFLOOR_DRIFT_SEC  # NOT refused
    over = {"start_time": base + MAX_REFLOOR_DRIFT_SEC + eps, "coordsys": "Horizon",
            "points": [[0.0, 1.0, 2.0, 0.0, 0.0]]}
    assert refloor_drift_seconds(base, over) == pytest.approx(MAX_REFLOOR_DRIFT_SEC + eps)
    assert refloor_drift_seconds(base, over) > MAX_REFLOOR_DRIFT_SEC  # refused


# ---------------------------------------------------------------------------
# SECONDARY: drive the body through a mock Go TCS (in-process FastAPI client).
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi", reason="FastAPI not available for mock-TCS test")
from fastapi.testclient import TestClient  # noqa: E402

from pcs.agents.acu_interface.tests.mock_tcs import PREFIX, create_mock_tcs  # noqa: E402


def test_mock_tcs_records_contract_valid_path(built):
    """POST the built body to the mock TCS and assert it is recorded + 200.

    Uses FastAPI's in-process TestClient, no live server, no open port. The
    mock's ``PathParameters`` model (Literal["Horizon","ICRS"], 5-wide rows)
    mirrors the real OCS proxy, so a 200 means the body is schema-valid.
    """
    result, _ = built
    payload = result["payload"]
    app = create_mock_tcs()
    with TestClient(app) as client:
        # Slew first (move_to), then POST the path: the task's order.
        move_resp = client.post(
            f"{PREFIX}/move-to",
            json={"azimuth": result["encoder_az"], "elevation": result["encoder_el"]},
        )
        path_resp = client.post(f"{PREFIX}/path", json=payload)

    assert move_resp.status_code == 200
    assert path_resp.status_code == 200

    recorder = app.state.recorder
    assert len(recorder.move_to_bodies) == 1
    assert len(recorder.path_bodies) == 1
    recorded = recorder.path_bodies[0]
    assert set(recorded.keys()) == {"start_time", "coordsys", "points"}
    assert recorded["coordsys"] == "Horizon"
    assert all(len(p) == 5 for p in recorded["points"])
    assert recorded["points"][0][1] == pytest.approx(result["encoder_az"], abs=1e-9)


def test_mock_tcs_rejects_wrong_coordsys():
    """A non-Horizon/ICRS coordsys is a schema violation (HTTP 422).

    Confirms the mock actually validates the contract rather than rubber-stamping.
    """
    app = create_mock_tcs()
    bad_body = {"start_time": time.time() + 100.0, "coordsys": "Galactic",
                "points": [[0.0, 100.0, 50.0, 0.5, 0.0]]}
    with TestClient(app) as client:
        resp = client.post(f"{PREFIX}/path", json=bad_body)
    assert resp.status_code == 422


def test_mock_tcs_abort_recorded():
    app = create_mock_tcs()
    with TestClient(app) as client:
        resp = client.post(f"{PREFIX}/abort")
    assert resp.status_code == 200
    assert app.state.recorder.abort_count == 1


# ---------------------------------------------------------------------------
# Process-level (constant_el_scan): SKIP-GUARDED. The Process body needs the
# ocs/twisted framework, unimportable here; each DECISION is unit-tested ocs-free
# above, so this just confirms the agent imports and wires the four helpers where
# ocs IS available (CI/Linux). SKIPPED here.
# ---------------------------------------------------------------------------

# NB: a module named ``ocs`` IS importable here, the OCS REST *client*, NOT the
# SO operations *framework* the agent needs, so ``importorskip("ocs")`` would
# not skip. Guard per-test on the submodule the agent imports (``ocs.ocs_agent``)
# so the skip is correct and local.
def _ocs_framework_available() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("ocs.ocs_agent") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


@pytest.mark.skipif(
    not _ocs_framework_available(),
    reason="ocs operations framework not importable; Process-level test skipped",
)
def test_agent_module_imports_and_wires_helpers():
    """The agent module imports and references the four dispatch helpers.

    Skipped where ocs is unavailable. This does not exercise the Process loop
    (that needs a live reactor + session); it guards against the agent drifting
    out of sync with the ocs-free helpers it now depends on.
    """
    import inspect

    from pcs.agents.acu_interface import agent as agent_mod

    src = inspect.getsource(agent_mod)
    # The Process delegates completion to the latch and reads HTTP status + the
    # re-floor through the ocs-free helpers; _safe_abort wraps the abort calls.
    assert "ScanCompletionLatch(" in src
    assert "tcs_response_status(" in src
    assert "refloor_payload_start_time(" in src
    assert "_safe_abort(" in src
