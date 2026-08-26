"""Tests for the FYST Pong-scan dispatch core + Go TCS contract.

PRIMARY (no server, no ocs): drive
:func:`pcs.agents.acu_interface.trajectory.build_pong_payload` directly and assert
the returned ``/path`` body satisfies every rule the Go TCS enforces in
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

Plus the dispatch-core guards:

- Velocity frame: ``plan_pong_scan``'s ``velocity`` is ON-SKY (tangent-plane)
  deg/s, NOT mount-frame. The realised mount az velocity is ``~velocity /
  cos(el)``, *larger* than the requested on-sky value (opposite of the
  source/constant-el convention);
- Hardware-dynamics escalation: a velocity/acceleration breach anywhere raises
  ``TrajectoryValidationError``;
- Dispatch-delay: Pong takes ``start_time`` literally, so its resolved start
  equals the floored anchor and the guard passes trivially; the shared
  :func:`enforce_dispatch_delay` is tested directly for raising-when-far, and the
  buffer floor is verified.

DATE-STABILITY: Pong uses ``start_time`` literally, so the only date dependence is
whether the field is observable (sun-safety disabled in the fixture). Every build
threads a FIXED ``Time`` epoch. At 2026-06-15T13:00 UTC the RA=80, Dec=-40 field
sits at el~45, comfortably inside the FYST limits.

Imports ``trajectory.py`` directly, never ``agent.py``.
"""

import warnings

import numpy as np
import pytest
from astropy.time import Time

from fyst_trajectories import get_fyst_site

from pcs.agents.acu_interface.trajectory import (
    MAX_DISPATCH_DELAY_SEC,
    SCAN_DISPATCH_BUFFER_SEC,
    DispatchDelayError,
    TrajectoryValidationError,
    build_pong_payload,
    enforce_dispatch_delay,
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

# A representative Pong over a field well inside the FYST limits at the epoch.
REPRESENTATIVE_PARAMS = dict(
    ra_center=80.0,
    dec_center=-40.0,
    width=2.0,
    height=2.0,
    velocity=0.5,  # ON-SKY deg/s (tangent-plane)
    spacing=0.1,
    num_terms=4,
)

# Epoch at which the field is observable (el~45). Pong uses start_time literally
# so there is no forward-search dependence, only observability + sun. See
# module docstring.
FIXED_EPOCH = Time("2026-06-15T13:00:00", scale="utc")
FIXED_NOW = FIXED_EPOCH.unix


@pytest.fixture
def site():
    # Sun avoidance disabled so the wrap choice is purely geometric. The Go TCS
    # contract is independent of sun avoidance.
    return get_fyst_site(sun_avoidance_enabled=False)


@pytest.fixture
def built(site):
    """A built Pong payload from representative params at the fixed epoch."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_pong_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    return result, FIXED_NOW


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
    assert start_time > 1e9
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


def test_slew_target_in_range(built, site):
    result, _ = built
    assert site.telescope_limits.azimuth.is_in_range(result["encoder_az"])
    assert site.telescope_limits.elevation.is_in_range(result["encoder_el"])


def test_wrap_alignment_first_point_equals_encoder_az(built):
    result, _ = built
    assert result["payload"]["points"][0][1] == pytest.approx(result["encoder_az"], abs=1e-9)


# ---------------------------------------------------------------------------
# ON-SKY velocity (the realised mount az rate is larger, not equal).
# ---------------------------------------------------------------------------


def test_velocity_is_on_sky_not_mount_frame(built):
    """Pong velocity is on-sky, so the realised mount az rate exceeds it.

    Unlike constant-el/source (mount-frame pass-through), ``plan_pong_scan``
    takes an ON-SKY scan speed and maps it to the mount frame via the field
    geometry: the realised azimuth-coordinate rate is ``~velocity / cos(el)``,
    which at el~45 (cos ~0.71) inflates the requested 0.5 deg/s to ~0.7 deg/s.
    The peak |az velocity| in the posted body must therefore be GREATER than the
    requested on-sky velocity, the opposite of the source/constant-el frame.
    (A naive mount-frame pass-through would peak at ~0.5; this catches that
    regression.)
    """
    result, _ = built
    vaz = np.array([abs(p[3]) for p in result["payload"]["points"]])
    assert vaz.max() > REPRESENTATIVE_PARAMS["velocity"] + 1e-3


# ---------------------------------------------------------------------------
# Hardware-dynamics escalation (velocity / acceleration, whole trajectory).
# ---------------------------------------------------------------------------


def test_too_fast_velocity_escalates_h3(site):
    """An on-sky velocity that pushes the mount az rate past 3.0 deg/s raises.

    On-sky velocity 4.0 at el~45 -> mount az ~5.6 deg/s, well over the Go TCS
    hardware ceiling (3.0, commands.go:20). The agent must reject it BEFORE the
    POST (Go TCS only validates the first 100 points). Date-stable fixed ``now``.
    """
    fast = dict(REPRESENTATIVE_PARAMS, velocity=4.0, spacing=0.5)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TrajectoryValidationError, match=r"velocity|acceleration"):
            build_pong_payload(
                scan_params=fast,
                current_az=200.0,
                current_el=60.0,
                site=site,
                now_unix=FIXED_NOW,
                max_dispatch_delay_sec=float("inf"),
            )


# ---------------------------------------------------------------------------
# Dispatch-delay guard. Pong's start == the floored anchor, so the guard
# passes trivially on a real scan; the shared helper is tested directly for the
# raising-when-far behaviour.
# ---------------------------------------------------------------------------


def test_pong_passes_default_dispatch_delay_guard(site):
    """Pong's literal start sits just past the buffer, inside the 30-min default."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_pong_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            now_unix=FIXED_NOW,
        )  # default max_dispatch_delay_sec
    delay = result["payload"]["start_time"] - FIXED_NOW
    assert 0.0 < delay <= MAX_DISPATCH_DELAY_SEC


def test_shared_dispatch_delay_helper_raises_when_far():
    """The centralized dispatch-delay guard raises when a resolved start is too far out.

    Date-independent (pure arithmetic on supplied Unix times): a resolved start
    two hours past ``now`` exceeds the 30-min default and raises
    ``DispatchDelayError``. This is the same helper every typed task shares.
    """
    now = 1_000_000_000.0
    with pytest.raises(DispatchDelayError, match=r"after dispatch"):
        enforce_dispatch_delay(now + 7200.0, now, MAX_DISPATCH_DELAY_SEC, context="pong scan")
    # Within the bound it does not raise.
    enforce_dispatch_delay(now + 60.0, now, MAX_DISPATCH_DELAY_SEC, context="pong scan")


def test_scheduled_t0_in_past_is_floored(site):
    """The buffer floor advances a past scheduled_t0 to now + buffer."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = build_pong_payload(
            scan_params=REPRESENTATIVE_PARAMS,
            current_az=200.0,
            current_el=60.0,
            site=site,
            scheduled_t0_unix=FIXED_NOW - 1000.0,
            now_unix=FIXED_NOW,
            max_dispatch_delay_sec=float("inf"),
        )
    assert result["payload"]["start_time"] >= FIXED_NOW + SCAN_DISPATCH_BUFFER_SEC - 1e-6


# ---------------------------------------------------------------------------
# SECONDARY: drive the body through a mock Go TCS (in-process FastAPI client).
# ---------------------------------------------------------------------------

fastapi = pytest.importorskip("fastapi", reason="FastAPI not available for mock-TCS test")
from fastapi.testclient import TestClient  # noqa: E402

from pcs.agents.acu_interface.tests.mock_tcs import PREFIX, create_mock_tcs  # noqa: E402


def test_mock_tcs_records_contract_valid_path(built):
    """POST the built Pong body to the mock TCS: recorded + 200 (schema-valid)."""
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
