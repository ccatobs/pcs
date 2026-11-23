"""OCS-free trajectory helpers for the PCS ACU-interface agent.

Dispatch-time *core* of the FYST typed scan tasks. Imports only
:mod:`fyst_trajectories` (with numpy and astropy), the standard library and
``aculib`` (for ``SCAN_DISPATCH_BUFFER_SEC``; it needs ``requests`` and
``yaml``), deliberately **no** ``ocs`` / ``twisted``, so it is unit-testable in
the minimal environment where the OCS operations framework (and hence the agent
module that wraps these helpers) is unavailable.

A typed PCS task calls ``fyst_trajectories`` at
*dispatch* time to build a full az/el trajectory, then POSTs it to the FYST Go
TCS ``/path`` endpoint. The Go TCS owns refraction and the hardware bounds; this
layer produces a body the Go TCS ``pathCmd.Check()`` will accept plus a sun-safe
encoder slew target to reach the trajectory's start.

Four typed tasks share this core, one ocs-free ``build_*_payload`` each
(:func:`build_constant_el_payload`, :func:`build_source_payload`,
:func:`build_pong_payload`, :func:`build_daisy_payload`). They differ only in
which planner they call and which velocity frame it expects: constant-el and
source take MOUNT-frame azimuth-coordinate deg/s (``cos(el)`` already applied
upstream, never re-applied); pong and daisy take ON-SKY deg/s.

Shared across all four: the dispatch-window helpers (:func:`floor_dispatch_start`
buffer floor, :func:`enforce_dispatch_delay` too-far-out guard,
:func:`refloor_payload_start_time` post-slew re-floor); the sun-safe slew target
+ 360-deg wrap alignment (:func:`_choose_slew_and_align`); and the dynamics
escalation against the Go TCS hardware ceilings (:func:`_escalate_velocity_warning`
for constant-el, :func:`_escalate_hardware_dynamics` for pong, daisy and source),
with constant-el and source adding an analytic ``1.5 * az_accel`` quintic
turnaround guard; the Sun exclusion-zone check of every sample
(:func:`_escalate_sun_exclusion`); the ``module`` key of the three field scans,
resolved to the position the scan is planned for (:func:`tracking_offset`); and
the ``scan_params`` keys each builder reads (:data:`SCAN_PARAM_KEYS`,
:func:`unknown_scan_param_keys`). The agent's
``az_scan`` task, which sends a Go TCS ``/azimuth-scan`` rather than a path,
reads its ``scan_params`` with :func:`parse_az_scan_params` and checks its
pattern against the Sun with :func:`az_scan_sun_breach`. At start the agent checks
the Earth-orientation data all of these need with :func:`earth_orientation_note`.
"""

import datetime
import math
import warnings

import numpy as np
from astropy import units as u
from astropy.time import Time

from fyst_trajectories import InstrumentOffset, check_earth_orientation_data, resolve_module_tag
from fyst_trajectories.coordinates import Coordinates
from fyst_trajectories.dispatch import choose_encoder_solution
from fyst_trajectories.exceptions import PointingError, VelocityLimitWarning
from fyst_trajectories.patterns import rewrap_trajectory_azimuth
from fyst_trajectories.planning import (
    FieldRegion,
    inflate_footprint,
    plan_constant_el_scan,
    plan_daisy_scan,
    plan_pong_scan,
    plan_source_ces,
    resolve_footprint,
)
from fyst_trajectories.site import Site
from fyst_trajectories.trajectory_utils import to_path_payload, validate_trajectory

from pcs.agents.acu_interface.aculib import SCAN_DISPATCH_BUFFER_SEC

#: Go TCS *hardware* acceleration ceilings (deg/s^2), NOT the conservative site
#: limits in fyst_trajectories.site (az 1.5, el 0.75 at v0.14.0); the dispatch
#: accel guard checks hardware so nominal scans pass.
TCS_AZ_MAX_ACCELERATION: float = 6.0  # azimuthAccelMax at TCS b7bbbf9
TCS_EL_MAX_ACCELERATION: float = 1.5  # elevationAccelMax at TCS b7bbbf9

#: Go TCS *hardware* velocity ceilings (deg/s), the quantity ``checkAzEl``
#: actually enforces (strict ``> max``); :func:`_escalate_hardware_dynamics`
#: says why it checks against these, not the site limits.
TCS_AZ_MAX_VELOCITY: float = 3.0  # azimuthSpeedMax at TCS b7bbbf9
TCS_EL_MAX_VELOCITY: float = 1.5  # elevationSpeedMax at TCS b7bbbf9

#: Free program-track stack count of a played-out track: one point left of
#: ``maxFreeProgramTrackStack`` (10000), tested with strict ``==`` as the isDone closure in
#: ``startPattern`` at TCS ``b7bbbf9`` does, since an empty stack reads 10000. A cleared
#: stack can read 9999 as well: the ACU simulator kept the last point through a clear until
#: antenna-control-unit ``116d7b9`` (v0.8.0), and the real ACU's count after a clear is not
#: established, so :class:`ScanCompletionLatch` trusts the count only once armed.
TCS_PROGRAM_TRACK_DRAINED: int = 9999  # maxFreeProgramTrackStack - 1

#: Axis-velocity magnitude (deg/s) below which an axis is "stopped".
TCS_SPEED_TOL: float = 1e-4  # speedTol at TCS b7bbbf9

#: ``start_time`` threshold of ``jsontime`` at TCS ``b7bbbf9``: a value below it is
#: seconds from the Go TCS's "now", any other absolute Unix seconds.
TCS_RELATIVE_TIME_MAX: float = 100000.0

#: The Go TCS's HTTP 503 message when its main loop does not take a command
#: within ``commandBusyTimeout`` (500 ms at TCS ``b7bbbf9``), as while a previous
#: command is still running; the one command refusal worth retrying.
TCS_BUSY_MESSAGE: str = "busy"

#: The Go TCS's refusal of a start under ~9.8 s out (``checkProgramTrackStartTime``
#: at TCS ``b7bbbf9``), matched as a substring of the relayed message.
TCS_START_TOO_SOON_MESSAGE: str = "program track starts too soon"

#: The Go TCS's HTTP 409 message to an ``/abort`` while it runs no command (its
#: ``/abort`` handler at TCS ``b7bbbf9``).
TCS_NOTHING_TO_ABORT_MESSAGE: str = "nothing to abort"

#: Characters of a reply that is not the Go TCS's quoted in a failure message.
TCS_REPLY_QUOTE_CHARS: int = 80

#: The status a 200 that is not the Go TCS's reply reads as: Bad Gateway, an
#: intermediary answered, so whether the command reached the TCS is unknown.
TCS_FOREIGN_REPLY_STATUS: int = 502

#: Maximum allowed delay (s) between dispatch and the resolved scan start.
#: The field form of plan_constant_el_scan treats start_time as a forward-search anchor, so a
#: below-horizon field can resolve a crossing many hours out (up to
#: max_search_hours, default 12 h). The PCS task slews to the scan start
#: immediately and holds azel_lock until start_time, so an unbounded delay parks
#: the dish on empty sky for hours and blocks all other azel ops. Applied by all four
#: builders. The root-semantics fix (take the elevation from upstream so
#: the resolve is always ~now and this cap rarely binds) is deferred to the
#: survey-to-execution handoff; this is the interim guard, not its substitute.
MAX_DISPATCH_DELAY_SEC: float = 1800.0  # 30 min

#: Maximum spacing (s) between Sun checks over the pre-scan dwell, while the dish
#: holds the scan-start az/el; the Sun moves at most 0.25 deg/min, so at most
#: 1.25 deg between checks.
SUN_DWELL_CHECK_STEP_SEC: float = 300.0  # 5 min

#: Maximum spacing (s) of the grid the Sun is solved on for :func:`sun_exclusion_breach`;
#: its unit vector, interpolated to every sample, is good over 60 s to a small
#: fraction of an arcsecond.
SUN_EXCLUSION_GRID_SEC: float = 60.0

#: Maximum spacing (deg) of the azimuths at which :func:`az_scan_sun_breach`
#: samples an azimuth scan's arc.
AZ_SCAN_SUN_ARC_STEP_DEG: float = 0.5

#: Maximum spacing (s) of the times at which :func:`az_scan_sun_breach` places the Sun:
#: it moves at most 0.25 deg/min, so with the arc step above the closest
#: approach is found to about 0.2 deg; nothing is interpolated.
AZ_SCAN_SUN_GRID_SEC: float = 60.0

#: The form of the time a Sun refusal names: ISO 8601 UTC with a Z, to the second.
_SUN_REFUSAL_TIME_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"

#: Maximum post-slew re-floor advance (s) of a scan's start_time before its baked
#: az/el track is too stale to POST. Advancing start_time by ``delta`` replays the
#: samples ``delta`` s late, so a sidereal track lags the sky by up to 15 arcsec/s
#: times ``delta``: 30 s caps the lag at 0.125 deg, a fifth of a PrimeCam module's
#: 0.65 deg FOV radius (1/cos(el) times that in azimuth coordinate), and leaves the
#: Sun checks up to 0.125 deg of solar motion behind. A source CES planned with no
#: padding loses up to ``delta`` of its crossing. ``delta`` is 0 unless a
#: dispatch-now scan's slew ate its lead. The constant-el window form is timed by its
#: start alone, so for it the bound only limits how late the scan runs; the field form
#: sweeps its fixed range while the field crosses it, so a late start loses up to
#: ``delta`` of the crossing.
MAX_REFLOOR_DRIFT_SEC: float = 30.0

#: Relative shortfall within which a constant-el window form's duration still counts
#: as one leg: the leg time is a difference of two azimuths over a rate, exact only
#: to rounding.
ONE_LEG_RELATIVE_TOLERANCE: float = 1e-9

#: How far past the agent's start (days) astropy's Earth-orientation table must reach,
#: so that a table set by hand and about to run out is reported two days early.
#: Astropy's refusal of a table whose predictions are 30 days old is judged by the
#: host's clock at each transform, which a check at start cannot see.
EARTH_ORIENTATION_MIN_COVERAGE_DAYS: float = 2.0


class TrajectoryValidationError(RuntimeError):
    """Raised when a built trajectory breaches a Go TCS limit or the Sun exclusion zone.

    A velocity/acceleration breach anywhere in the trajectory is turned into a
    hard failure before the body is POSTed, because the Go TCS checks the first
    100 points before accepting a path and the rest only as it uploads them, and
    ``validate_trajectory`` only warns.
    """


class DispatchDelayError(RuntimeError):
    """Raised when the resolved scan start_time is too far past dispatch."""


def floor_dispatch_start(scheduled_t0_unix: float | None, now_unix: float) -> Time:
    """Apply the dispatch-buffer floor to a scan's start anchor (shared).

    Returns ``max(scheduled_t0_unix or 0, now_unix + SCAN_DISPATCH_BUFFER_SEC)``
    as an absolute ``astropy.time.Time``, the ``start_time`` the planners take.
    The floor guarantees the search anchor (and so the posted ``start_time``,
    which is at or after it) clears the Go TCS minimum lead (``< ~9.8 s``
    rejected by ``checkProgramTrackStartTime`` at TCS ``b7bbbf9``). Used by every
    typed scan task's payload core.

    ``scheduled_t0_unix`` is ``None`` to dispatch as soon as the buffer allows;
    a past value is floored away. ``now_unix`` is caller-supplied so the helper
    stays deterministic and testable.
    """
    actual_t0_unix = max(scheduled_t0_unix or 0.0, now_unix + SCAN_DISPATCH_BUFFER_SEC)
    return Time(actual_t0_unix, format="unix")


def enforce_dispatch_delay(
    resolved_start_unix: float,
    now_unix: float,
    max_dispatch_delay_sec: float,
    *,
    context: str = "scan",
    anchor_unix: float | None = None,
) -> None:
    """Reject a scan whose resolved start is too far past dispatch (shared).

    The elevation-searching planners (the field form of ``plan_constant_el_scan``,
    ``plan_source_ces``) treat ``start_time`` as a forward-search anchor, so a
    below-horizon field can resolve a crossing many hours out (up to
    ``max_search_hours``). The task slews to the scan start immediately and holds
    ``azel_lock`` until ``start_time``, so an unbounded delay parks the dish on
    empty sky for hours and blocks all other azel ops. Raise before any slew is
    computed. Pong and daisy take ``start_time`` literally (no forward search),
    so their resolved start equals the floored anchor and this bounds the
    scheduled start itself; applied uniformly so all four tasks share one path.

    Pass ``max_dispatch_delay_sec=float("inf")`` to disable (e.g. contract tests
    not concerned with timeliness). ``context`` is woven into the error message,
    whose advice suits the elevation-searching planners; when ``anchor_unix``, the
    floored anchor, is itself past the bound, the advice is to dispatch closer to
    the start.

    Raises
    ------
    DispatchDelayError
        If ``resolved_start_unix - now_unix`` exceeds ``max_dispatch_delay_sec``.
    """
    delay = resolved_start_unix - now_unix
    if delay > max_dispatch_delay_sec:
        if anchor_unix is not None and anchor_unix - now_unix > max_dispatch_delay_sec:
            hint = "A scheduled start this far out must be dispatched closer to its time."
        else:
            hint = (
                "The target does not reach the requested geometry promptly; a scan-now "
                "dispatch must pass a promptly-reachable elevation."
            )
        raise DispatchDelayError(
            f"Resolved {context} start_time is {delay / 3600:.2f} h after dispatch "
            f"(> {max_dispatch_delay_sec / 3600:.2f} h limit). {hint} "
            f"Refusing to slew and hold azel_lock."
        )


def earth_orientation_note(now_unix: float) -> tuple[bool, str]:
    """Check astropy's Earth-orientation (IERS) data at agent start; return what to log.

    Every sky-to-horizon transform of the typed scans and the ``az_scan`` Sun check
    needs UT1-UTC from astropy's IERS table. This asks
    :func:`fyst_trajectories.check_earth_orientation_data` whether the table reaches
    :data:`EARTH_ORIENTATION_MIN_COVERAGE_DAYS` past ``now_unix`` (Unix seconds),
    which also makes astropy load the table, downloading one when it has none or
    judges its own stale, so this can block for astropy's network timeouts.

    Returns ``(True, note)`` naming the date the table runs to, or ``(False, note)``
    with the reason. It never raises: a start-up check must not stop the agent, so
    an error other than the library's verdict is reported the same way.
    """
    try:
        covered_until = check_earth_orientation_data(
            Time(now_unix, format="unix"), days=EARTH_ORIENTATION_MIN_COVERAGE_DAYS
        )
    except PointingError as exc:
        return False, (
            f"Earth-orientation data are not usable: {exc} Until this is fixed, typed "
            f"scans and the az_scan Sun check fail or run with degraded accuracy."
        )
    except Exception as exc:  # astropy's table code: a download, a parse, a setting
        return False, f"Earth-orientation data check could not run: {exc!r}"
    return True, (
        f"Earth-orientation (IERS) data usable at start: astropy's table runs to "
        f"{covered_until.utc.strftime('%Y-%m-%d')}."
    )


#: ACU az/el mode strings that count as ProgramTrack. Matched
#: case-insensitively as a substring so vendor variants ("ProgramTrack",
#: "Program Track", "ProgramTrackTime") all count.
_PROGRAM_TRACK_MODE_TOKEN = "programtrack"


def mode_is_program_track(mode) -> bool:
    """Return whether an axis mode string names ProgramTrack (vendor spellings)."""
    if mode is None:
        return False
    normalized = str(mode).strip().lower().replace(" ", "")
    return _PROGRAM_TRACK_MODE_TOKEN in normalized


#: ACU az/el mode string of a move-to (``AzimuthModePreset`` in the ACU
#: datasets), matched as :func:`mode_is_program_track` matches its own.
_PRESET_MODE_TOKEN = "preset"


def mode_is_preset(mode) -> bool:
    """Return whether an axis mode string names Preset (vendor spellings)."""
    if mode is None:
        return False
    return _PRESET_MODE_TOKEN in str(mode).strip().lower().replace(" ", "")


class ScanCompletionLatch:
    """Decide when a POSTed ``/path`` scan has genuinely completed.

    The Go TCS /path endpoint is fire-and-forget: an accepted ``/path`` is
    queued, not done, and the Go TCS never calls back. The PCS Process detects
    completion by polling the ACU status for the stack-drained signal
    (:data:`TCS_PROGRAM_TRACK_DRAINED`). ``startPattern`` clears the stack before
    its upload goroutine fills it, so a poll before the first points land reads
    drained, in ProgramTrack too at TCS ``dd7151b`` (TCS ``b7bbbf9`` sets
    ProgramTrack only after the first batch). A drained reading therefore counts
    only once THIS track has been seen: points on the stack while an axis is in
    ProgramTrack (or the read carries no mode; in Stop or Preset they are a stopped
    track's leftovers), or the wall clock reaching ``start_time``. This is the latch.

    A tiny mutable state machine so the agent's poll loop stays a thin wrapper and
    the decision is unit-testable without ``ocs`` / ``twisted``. Feed each poll's
    status to :meth:`update`; it returns ``True`` exactly once the scan is
    confirmed complete. The agent keeps its own absolute-time backstop and abort
    handling around this.

    ``start_time_unix`` is the POSTed trajectory's absolute start; reaching it
    arms the latch even if no read ever shows points on the stack.
    ``drained_free`` /``speed_tol`` default to :data:`TCS_PROGRAM_TRACK_DRAINED`
    / :data:`TCS_SPEED_TOL`.
    """

    def __init__(
        self,
        start_time_unix: float,
        *,
        drained_free: int = TCS_PROGRAM_TRACK_DRAINED,
        speed_tol: float = TCS_SPEED_TOL,
    ) -> None:
        self.start_time_unix = float(start_time_unix)
        self.drained_free = int(drained_free)
        self.speed_tol = float(speed_tol)
        #: Set once the scan has been observed running at least once. Until
        #: then a drained reading is NOT accepted (guards the first-poll race).
        self.running_observed = False

    def update(
        self,
        *,
        free,
        vaz,
        vel,
        now_unix: float,
        az_mode=None,
        el_mode=None,
    ) -> bool:
        """Fold one status poll into the latch; return ``True`` if complete.

        ``free`` is the free program-track stack count (the status field
        ``QtyOfFreeProgramTrackStackPositions``); ``vaz``/``vel`` the
        az/el current velocity (deg/s); ``az_mode``/``el_mode`` the axis modes.
        Any may be ``None`` if the read did not surface it.

        Returns ``True`` exactly when the latch has armed (running observed) AND
        the stack is strictly drained AND both axis velocities are below
        ``speed_tol``.
        """
        # Arm only on evidence of THIS track (see the class docstring).
        modes_known = az_mode is not None or el_mode is not None
        in_program_track = (mode_is_program_track(az_mode)
                            or mode_is_program_track(el_mode))
        if (free is not None and free < self.drained_free
                and (in_program_track or not modes_known)):
            self.running_observed = True
        if now_unix >= self.start_time_unix:
            self.running_observed = True

        if not self.running_observed:
            # Pre-run window: a cleared stack can read drained. Do NOT accept it yet.
            return False

        return (
            free is not None
            and free == self.drained_free
            and vaz is not None
            and abs(vaz) < self.speed_tol
            and vel is not None
            and abs(vel) < self.speed_tol
        )


def _dwell_check_times(now_unix: float, start: Time) -> Time:
    """Sample times spanning the pre-scan dwell ``[now, start]``.

    An array-valued :class:`~astropy.time.Time` in equal steps of at most
    :data:`SUN_DWELL_CHECK_STEP_SEC`; just ``[start]`` if there is no dwell.
    """
    start_unix = float(start.unix)
    if start_unix <= now_unix:
        return Time(np.array([start_unix]), format="unix")
    span = start_unix - now_unix
    n_intervals = max(1, int(np.ceil(span / SUN_DWELL_CHECK_STEP_SEC)))
    unix_values = now_unix + span * np.arange(n_intervals + 1) / n_intervals
    return Time(np.array(unix_values), format="unix")


def _choose_slew_and_align(
    traj,
    *,
    current_az: float,
    current_el: float,
    start: Time,
    site: Site,
    sun_safe,
    now_unix: float,
):
    """Pick a sun-safe encoder slew target and wrap-align the trajectory to it.

    Shared step 3 + 4 for every typed scan task's payload core:

    1. Choose an encoder ``(az, el)`` for the trajectory's first sample via
       :func:`fyst_trajectories.dispatch.choose_encoder_solution`, sun-safe over
       the whole pre-scan dwell ``[now_unix, start]`` while the dish holds the
       start position (sampled by :func:`_dwell_check_times`; the slew path is
       not checked).
    2. Shift the WHOLE trajectory azimuth by the 360-deg multiple that lands its
       first sample on the chosen encoder azimuth, so the posted ``/path`` and
       the slew target share one azimuth wrap.

    ``current_az``/``current_el`` are the current encoder position (deg, 200 Hz
    broadcast); ``start`` is the resolved scan start; ``sun_safe`` is forwarded
    to ``choose_encoder_solution``. Returns ``(encoder_az, encoder_el, aligned_traj)``
    with ``aligned_traj.az[0] == encoder_az``. Raises ``PointingError`` from
    ``choose_encoder_solution`` (goal el out of range, or no wrap both keeps the
    whole azimuth span in range and is sun-safe).
    """
    dwell = _dwell_check_times(now_unix, start)

    solution = choose_encoder_solution(
        current_az=current_az,
        current_el=current_el,
        goal_az=float(traj.az[0]),
        goal_el=float(traj.el[0]),
        obstime=dwell,
        site=site,
        sun_safe=sun_safe,
        # The whole trajectory is shifted below, so the wrap must keep its full
        # az span in range.
        goal_az_span=(float(traj.az.min()), float(traj.az.max())),
    )
    enc_az, enc_el = solution
    return enc_az, enc_el, rewrap_trajectory_azimuth(traj, solution.az_shift)


def _escalate_velocity_warning(caught, scan_label: str) -> None:
    """Escalate a library :class:`VelocityLimitWarning` to a hard error.

    Used by :func:`build_constant_el_payload` only. ``validate_trajectory`` only
    *warns* on a velocity breach, but velocity is the quantity the Go TCS
    ``checkAzEl`` enforces, so a breach must abort BEFORE the POST. Safe for a CE
    scan: its elevation is held fixed (el_vel == 0) and the site az-velocity limit
    (3.0) equals the Go TCS hardware az ceiling, so the library warning fires at
    exactly the Go TCS contract. Pong and daisy, whose site el-velocity limit is
    tighter than hardware, and source use :func:`_escalate_hardware_dynamics`
    instead. ``caught`` is the warnings list captured around
    ``validate_trajectory``; raises :class:`TrajectoryValidationError` on any
    :class:`VelocityLimitWarning`.
    """
    velocity_breaches = [
        str(w.message) for w in caught if issubclass(w.category, VelocityLimitWarning)
    ]
    if velocity_breaches:
        raise TrajectoryValidationError(
            f"{scan_label} trajectory exceeds a velocity limit the Go TCS "
            f"enforces; refusing to POST. " + " ".join(velocity_breaches)
        )


def _unit_vectors(az_deg, el_deg) -> np.ndarray:
    """Return the (3, n) unit vectors of horizon coordinates in degrees."""
    az = np.radians(np.asarray(az_deg, dtype=float))
    el = np.radians(np.asarray(el_deg, dtype=float))
    return np.vstack((np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)))


def sun_exclusion_breach(traj, site: Site) -> dict | None:
    """Return the first sample of ``traj`` inside the Sun exclusion zone, or ``None``.

    Checks every sample, since the library's ``validate_sun_avoidance`` checks
    one about every 60 s and a constant-elevation leg can cross the zone between
    them; a sample is inside at a separation at or below
    ``site.sun_avoidance.exclusion_radius``. Returns ``None`` when the site's Sun
    avoidance is disabled or no sample is inside, else a dict of the first sample
    inside (``time`` ISO 8601 UTC with a Z, to the second; ``az``, ``el``,
    ``separation`` in degrees), the ``radius`` and the ``count`` of samples inside.
    """
    cfg = site.sun_avoidance
    if cfg is None or not cfg.enabled:
        return None
    rel = np.asarray(traj.times, dtype=float)
    span = float(rel[-1]) if rel.size else 0.0
    n_grid = int(np.ceil(span / SUN_EXCLUSION_GRID_SEC)) + 1
    grid = np.linspace(0.0, max(span, SUN_EXCLUSION_GRID_SEC), max(n_grid, 2))
    sun_az, sun_el = Coordinates(site).get_sun_altaz(traj.start_time + grid * u.s)
    sun_xyz = _unit_vectors(sun_az, sun_el)
    sun_at = np.vstack([np.interp(rel, grid, sun_xyz[i]) for i in range(3)])
    sun_at /= np.linalg.norm(sun_at, axis=0)
    cos_sep = np.clip(np.sum(_unit_vectors(traj.az, traj.el) * sun_at, axis=0), -1.0, 1.0)
    separation = np.degrees(np.arccos(cos_sep))
    inside = np.flatnonzero(separation <= cfg.exclusion_radius)
    if inside.size == 0:
        return None
    first = int(inside[0])
    return {
        "time": (traj.start_time + rel[first] * u.s).strftime(_SUN_REFUSAL_TIME_FORMAT),
        "az": float(traj.az[first]),
        "el": float(traj.el[first]),
        "separation": float(separation[first]),
        "radius": float(cfg.exclusion_radius),
        "count": int(inside.size),
    }


def _escalate_sun_exclusion(traj, site: Site, scan_label: str) -> None:
    """Refuse a built trajectory with a sample inside the site's Sun exclusion zone.

    The library's planners and ``validate_trajectory`` only warn on the Sun and
    nothing downstream refuses. :func:`_choose_slew_and_align` covers the dwell
    at the scan start; this covers the scan itself. Raises
    :class:`TrajectoryValidationError` naming the first sample inside.
    """
    breach = sun_exclusion_breach(traj, site)
    if breach is not None:
        raise TrajectoryValidationError(
            f"{scan_label} trajectory enters the Sun exclusion zone; refusing to POST. "
            f"First sample inside: boresight az={breach['az']:.2f} deg, el={breach['el']:.2f} deg at "
            f"{breach['time']}, {breach['separation']:.1f} deg from the Sun (exclusion radius "
            f"{breach['radius']:.1f} deg); {breach['count']} of {traj.times.size} samples inside."
        )


#: The opening of an ``az_scan`` refusal for an incomplete or malformed ``scan_params``.
_AZ_SCAN_NEEDS = (
    "az_scan: scan_params needs start_time, azimuth_range (two azimuths), "
    "elevation, speed, num_scans and turnaround_time"
)


def _az_scan_refusal(problem: str) -> ValueError:
    return ValueError(f"{_AZ_SCAN_NEEDS}; {problem}; not launched.")


def _az_scan_number(scan_params: dict, key: str) -> float:
    """Return ``scan_params[key]`` as a finite float, or az_scan's refusal naming ``key``."""
    try:
        value = float(scan_params[key])
    except KeyError:
        raise _az_scan_refusal(f"{key} is missing") from None
    except (TypeError, ValueError) as e:
        raise _az_scan_refusal(f"{key}: {e}") from None
    if not math.isfinite(value):
        raise _az_scan_refusal(f"{key}: not finite")
    return value


def parse_az_scan_params(scan_params: dict) -> tuple:
    """Check the agent's ``az_scan`` ``scan_params`` and read its pattern.

    Returns ``(scan_params, azimuth_range, elevation, speed, num_scans,
    turnaround_time)``: a copy of ``scan_params`` with ``start_time`` as Unix
    seconds (the OCS API and the Go TCS read it only as a number; an ISO-8601
    string with no offset is UTC), the two azimuths as floats and the other four
    pattern keys as floats. The other keys of the copy are left as given, so the
    Go TCS receives them unchanged. The start is not floored here.

    Raises
    ------
    ValueError
        The task's refusal text, for the first of: a key outside
        :data:`AZ_SCAN_PARAM_KEYS`; no ``start_time``; a ``start_time`` that is
        neither finite Unix seconds nor an ISO-8601 string; a missing pattern
        key, one ``float`` cannot read, or one not finite (``azimuth_range`` first, then
        ``elevation``, ``speed``, ``num_scans``, ``turnaround_time``); a
        ``speed`` not positive, a ``num_scans`` not a whole number of at least
        0, a negative ``turnaround_time``.
    """
    scan_params = dict(scan_params)
    unexpected = sorted(str(key) for key in scan_params if key not in AZ_SCAN_PARAM_KEYS)
    if unexpected:
        raise _az_scan_refusal(f"unexpected key(s) {unexpected}")
    if "start_time" not in scan_params:
        raise _az_scan_refusal("start_time is missing")
    start_time = scan_params["start_time"]
    try:
        if isinstance(start_time, str):
            # fromisoformat accepts a trailing Z only from Python 3.11.
            when = datetime.datetime.fromisoformat(
                start_time[:-1] + "+00:00" if start_time.endswith("Z") else start_time
            )
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            start_time = when.timestamp()
        else:
            start_time = float(start_time)
        if not math.isfinite(start_time):
            raise ValueError("not a finite number")
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"az_scan: start_time {start_time!r} is neither Unix "
            f"seconds nor an ISO-8601 UTC string ({e}); not launched."
        ) from None
    scan_params["start_time"] = start_time

    try:
        azimuth_range = [float(az) for az in scan_params["azimuth_range"]]
    except KeyError:
        raise _az_scan_refusal("azimuth_range is missing") from None
    except (TypeError, ValueError) as e:
        raise _az_scan_refusal(f"azimuth_range: {e}") from None
    if len(azimuth_range) != 2:
        raise _az_scan_refusal(f"azimuth_range: {len(azimuth_range)} values, not two")
    if not all(math.isfinite(az) for az in azimuth_range):
        raise _az_scan_refusal("azimuth_range: not finite")
    elevation, speed, num_scans, turnaround_time = (
        _az_scan_number(scan_params, key)
        for key in ("elevation", "speed", "num_scans", "turnaround_time")
    )
    if speed <= 0:
        raise _az_scan_refusal("speed: not positive")
    # The Go TCS decodes NumScans as an int.
    if num_scans < 0 or not num_scans.is_integer():
        raise _az_scan_refusal("num_scans: not a whole number of at least 0")
    if turnaround_time < 0:
        raise _az_scan_refusal("turnaround_time: negative")
    return scan_params, azimuth_range, elevation, speed, num_scans, turnaround_time


def az_scan_duration_sec(
    azimuth_range, speed: float, num_scans: float, turnaround_time: float
) -> float:
    """Return the span (s) of a Go TCS ``/azimuth-scan`` pattern, plus one turnaround.

    ``azimuth_range`` holds two azimuths (deg), ``speed`` is in deg/s and
    ``turnaround_time`` in s. The pattern ends at the range start one turnaround
    before the value returned, an overstatement the Sun check can afford.
    """
    lo, hi = azimuth_range
    # NewAzimuthScanPattern at TCS b7bbbf9: two legs and two turnarounds per
    # repetition, the last turnaround never run.
    return num_scans * 2 * (abs(hi - lo) / speed + turnaround_time)


def az_scan_sun_breach(
    *,
    azimuth_range,
    elevation: float,
    start_unix: float,
    duration_sec: float,
    now_unix: float,
    site: Site,
) -> dict | None:
    """Return an azimuth scan's closest approach to the Sun if in the exclusion zone, else ``None``.

    The Go TCS moves the dish to the range start and holds it there until the scan
    starts (``moveThenStartPattern``, TCS ``b5666ae`` and later), so the whole arc,
    ``min(azimuth_range)`` to ``max(azimuth_range)`` at ``elevation``, is checked
    from ``now_unix`` to ``start_unix + duration_sec``, with times at most
    :data:`AZ_SCAN_SUN_GRID_SEC` and azimuths at most :data:`AZ_SCAN_SUN_ARC_STEP_DEG`
    apart, both ends included. Returns ``None`` when the site's Sun avoidance is
    disabled or the closest approach lies outside
    ``site.sun_avoidance.exclusion_radius``, else a dict of the closest approach:
    ``time`` (ISO 8601 UTC with a Z, to the second), ``az`` and ``separation``
    (deg), and the ``radius``.
    """
    cfg = site.sun_avoidance
    if cfg is None or not cfg.enabled:
        return None
    first = min(now_unix, start_unix)
    last = start_unix + duration_sec
    n_times = max(1, int(np.ceil((last - first) / AZ_SCAN_SUN_GRID_SEC)))
    times = Time(np.linspace(first, last, n_times + 1), format="unix")
    lo, hi = float(min(azimuth_range)), float(max(azimuth_range))
    n_arc = max(1, int(np.ceil((hi - lo) / AZ_SCAN_SUN_ARC_STEP_DEG)))
    arc = np.linspace(lo, hi, n_arc + 1)
    coords = Coordinates(site)
    sun_az, sun_el = coords.get_sun_altaz(times)
    separation = np.asarray(coords.angular_separation(
        arc[None, :],
        float(elevation),
        np.asarray(sun_az, dtype=float)[:, None],
        np.asarray(sun_el, dtype=float)[:, None],
    ))
    i_time, i_arc = np.unravel_index(int(np.argmin(separation)), separation.shape)
    closest = float(separation[i_time, i_arc])
    if closest > cfg.exclusion_radius:
        return None
    return {
        "time": times[i_time].strftime(_SUN_REFUSAL_TIME_FORMAT),
        "az": float(arc[i_arc]),
        "separation": closest,
        "radius": float(cfg.exclusion_radius),
    }


def _escalate_hardware_dynamics(traj, scan_label: str) -> None:
    """Escalate a Go-TCS *hardware* velocity / acceleration breach.

    Used by :func:`build_pong_payload` and :func:`build_daisy_payload`, where
    elevation MOVES, and by :func:`build_source_payload`, whose az velocity varies
    across the drifted legs. Computes realised az/el velocity + acceleration via
    ``np.gradient`` over the WHOLE trajectory, takes each peak velocity as the
    larger of that and the ``az_vel`` / ``el_vel`` columns the Go TCS tests, and
    raises :class:`TrajectoryValidationError` if any exceeds the Go TCS *hardware*
    ceiling. Three reasons it does NOT just escalate the library
    ``validate_trajectory_dynamics`` warnings:

    1. Velocity: the library warns at the *site* limits, whose el value (1.0
       deg/s) is TIGHTER than the hardware ceiling (1.5); escalating it would
       reject an el rate in (1.0, 1.5] the Go TCS would accept.
    2. Acceleration: ``checkAzEl`` does not check acceleration at all, and
       nominal pong turnarounds exceed the site accel limits (see
       :data:`TCS_AZ_MAX_ACCELERATION`) while sitting inside hardware.
    3. Whole-trajectory: the Go TCS checks the first 100 points before accepting
       a path (``pathCmd.Check`` at TCS ``b7bbbf9``) and the rest only as it
       uploads them (``UploadScanPattern``), where a breach ends the track
       mid-scan, so the agent enforces the ceiling everywhere before the POST.

    ``np.gradient`` resolves the smooth pong and daisy turnarounds at the 0.1 s
    timestep but under-resolves the short quintic turnaround of constant-el and
    source scans, so those builders guard its peak (``1.5 * az_accel``)
    analytically; for source this helper still covers velocity.
    """
    times = np.asarray(traj.times, dtype=float)
    if times.size < 2:
        return
    az = np.unwrap(np.asarray(traj.az, dtype=float), period=360.0)
    el = np.asarray(traj.el, dtype=float)
    az_vel = np.gradient(az, times)
    el_vel = np.gradient(el, times)
    # checkAzEl at TCS b7bbbf9 tests the velocity columns; one above the ceiling
    # is refused after the slew.
    max_vaz = float(max(np.abs(az_vel).max(), np.abs(traj.az_vel).max()))
    max_vel = float(max(np.abs(el_vel).max(), np.abs(traj.el_vel).max()))

    breaches: list[str] = []
    if max_vaz > TCS_AZ_MAX_VELOCITY:
        breaches.append(
            f"peak az velocity {max_vaz:.3f} deg/s exceeds the Go TCS hardware "
            f"ceiling {TCS_AZ_MAX_VELOCITY} deg/s."
        )
    if max_vel > TCS_EL_MAX_VELOCITY:
        breaches.append(
            f"peak el velocity {max_vel:.3f} deg/s exceeds the Go TCS hardware "
            f"ceiling {TCS_EL_MAX_VELOCITY} deg/s."
        )

    if times.size >= 4:
        az_accel = np.gradient(az_vel, times)
        el_accel = np.gradient(el_vel, times)
        max_aaz = float(np.abs(az_accel).max())
        max_ael = float(np.abs(el_accel).max())
        if max_aaz > TCS_AZ_MAX_ACCELERATION:
            breaches.append(
                f"peak az acceleration {max_aaz:.3f} deg/s^2 exceeds the Go TCS "
                f"hardware ceiling {TCS_AZ_MAX_ACCELERATION} deg/s^2."
            )
        if max_ael > TCS_EL_MAX_ACCELERATION:
            breaches.append(
                f"peak el acceleration {max_ael:.3f} deg/s^2 exceeds the Go TCS "
                f"hardware ceiling {TCS_EL_MAX_ACCELERATION} deg/s^2."
            )

    if breaches:
        raise TrajectoryValidationError(
            f"{scan_label} trajectory breaches a Go TCS hardware dynamics ceiling "
            f"anywhere along the path (the Go TCS checks the first 100 points before "
            f"accepting and the rest only as it uploads them); refusing to POST. "
            + " ".join(breaches)
        )


#: Peak over mean acceleration of the quintic constant-el turnaround
#: (fyst_trajectories.patterns.turnarounds.quintic_turnaround).
QUINTIC_PEAK_ACCEL_FACTOR: float = 1.5


def _escalate_quintic_accel(az_accel: float, scan_label: str) -> None:
    """Refuse a quintic turnaround whose peak az acceleration exceeds the Go TCS ceiling.

    ``np.gradient`` under-resolves the turnaround's short peak at the 0.1 s timestep,
    and ``checkAzEl`` does not check acceleration.
    """
    az_accel = abs(az_accel)
    peak_az_accel = QUINTIC_PEAK_ACCEL_FACTOR * az_accel
    if peak_az_accel > TCS_AZ_MAX_ACCELERATION:
        raise TrajectoryValidationError(
            f"{scan_label} scan az_accel={az_accel:.3f} deg/s^2 produces a quintic "
            f"turnaround peak acceleration of {peak_az_accel:.3f} deg/s^2, which "
            f"exceeds the Go TCS hardware ceiling of {TCS_AZ_MAX_ACCELERATION} "
            f"deg/s^2; refusing to POST."
        )


def tracking_offset(module) -> InstrumentOffset | None:
    """Return the focal-plane position a field scan's ``module`` key names, or ``None``.

    ``module`` is ``None`` (no key, or a JSON null), one Prime-Cam bay name, or
    several as a list or one comma-separated string, in any case, resolved by
    :func:`fyst_trajectories.resolve_module_tag`. One bay gives that bay's own
    offset; several give the mean of their bay centres, as a several-module
    footprint is centred. A mean on the optical axis (``IM0``, or a symmetric
    set such as an opposite pair or ``"all"``) gives ``None``, so it builds
    exactly as no key. The library's
    ``KeyError`` (a name that is no bay), ``ValueError`` (an empty value) and
    ``TypeError`` (a value that is neither a str nor a sequence, or a sequence
    holding an element that is not a str) propagate unchanged.
    """
    if module is None:
        return None
    offsets = resolve_module_tag(module)
    if len(offsets) == 1:
        (offset,) = offsets
    else:
        offset = InstrumentOffset(
            dx=math.fsum(o.dx for o in offsets) / len(offsets),
            dy=math.fsum(o.dy for o in offsets) / len(offsets),
        )
    if offset.dx == 0.0 and offset.dy == 0.0:
        return None
    return offset


#: What a field scan given ``footprint`` is told to use instead.
_FIELD_SCAN_POINTING_USE = (
    "a field scan is aimed with module (a Prime-Cam bay name, or several for their mean "
    "position); footprint is the source_scan key"
)

#: What a source scan given ``module`` is told to use instead.
_SOURCE_SCAN_POINTING_USE = (
    "a planet pass is aimed with footprint, which must name the centre for now; module is "
    "the key of the three field scans"
)


def _refuse_misplaced_pointing_key(value, *, key: str, scan: str, use: str) -> None:
    """Refuse the pointing key of another scan type before any motion.

    Named only as ignored, the key would let the scan run aimed where the caller
    did not ask.
    """
    if value is not None:
        raise ValueError(f"{scan} does not take {key}, got {value!r}: {use}.")


def build_constant_el_payload(
    *,
    scan_params: dict,
    current_az: float,
    current_el: float,
    site: Site,
    sun_safe=None,
    scheduled_t0_unix: float | None = None,
    now_unix: float,
    max_dispatch_delay_sec: float = MAX_DISPATCH_DELAY_SEC,
) -> dict:
    """Build a Go TCS ``/path`` body and sun-safe slew target for a CE scan.

    Dispatch-time core of the ``constant_el_scan`` typed task. Given a scan
    specification and the telescope's current encoder position, this:

    1. Applies the :data:`SCAN_DISPATCH_BUFFER_SEC` floor to the start time
       (``actual_t0 = max(scheduled_t0_unix or 0, now_unix + buffer)``).
    2. Builds the trajectory via
       :func:`fyst_trajectories.planning.plan_constant_el_scan`, in its field
       form or in its window form (selected by ``az_min`` and ``az_max``). The scan's
       ``velocity`` is passed straight through as mount-frame
       azimuth-coordinate deg/s (*not* scaled by ``cos(el)``).
    3. Picks a sun-safe encoder ``(az, el)`` to slew to via
       :func:`fyst_trajectories.dispatch.choose_encoder_solution`, current-
       position-aware and safe over the whole pre-scan dwell.
    4. Shifts the whole trajectory azimuth by the 360-deg multiple that aligns
       its first sample to the chosen encoder azimuth (wrap alignment), then
       re-validates.
    5. Escalates a velocity breach, a turnaround acceleration over the hardware
       ceiling or a sample inside the Sun exclusion zone to
       :class:`TrajectoryValidationError`; position-bounds breaches already raise.
    6. Returns the encoder target plus the exact three-key ``/path`` body.

    Parameters
    ----------
    scan_params : dict
        Constant-elevation scan spec in one of two forms, selected by the
        azimuth window: ``az_min`` and ``az_max`` both given select the window
        form, neither the field form, and one alone is refused. Both forms
        require ``elevation`` (deg) and ``velocity`` (deg/s, mount-frame
        azimuth-coordinate, sent to the ACU as-is, NOT cos(el)-scaled) and take
        ``az_accel`` (default 1.0) and ``timestep`` (default 0.1).

        Field form, modeled on
        :func:`~fyst_trajectories.planning.plan_constant_el_scan`: required
        ``ra_center``, ``dec_center``, ``width``, ``height`` (deg, the
        :class:`~fyst_trajectories.planning.FieldRegion`); optional (defaults
        match the planner) ``rising``, ``angle``, ``az_padding`` (default 0.0),
        ``max_search_hours``, ``step_seconds``, ``lsa_window``. ``rising`` is
        not forwarded with ``lsa_window``, which fixes the timing itself, and
        ``duration`` has no effect in this form; the agent names either, when
        given, as an ignored key (:func:`unknown_scan_param_keys`).

        Window form, modeled on the window form of
        :func:`~fyst_trajectories.planning.plan_constant_el_scan`: the dish
        sweeps ``az_min`` to ``az_max`` (deg, ``az_max > az_min``; a window
        across azimuth 0/360 is written unwrapped) for ``duration`` (required,
        in SECONDS, rounded by the planner to whole legs, and refused when
        shorter than one leg, ``(az_max - az_min) / velocity``) from the
        floored start, taken literally. The pair is the planner's
        ``az_window``, so a non-numeric ``az_min`` or ``az_max`` is refused as
        its ``az_window[0]`` or ``az_window[1]``. Optional: ``az_padding`` (default 0.0) and the
        four field keys, all together or none, which the planner reads only
        for its drift advisories. ``lsa_window`` is refused beside the window
        keys; ``rising``, ``angle``, ``max_search_hours`` and ``step_seconds``
        are not forwarded, since the window form has no crossing to search, and
        the agent names them, when given, as ignored keys.

        Both forms take ``module`` (optional): a Prime-Cam bay name such as
        ``"IM5"``, or several as a list or one comma-separated string, whose
        mean position is the tracking centre. The scan's own geometry keys stay
        as given and describe where that centre looks; in the window form the
        window and elevation are the unit's own row, never one shifted by hand
        as well. The returned trajectory and slew target are the boresight's,
        and both the scan as given and the commanded pose must lie inside the
        telescope limits. ``IM0`` or no key is the boresight; ``footprint`` is
        refused.
    current_az, current_el : float
        Current encoder position (deg, 200 Hz broadcast); telescope-range
        encoder values, not astropy ``[0, 360)``.
    site : Site
        FYST site configuration (limits, sun-avoidance config).
    sun_safe : callable, optional
        Predicate ``(az_deg, el_deg, time) -> bool`` for
        :func:`~fyst_trajectories.dispatch.choose_encoder_solution`. Defaults to
        the site's scalar exclusion check.
    scheduled_t0_unix : float or None, optional
        Scheduled start (Unix s), or ``None`` to dispatch as soon as the buffer
        allows. In the field form it is the planner's *search anchor*, not the
        literal start: ``plan_constant_el_scan`` searches forward for the
        elevation crossing and starts there, so the posted ``start_time`` is the
        resolved crossing (>= the floored anchor >= ``now + buffer``). In the
        window form the posted ``start_time`` is the floored anchor itself.
    now_unix : float
        Current wall-clock Unix time (caller-supplied; deterministic/testable).
    max_dispatch_delay_sec : float, optional
        Max delay between ``now_unix`` and the resolved start before
        :class:`DispatchDelayError`. Defaults to :data:`MAX_DISPATCH_DELAY_SEC`;
        ``float("inf")`` disables it.

    Returns
    -------
    dict
        ``{"encoder_az": float, "encoder_el": float, "payload": dict}``: the
        exact three-key Go TCS ``/path`` body ``{"start_time", "coordsys",
        "points"}`` (``coordsys == "Horizon"``), with
        ``payload["points"][0][1] == encoder_az`` (wrap alignment).

    Raises
    ------
    KeyError
        If a required ``scan_params`` key is missing (``duration`` excepted), or
        ``module`` names no bay.
    ValueError
        If a ``scan_params`` value is malformed (from the planner), ``az_min`` or
        ``az_max`` is given alone, the window form lacks ``duration``, has one
        shorter than a leg, carries ``lsa_window`` or carries only some field keys,
        ``module`` is empty, or ``footprint`` is given.
    TypeError
        If ``module`` is neither a str nor a sequence.
    PointingError
        If ``plan_constant_el_scan`` finds no elevation crossing within
        ``max_search_hours`` (or the field's edges cross on different passes),
        the goal el is out of range, or no az wrap both keeps the whole azimuth
        span in range and is sun-safe over the pre-scan dwell; as
        ``OffsetInversionError``, if the boresight for ``module`` cannot be
        solved, as near the zenith.
    AzimuthBoundsError, ElevationBoundsError
        If the (wrap-shifted) trajectory leaves the telescope limits anywhere.
    TrajectoryValidationError
        If az/el velocity exceeds the site limit anywhere, the quintic
        turnaround's peak az accel (``1.5 * az_accel``) exceeds the Go TCS
        hardware ceiling, or a sample lies inside the Sun exclusion zone.
    DispatchDelayError
        If the resolved ``start_time`` is more than ``max_dispatch_delay_sec``
        after ``now_unix``.
    """
    _refuse_misplaced_pointing_key(
        scan_params.get("footprint"), key="footprint", scan="constant_el_scan",
        use=_FIELD_SCAN_POINTING_USE,
    )
    detector_offset = tracking_offset(scan_params.get("module"))
    actual_t0 = floor_dispatch_start(scheduled_t0_unix, now_unix)

    # Build the trajectory. Velocity passes straight through as mount-frame
    # azimuth-coordinate deg/s (no cos(el) scaling).
    az_min = scan_params.get("az_min")
    az_max = scan_params.get("az_max")
    if (az_min is None) != (az_max is None):
        raise ValueError(
            "constant_el_scan window form: az_min and az_max must be given together; "
            f"got only {'az_min' if az_max is None else 'az_max'}."
        )
    if az_min is not None:
        scan_label = "Constant-el window"
        velocity = scan_params["velocity"]
        duration = scan_params.get("duration")
        if duration is None:
            raise ValueError(
                "constant_el_scan window form: needs duration (seconds) beside az_min and "
                "az_max."
            )
        # lsa_window is a timing constraint this form would silently drop; the other
        # field-only keys only steer the crossing search it does not run.
        if scan_params.get("lsa_window") is not None:
            raise ValueError(
                "constant_el_scan window form: lsa_window cannot be given beside az_min and "
                "az_max; the window form starts at the dispatch start, so both would set the "
                "timing."
            )
        field_values = {
            "ra_center": scan_params.get("ra_center"),
            "dec_center": scan_params.get("dec_center"),
            "width": scan_params.get("width"),
            "height": scan_params.get("height"),
        }
        given = [key for key, value in field_values.items() if value is not None]
        if given and len(given) != len(field_values):
            raise ValueError(
                "constant_el_scan window form: ra_center, dec_center, width and height go "
                f"all together or not at all; got only {', '.join(given)}."
            )
        # The planner rounds a duration under one leg up to one leg, so a duration
        # given in hours would run a single leg without a word.
        try:
            leg_sec = (float(az_max) - float(az_min)) / float(velocity)
            too_short = 0.0 < float(duration) < leg_sec * (1.0 - ONE_LEG_RELATIVE_TOLERANCE)
        except (TypeError, ValueError, ZeroDivisionError):
            too_short = False  # the planner refuses a malformed value by name
        if too_short:
            raise ValueError(
                f"constant_el_scan window form: duration {float(duration):g} s is shorter "
                f"than one leg of the window ({leg_sec:.1f} s); duration is in seconds."
            )
        block = plan_constant_el_scan(
            FieldRegion(**field_values) if given else None,
            az_window=(az_min, az_max),
            elevation=scan_params["elevation"],
            velocity=velocity,
            duration=duration,
            site=site,
            start_time=actual_t0,
            az_accel=scan_params.get("az_accel", 1.0),
            timestep=scan_params.get("timestep", 0.1),
            detector_offset=detector_offset,
            az_padding=scan_params.get("az_padding", 0.0),
        )
        context = (
            f"constant-el window scan (az {az_min} to {az_max}, "
            f"el={scan_params['elevation']})"
        )
    else:
        scan_label = "Constant-el"
        field = FieldRegion(
            ra_center=scan_params["ra_center"],
            dec_center=scan_params["dec_center"],
            width=scan_params["width"],
            height=scan_params["height"],
        )
        lsa_window = scan_params.get("lsa_window")
        block = plan_constant_el_scan(
            field=field,
            elevation=scan_params["elevation"],
            velocity=scan_params["velocity"],
            site=site,
            start_time=actual_t0,
            # plan_constant_el_scan rejects rising together with lsa_window.
            rising=None if lsa_window is not None else scan_params.get("rising"),
            angle=scan_params.get("angle"),
            az_accel=scan_params.get("az_accel", 1.0),
            timestep=scan_params.get("timestep", 0.1),
            detector_offset=detector_offset,
            az_padding=scan_params.get("az_padding", 0.0),
            max_search_hours=scan_params.get("max_search_hours"),
            step_seconds=scan_params.get("step_seconds"),
            lsa_window=tuple(lsa_window) if lsa_window is not None else None,
        )
        context = (
            f"constant-el scan (ra={scan_params['ra_center']}, "
            f"dec={scan_params['dec_center']}, el={scan_params['elevation']})"
        )
    traj = block.trajectory

    # Dispatch-delay guard: reject a start that resolves too far out (a far
    # crossing, or a window form's far scheduled start) before any slew is
    # computed, else the task slews to it and holds azel_lock until start_time.
    enforce_dispatch_delay(
        float(block.trajectory.start_time.unix),
        now_unix,
        max_dispatch_delay_sec,
        context=context,
        anchor_unix=float(actual_t0.unix),
    )

    # Sun-safe encoder slew target + wrap alignment.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        start=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
        now_unix=now_unix,
    )

    # validate_trajectory raises on a position breach anywhere but only warns on
    # dynamics; escalate its velocity warning (see _escalate_velocity_warning).
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_trajectory(traj, site=site)  # may raise Azimuth/ElevationBoundsError
    _escalate_velocity_warning(caught, scan_label)
    _escalate_sun_exclusion(traj, site, scan_label)
    _escalate_quintic_accel(block.config.az_accel, scan_label)

    # Assemble the three-key /path body (coordsys defaults to "Horizon"). After
    # the wrap shift, payload["points"][0][1] == enc_az.
    payload = to_path_payload(traj)
    return {"encoder_az": enc_az, "encoder_el": enc_el, "payload": payload}


#: Radius (deg) of the Prime-Cam ring: the distance of a ring bay from the optical axis,
#: read from the library's bay table at import.
_RING_BAY = resolve_module_tag("IM4")[0]
_RING_RADIUS_DEG = math.hypot(_RING_BAY.dx_deg, _RING_BAY.dy_deg)

#: The reason an off-centre ``footprint`` is refused (see build_source_payload).
_FOOTPRINT_GATE_NOTE = (
    "a planet pass aimed at one module misses it altogether if the bay layout is off, "
    "and the layout (the instrument team's optical model) has not been checked on the "
    "sky yet; until it has, only the centred footprint is supported, and a "
    f"footprint_margin of about the ring's radius ({_RING_RADIUS_DEG:.2f} deg) widens it "
    "to take in the ring bays."
)

#: The reason a commanded ``boresight_rot`` is refused (see build_source_payload).
_BORESIGHT_ROT_GATE_NOTE = (
    "boresight_rot adds a mechanical rotation of the focal plane on top of the "
    "elevation turn, and none has been confirmed for Prime-Cam, so the scan is "
    "planned with none."
)


def build_source_payload(
    *,
    scan_params: dict,
    current_az: float,
    current_el: float,
    site: Site,
    sun_safe=None,
    scheduled_t0_unix: float | None = None,
    now_unix: float,
    max_dispatch_delay_sec: float = MAX_DISPATCH_DELAY_SEC,
) -> dict:
    """Build a Go TCS ``/path`` body + sun-safe slew target for a source-track CES.

    Dispatch-time core of the ``source_scan`` typed task. Drags a moving source
    (planet or sidereal point) across the *centred* PrimeCam focal plane at a
    fixed boresight elevation, via
    :func:`fyst_trajectories.planning.plan_source_ces`. Mirrors
    :func:`build_constant_el_payload` in structure: dispatch-buffer floor, plan,
    too-far-out guard, sun-safe slew target + wrap alignment, validate +
    hardware-dynamics escalation, assemble the three-key body.

    **Centred only (the source_scan gate).** Builds the on-axis, full-array case:
    ``footprint="c"`` (the PrimeCam centre module, ``(dx, dy) = (0, 0)``) with
    ``boresight_rot`` left ``None`` (uncommanded rotator), and raises
    :class:`ValueError` for anything else, each refusal with its own reason. An
    off-centre ``footprint``: a pass aimed at one module misses it altogether if
    the bay layout (the instrument team's optical model, which the field scans'
    ``module`` key relies on too) is off, and the layout has not been checked on
    the sky yet; ``footprint_margin`` widens the centre, and at about the ring's
    radius (:data:`_RING_RADIUS_DEG`) takes in the ring bays. A
    non-``None`` ``boresight_rot``: it adds a mechanical rotation of the focal
    plane on top of the elevation turn, and none has been confirmed for
    Prime-Cam. The footprint gate lifts once the layout has been checked on the
    sky. ``module``, the field scans' pointing key, is refused.

    Velocity frame: ``plan_source_ces`` builds from a
    :class:`~fyst_trajectories.ConstantElScanConfig` whose ``az_speed`` is the
    per-leg speed (``scan_params["az_speed"]`` when given, else derived) in
    MOUNT-frame azimuth-coordinate deg/s; ``cos(el)`` is already implicit in the
    elevation-fixed azimuth track and is NOT re-applied (the solved drift
    ``v_az`` is likewise a mount-frame rate). So like constant-el (unlike
    pong/daisy), the commanded az velocity is mount-frame.

    Parameters
    ----------
    scan_params : dict
        Source-CES spec, modeled on
        :func:`~fyst_trajectories.planning.plan_source_ces`. Provide a source,
        ``body`` (solar-system name) OR both ``ra`` and ``dec`` (deg), and
        ``el_bore`` (deg) unless anchoring on the start (below). Optional
        (defaults match the planner): ``footprint`` (MUST name the centre
        module, ``"c"``, ``"center"`` or ``"IM0"`` in any case, while the gate
        stands; ``None`` is the centre), ``boresight_rot`` (MUST be ``None``
        while the gate stands), ``mode`` (``"rising"``/``"setting"``), ``pm_ra``,
        ``pm_dec``, ``ref_epoch`` (ISO str or ``Time``), ``timestep``,
        ``sampling_step_seconds``, ``az_accel``, ``az_padding``, ``az_branch``,
        ``allow_partial``, ``v_az``, ``az_speed`` (deg/s, mount-frame),
        ``az_throw`` (deg; replaces the padded throw, so leave ``az_padding``
        unset), ``dwell`` (s), ``footprint_margin`` (deg, non-negative or
        ``None``; widens the module on every side for the crossing solve).
        Search window is the floored anchor (``night=actual_t0``) with ``mode``;
        pass a ``window`` 2-sequence of ISO strings / ``Time`` to override.
        Without ``el_bore`` the planner derives the boresight elevation so the
        pass starts at or shortly after the floored start (its ``start_time``
        anchor), and ``mode`` from the source's motion unless given; a
        ``window`` needs ``el_bore``.
    current_az, current_el : float
        Current encoder position (deg, 200 Hz broadcast).
    site : Site
        FYST site configuration.
    sun_safe : callable, optional
        Sun-safety predicate for ``choose_encoder_solution``.
    scheduled_t0_unix : float or None, optional
        Scheduled start (Unix s), or ``None`` to dispatch as soon as the buffer
        allows. Used as the ``plan_source_ces`` search anchor.
    now_unix : float
        Current wall-clock Unix time (caller-supplied; deterministic).
    max_dispatch_delay_sec : float, optional
        Max delay before :class:`DispatchDelayError`. Defaults to
        :data:`MAX_DISPATCH_DELAY_SEC`; ``float("inf")`` disables it.

    Returns
    -------
    dict
        ``{"encoder_az", "encoder_el", "payload"}``: the three-key Go TCS
        ``/path`` body; ``payload["points"][0][1] == encoder_az`` (wrap align).

    Raises
    ------
    ValueError
        On an off-centre ``footprint`` / non-``None`` ``boresight_rot`` (the
        centred-only gate), a ``module`` key, a negative or non-finite
        ``footprint_margin``, a missing source spec, a ``window`` without
        ``el_bore``, or an incompatible source/window combo.
    KeyError, TypeError
        An unknown or malformed ``footprint`` tag (from ``resolve_footprint``).
    PointingError, TargetNotObservableError
        If the source never reaches ``el_bore``, drifts too slowly in elevation
        at the anchor (near transit, without ``el_bore``), the goal el is out of
        range, or no az wrap both keeps the whole azimuth span in range and is
        sun-safe over the pre-scan dwell.
    AzimuthBoundsError, ElevationBoundsError
        If the (wrap-shifted) trajectory leaves the telescope limits anywhere.
    TrajectoryValidationError
        If az/el velocity or acceleration exceeds a Go TCS hardware ceiling
        anywhere, or a sample lies inside the Sun exclusion zone.
    DispatchDelayError
        If the resolved start is more than ``max_dispatch_delay_sec`` out.
    """
    _refuse_misplaced_pointing_key(
        scan_params.get("module"), key="module", scan="source_scan",
        use=_SOURCE_SCAN_POINTING_USE,
    )
    actual_t0 = floor_dispatch_start(scheduled_t0_unix, now_unix)

    # Centred-only gate: the bay layout is not yet checked on the sky and no
    # boresight rotation is confirmed (see docstring).
    footprint = scan_params.get("footprint")
    if footprint is None:  # absent, or a JSON null: the centre
        footprint = "c"
    resolved = resolve_footprint(footprint)
    if resolved.center_xi_deg != 0.0 or resolved.center_eta_deg != 0.0:
        raise ValueError(
            f"source_scan currently supports only the centred PrimeCam footprint "
            f'("c", "center" or "IM0", any case), got {footprint!r}: {_FOOTPRINT_GATE_NOTE}'
        )
    footprint = "c"
    if scan_params.get("boresight_rot") is not None:
        raise ValueError(
            f"source_scan currently supports only an uncommanded boresight "
            f"rotator (boresight_rot=None), got "
            f"{scan_params.get('boresight_rot')!r}: {_BORESIGHT_ROT_GATE_NOTE}"
        )

    # Inflate before the planner call, as the library's simulator does when it
    # rebuilds a margined pass (_generate_source_ces_trajectory in
    # fyst_trajectories.overhead.simulation at v0.14.0); the centre stays on the
    # boresight, so the gate above still holds.
    margin = scan_params.get("footprint_margin")
    if margin is not None:
        footprint_arg = inflate_footprint(resolve_footprint(footprint), margin)
    else:
        footprint_arg = footprint

    # az_speed and v_az are mount-frame azimuth rates; neither is cos(el)-scaled.
    el_bore = scan_params.get("el_bore")
    window = scan_params.get("window")
    ref_epoch = scan_params.get("ref_epoch")
    if isinstance(ref_epoch, str):
        ref_epoch = Time(ref_epoch, scale="utc")
    start_time = None
    if window is not None:
        w0, w1 = window
        window = (
            Time(w0, scale="utc") if isinstance(w0, str) else w0,
            Time(w1, scale="utc") if isinstance(w1, str) else w1,
        )
        night = None
        mode = scan_params.get("mode")
    elif el_bore is None:
        # Anchored form, for a scan handed only a start: the planner derives the
        # boresight elevation so the pass starts at or shortly after the anchor.
        start_time = actual_t0
        night = None
        mode = scan_params.get("mode")
    else:
        night = actual_t0
        mode = scan_params.get("mode", "rising")
    block = plan_source_ces(
        body=scan_params.get("body"),
        ra=scan_params.get("ra"),
        dec=scan_params.get("dec"),
        pm_ra=scan_params.get("pm_ra", 0.0),
        pm_dec=scan_params.get("pm_dec", 0.0),
        ref_epoch=ref_epoch,
        footprint=footprint_arg,
        el_bore=el_bore,
        boresight_rot=None,
        window=window,
        night=night,
        start_time=start_time,
        mode=mode,
        site=site,
        timestep=scan_params.get("timestep", 0.1),
        sampling_step_seconds=scan_params.get("sampling_step_seconds", 30.0),
        az_accel=scan_params.get("az_accel", 1.0),
        az_padding=scan_params.get("az_padding"),
        az_branch=scan_params.get("az_branch"),
        allow_partial=scan_params.get("allow_partial", False),
        v_az=scan_params.get("v_az"),
        az_speed=scan_params.get("az_speed"),
        az_throw=scan_params.get("az_throw"),
        dwell=scan_params.get("dwell"),
    )
    traj = block.trajectory

    # Dispatch-delay guard: plan_source_ces searches forward for the el_bore
    # crossing, so a far crossing can resolve hours out; reject before any slew
    # is computed.
    src_label = scan_params.get("body") or (
        f"ra={scan_params.get('ra')}, dec={scan_params.get('dec')}"
    )
    enforce_dispatch_delay(
        float(block.trajectory.start_time.unix),
        now_unix,
        max_dispatch_delay_sec,
        context=f"source scan ({src_label}, el_bore={block.computed_params['el_bore']:.3f})",
        anchor_unix=float(actual_t0.unix),
    )

    # Sun-safe encoder slew target + wrap alignment.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        start=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
        now_unix=now_unix,
    )

    # Validate position over the whole trajectory (raises on a bounds breach,
    # closing the Go TCS first-100 gap) + escalate a Go TCS hardware velocity or
    # acceleration breach; see _escalate_hardware_dynamics.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_trajectory(traj, site=site)  # may raise Azimuth/ElevationBoundsError
    _escalate_sun_exclusion(traj, site, "Source-CES")
    _escalate_hardware_dynamics(traj, "Source-CES")
    _escalate_quintic_accel(block.config.az_accel, "Source-CES")

    # Assemble the three-key /path body (points[0][1] == enc_az after align).
    payload = to_path_payload(traj)
    return {"encoder_az": enc_az, "encoder_el": enc_el, "payload": payload}


def build_pong_payload(
    *,
    scan_params: dict,
    current_az: float,
    current_el: float,
    site: Site,
    sun_safe=None,
    scheduled_t0_unix: float | None = None,
    now_unix: float,
    max_dispatch_delay_sec: float = MAX_DISPATCH_DELAY_SEC,
) -> dict:
    """Build a Go TCS ``/path`` body + sun-safe slew target for a Pong scan.

    Dispatch-time core of the ``pong_scan`` typed task. Covers a rectangular
    RA/Dec field with a curvy-box Pong pattern via
    :func:`fyst_trajectories.planning.plan_pong_scan`. Mirrors
    :func:`build_constant_el_payload` in structure (dispatch floor, plan,
    too-far-out guard, sun-safe slew + wrap align, validate + hardware-dynamics
    escalation, assemble).

    Velocity frame: ``plan_pong_scan``'s ``velocity`` is ON-SKY (tangent-plane /
    sky-offset) deg/s, a DIFFERENT frame from constant-el/source. The planner
    maps it to az/el via the field's instantaneous geometry, so the realised
    mount-frame az velocity is ``~velocity / cos(el)`` and is NOT caller-scaled;
    pass the astronomer's on-sky scan speed directly. The hardware-ceiling
    escalation below checks the realised mount-frame dynamics.

    Parameters
    ----------
    scan_params : dict
        Pong spec, modeled on :func:`~fyst_trajectories.planning.plan_pong_scan`.
        Required: ``ra_center``, ``dec_center``, ``width``, ``height`` (deg, the
        :class:`~fyst_trajectories.planning.FieldRegion`); ``velocity`` (ON-SKY
        deg/s), ``spacing`` (deg), ``num_terms`` (int). Optional (defaults match
        the planner): ``angle``, ``n_cycles``, ``timestep``, and ``module``: a
        Prime-Cam bay name such as ``"IM5"``, or several as a list or one
        comma-separated string, whose mean position is the tracking centre. The
        field keys stay as given and describe where that centre looks; the
        returned trajectory and slew target are the boresight's, and both the
        scan as given and the commanded pose must lie inside the telescope
        limits. ``IM0`` or no key is the boresight; ``footprint`` is refused.
    current_az, current_el : float
        Current encoder position (deg, 200 Hz broadcast).
    site : Site
        FYST site configuration.
    sun_safe : callable, optional
        Sun-safety predicate for ``choose_encoder_solution``.
    scheduled_t0_unix : float or None, optional
        Scheduled start (Unix s), or ``None`` to dispatch as soon as the buffer
        allows. Pong takes ``start_time`` LITERALLY (no forward search), so the
        posted ``start_time`` equals the floored anchor.
    now_unix : float
        Current wall-clock Unix time (caller-supplied; deterministic).
    max_dispatch_delay_sec : float, optional
        Dispatch-delay bound; defaults to :data:`MAX_DISPATCH_DELAY_SEC`. Pong's
        start equals the floored anchor, so this bounds the scheduled start
        itself; applied uniformly for parity with the elevation-searching tasks.

    Returns
    -------
    dict
        ``{"encoder_az", "encoder_el", "payload"}`` (three-key ``/path`` body);
        ``payload["points"][0][1] == encoder_az``.

    Raises
    ------
    KeyError
        If a required key is missing, or ``module`` names no bay.
    ValueError
        If ``n_cycles < 1`` (from ``plan_pong_scan``), ``module`` is empty, or
        ``footprint`` is given.
    TypeError
        If ``module`` is neither a str nor a sequence.
    TargetNotObservableError, AzimuthBoundsError, ElevationBoundsError
        If the field is unobservable at the start time or the trajectory leaves
        the telescope limits anywhere.
    PointingError
        If no az wrap both keeps the whole azimuth span in range and is sun-safe
        over the pre-scan dwell; as ``OffsetInversionError``, if the boresight
        for ``module`` cannot be solved, as near the zenith.
    TrajectoryValidationError
        If az/el velocity or acceleration exceeds a Go TCS hardware ceiling
        anywhere, or a sample lies inside the Sun exclusion zone.
    DispatchDelayError
        If the resolved start is more than ``max_dispatch_delay_sec`` out.
    """
    _refuse_misplaced_pointing_key(
        scan_params.get("footprint"), key="footprint", scan="pong_scan",
        use=_FIELD_SCAN_POINTING_USE,
    )
    detector_offset = tracking_offset(scan_params.get("module"))
    actual_t0 = floor_dispatch_start(scheduled_t0_unix, now_unix)

    # Build the Pong trajectory. velocity is on-sky deg/s, a different frame from
    # constant-el/source; passed straight to the planner, which maps it to the
    # mount frame via the field geometry.
    field = FieldRegion(
        ra_center=scan_params["ra_center"],
        dec_center=scan_params["dec_center"],
        width=scan_params["width"],
        height=scan_params["height"],
    )
    block = plan_pong_scan(
        field=field,
        velocity=scan_params["velocity"],
        spacing=scan_params["spacing"],
        num_terms=scan_params["num_terms"],
        site=site,
        start_time=actual_t0,
        timestep=scan_params.get("timestep", 0.1),
        angle=scan_params.get("angle", 0.0),
        detector_offset=detector_offset,
        n_cycles=scan_params.get("n_cycles", 1),
    )
    traj = block.trajectory

    # Dispatch-delay guard: a Pong starts at the floored anchor, so this bounds
    # its scheduled start.
    enforce_dispatch_delay(
        float(block.trajectory.start_time.unix),
        now_unix,
        max_dispatch_delay_sec,
        context=(
            f"pong scan (ra={scan_params['ra_center']}, dec={scan_params['dec_center']})"
        ),
        anchor_unix=float(actual_t0.unix),
    )

    # Sun-safe encoder slew target + wrap alignment.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        start=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
        now_unix=now_unix,
    )

    # Validate position over the whole trajectory + escalate a Go TCS hardware
    # velocity or acceleration breach. El moves in a Pong, so the hardware ceiling,
    # not the tighter site-limit library warning, is the Go TCS contract.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_trajectory(traj, site=site)  # may raise Azimuth/ElevationBoundsError
    _escalate_sun_exclusion(traj, site, "Pong")
    _escalate_hardware_dynamics(traj, "Pong")

    payload = to_path_payload(traj)
    return {"encoder_az": enc_az, "encoder_el": enc_el, "payload": payload}


def build_daisy_payload(
    *,
    scan_params: dict,
    current_az: float,
    current_el: float,
    site: Site,
    sun_safe=None,
    scheduled_t0_unix: float | None = None,
    now_unix: float,
    max_dispatch_delay_sec: float = MAX_DISPATCH_DELAY_SEC,
) -> dict:
    """Build a Go TCS ``/path`` body + sun-safe slew target for a Daisy scan.

    Dispatch-time core of the ``daisy_scan`` typed task. Covers a point source
    with a constant-velocity petal (Daisy) pattern via
    :func:`fyst_trajectories.planning.plan_daisy_scan`. Mirrors
    :func:`build_constant_el_payload` in structure (dispatch floor, plan,
    too-far-out guard, sun-safe slew + wrap align, validate + hardware-dynamics
    escalation, assemble).

    Velocity frame: ``plan_daisy_scan``'s ``velocity`` is ON-SKY (tangent-plane /
    sky-offset) deg/s, the SAME frame as Pong, DIFFERENT from constant-el/source.
    Pass the astronomer's on-sky scan speed directly; the planner maps it to the
    mount frame. The hardware-ceiling escalation below checks the realised
    mount-frame dynamics.

    Parameters
    ----------
    scan_params : dict
        Daisy spec, modeled on
        :func:`~fyst_trajectories.planning.plan_daisy_scan`. Required: ``ra``,
        ``dec`` (deg, source centre); ``radius`` (deg), ``velocity`` (ON-SKY
        deg/s), ``turn_radius`` (deg), ``avoidance_radius`` (deg >= 0),
        ``start_acceleration`` (deg/s^2), ``duration`` (s). Optional (defaults
        match the planner): ``y_offset``, ``timestep``, and ``module``: a
        Prime-Cam bay name such as ``"IM5"``, or several as a list or one
        comma-separated string, whose mean position is the tracking centre. The
        source keys stay as given and describe where that centre looks; the
        returned trajectory and slew target are the boresight's, and both the
        scan as given and the commanded pose must lie inside the telescope
        limits. ``IM0`` or no key is the boresight; ``footprint`` is refused.
    current_az, current_el : float
        Current encoder position (deg, 200 Hz broadcast).
    site : Site
        FYST site configuration.
    sun_safe : callable, optional
        Sun-safety predicate for ``choose_encoder_solution``.
    scheduled_t0_unix : float or None, optional
        Scheduled start (Unix s), or ``None`` to dispatch as soon as the buffer
        allows. Daisy takes ``start_time`` LITERALLY (no forward search), so the
        posted ``start_time`` equals the floored anchor.
    now_unix : float
        Current wall-clock Unix time (caller-supplied; deterministic).
    max_dispatch_delay_sec : float, optional
        Dispatch-delay bound; defaults to :data:`MAX_DISPATCH_DELAY_SEC`. Daisy's
        start equals the floored anchor, so this bounds the scheduled start
        itself; applied uniformly for parity with the elevation-searching tasks.

    Returns
    -------
    dict
        ``{"encoder_az", "encoder_el", "payload"}`` (three-key ``/path`` body);
        ``payload["points"][0][1] == encoder_az``.

    Raises
    ------
    KeyError
        If a required key is missing, or ``module`` names no bay.
    ValueError
        If a ``scan_params`` value is malformed (from ``plan_daisy_scan``),
        ``module`` is empty, or ``footprint`` is given.
    TypeError
        If ``module`` is neither a str nor a sequence.
    TargetNotObservableError, AzimuthBoundsError, ElevationBoundsError
        If the source is unobservable at the start time or the trajectory leaves
        the telescope limits anywhere.
    PointingError
        If no az wrap both keeps the whole azimuth span in range and is sun-safe
        over the pre-scan dwell; as ``OffsetInversionError``, if the boresight
        for ``module`` cannot be solved, as near the zenith.
    TrajectoryValidationError
        If az/el velocity or acceleration exceeds a Go TCS hardware ceiling
        anywhere, or a sample lies inside the Sun exclusion zone.
    DispatchDelayError
        If the resolved start is more than ``max_dispatch_delay_sec`` out.
    """
    _refuse_misplaced_pointing_key(
        scan_params.get("footprint"), key="footprint", scan="daisy_scan",
        use=_FIELD_SCAN_POINTING_USE,
    )
    detector_offset = tracking_offset(scan_params.get("module"))
    actual_t0 = floor_dispatch_start(scheduled_t0_unix, now_unix)

    # Build the Daisy trajectory. velocity is on-sky deg/s, same frame as Pong,
    # different from constant-el/source; passed straight to the planner, which
    # maps it to the mount frame.
    block = plan_daisy_scan(
        ra=scan_params["ra"],
        dec=scan_params["dec"],
        radius=scan_params["radius"],
        velocity=scan_params["velocity"],
        turn_radius=scan_params["turn_radius"],
        avoidance_radius=scan_params["avoidance_radius"],
        start_acceleration=scan_params["start_acceleration"],
        site=site,
        start_time=actual_t0,
        timestep=scan_params.get("timestep", 0.1),
        duration=scan_params["duration"],
        y_offset=scan_params.get("y_offset", 0.0),
        detector_offset=detector_offset,
    )
    traj = block.trajectory

    # Dispatch-delay guard: a Daisy starts at the floored anchor, so this bounds
    # its scheduled start.
    enforce_dispatch_delay(
        float(block.trajectory.start_time.unix),
        now_unix,
        max_dispatch_delay_sec,
        context=f"daisy scan (ra={scan_params['ra']}, dec={scan_params['dec']})",
        anchor_unix=float(actual_t0.unix),
    )

    # Sun-safe encoder slew target + wrap alignment.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        start=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
        now_unix=now_unix,
    )

    # Validate position over the whole trajectory + escalate a Go TCS hardware
    # velocity or acceleration breach. El moves in a Daisy, so the hardware ceiling,
    # not the tighter site-limit library warning, is the Go TCS contract.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_trajectory(traj, site=site)  # may raise Azimuth/ElevationBoundsError
    _escalate_sun_exclusion(traj, site, "Daisy")
    _escalate_hardware_dynamics(traj, "Daisy")

    payload = to_path_payload(traj)
    return {"encoder_az": enc_az, "encoder_el": enc_el, "payload": payload}


def refloor_payload_start_time(payload: dict, now_unix: float) -> dict:
    """Re-apply the dispatch-buffer floor to a built payload's ``start_time``.

    Each ``build_*_payload`` floors ``start_time`` at *build* time, but
    the PCS task then slews to the scan start and that slew can block up to
    ``SLEW_TIMEOUT_SEC`` (180 s). A dispatch-now scan whose resolved crossing
    landed only just above the 10 s floor can, by the time the slew finishes, be
    *less* than the Go TCS ``pathCmd.Check()`` minimum lead (rejected at
    ``< ~9.8 s`` out by ``checkProgramTrackStartTime`` at TCS ``b7bbbf9``), so
    the POST would fail. Re-floor with a *fresh* ``now`` just before POST so the
    body still clears the receiver.

    Only ``start_time`` is absolute; ``points`` carry RELATIVE seconds in column 0
    (``to_path_payload``), so advancing the start just shifts the whole scan later
    in wall-clock with no point re-serialisation. The completion-loop end time
    (``start_time + points[-1][0]``) reads this same ``start_time``, so it stays
    consistent. The floor never moves the start earlier (it is a ``max``), so a
    comfortably-future crossing is untouched.

    The re-floor shifts *when* the scan plays, not *where*; the caller bounds the
    resulting sidereal staleness via :data:`MAX_REFLOOR_DRIFT_SEC`. ``payload`` is
    mutated in place and returned; ``now_unix`` is read *after* the slew completes.
    """
    payload["start_time"] = max(
        float(payload["start_time"]), now_unix + SCAN_DISPATCH_BUFFER_SEC
    )
    return payload


def refloor_drift_seconds(original_start_unix: float, payload: dict) -> float:
    """Return how many seconds the re-floor advanced a payload's ``start_time``.

    :func:`refloor_payload_start_time` advances only ``start_time``; the baked
    az/el samples are frozen at the build-time start, so this delta is exactly
    how stale the sidereal-tracked geometry now is (the boresight lags the sky by
    ~az_rate * delta). Pure / ocs-free so the agent's dispatch path stays a thin
    wrapper. Returns ``>= 0`` (the re-floor never regresses ``start_time``).
    """
    return max(0.0, float(payload["start_time"]) - float(original_start_unix))


def _tcs_reply_body(response) -> dict:
    """Return the JSON object an ``aculib`` TCS return carries, else ``{}``."""
    try:
        body = response.json()
    except (AttributeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _is_foreign_reply(response) -> bool:
    """Return whether a 200 carries neither the OCS API's accepted nor relayed-refusal answer."""
    return (getattr(response, "status_code", None) == 200
            and _tcs_reply_body(response).get("status") not in ("ok", "error"))


def tcs_response_status(response) -> int | None:
    """Read the Go TCS's status for one command from an ``aculib`` return.

    Returns ``200`` when the TCS accepted the command, ``None`` when it was busy
    (the refusal worth retrying; ``aculib.post`` returns ``{}`` on a 503), and
    otherwise the HTTP status of the refusal. The OCS API relays the Go TCS's
    answer with HTTP 200 whatever status Go sent (``api/app/telescope.py`` at OCS
    ``c515a51``), so a 200 whose body has ``"status": "error"`` is a refusal:
    ``None`` for :data:`TCS_BUSY_MESSAGE`, else ``400``, Go's status for a body it
    cannot decode or check (TCS ``b7bbbf9``). A 200 with neither ``"ok"`` nor
    ``"error"`` is not the Go TCS's reply and reads as :data:`TCS_FOREIGN_REPLY_STATUS`.
    """
    code = getattr(response, "status_code", None)
    if code == 200:
        if _is_foreign_reply(response):
            return TCS_FOREIGN_REPLY_STATUS
        body = _tcs_reply_body(response)
        if body.get("status") == "ok":
            return 200
        return None if body.get("message") == TCS_BUSY_MESSAGE else 400
    return code


def tcs_response_reason(response) -> str:
    """Describe an ``aculib`` TCS return for a task's failure message.

    A 200 that is not the Go TCS's reply (see :func:`tcs_response_status`) is
    quoted, cut to :data:`TCS_REPLY_QUOTE_CHARS`. Otherwise gives the TCS's
    ``message``, else the OCS API's ``detail`` (a request it refused itself, HTTP
    422), else the HTTP status. Every Go refusal carries a message, so a 5xx
    without one is the OCS API's own failure (the command may or may not have
    reached the TCS) and a 4xx without one came from the OCS API or its proxy
    (nginx's 413 for a body over its limit); ``aculib``'s ``{}`` reads ``"HTTP 503"``.
    """
    if _is_foreign_reply(response):
        text = str(getattr(response, "text", ""))[:TCS_REPLY_QUOTE_CHARS]
        return f"the reply was not the Go TCS's: {text!r}"
    body = _tcs_reply_body(response)
    message = body.get("message") or body.get("detail")
    if message:
        return str(message)
    code = getattr(response, "status_code", None)
    if code is not None and code >= 500:
        return f"OCS API error HTTP {code} (the TCS may or may not have taken the command)"
    if code is not None and 400 <= code < 500:
        return f"HTTP {code} from the OCS API or its proxy, not the Go TCS"
    return f"HTTP {503 if code is None else code}"


#: The ``scan_params`` keys each typed scan's builder reads, by op name; the agent
#: names any other key as ignored. ``constant_el_scan``'s set spans both its forms;
#: a key the chosen form does not use is named too (see
#: :data:`CONSTANT_EL_UNUSED_IN_WINDOW_FORM`, :data:`CONSTANT_EL_UNUSED_IN_FIELD_FORM`).
#: ``source_scan`` also admits ``pass_index`` and ``n_passes``, bookkeeping keys of
#: the library's planet-pass dispatch dict. A scan's set includes the other scans'
#: pointing key (``module`` or ``footprint``), which its builder reads to refuse
#: it. ``tests/test_scan_param_keys.py`` holds each set equal to the keys its
#: builder's source reads.
SCAN_PARAM_KEYS: dict[str, frozenset[str]] = {
    "constant_el_scan": frozenset({
        "ra_center", "dec_center", "width", "height", "elevation", "velocity",
        "lsa_window", "rising", "angle", "az_accel", "timestep", "az_padding",
        "max_search_hours", "step_seconds", "az_min", "az_max", "duration",
        "module", "footprint",
    }),
    "source_scan": frozenset({
        "body", "ra", "dec", "pm_ra", "pm_dec", "ref_epoch", "footprint",
        "boresight_rot", "footprint_margin", "el_bore", "mode", "window",
        "timestep", "sampling_step_seconds", "az_accel", "az_padding", "az_branch",
        "allow_partial", "v_az", "az_speed", "az_throw", "dwell",
        "pass_index", "n_passes", "module",
    }),
    "pong_scan": frozenset({
        "ra_center", "dec_center", "width", "height", "velocity", "spacing",
        "num_terms", "timestep", "angle", "n_cycles", "module", "footprint",
    }),
    "daisy_scan": frozenset({
        "ra", "dec", "radius", "velocity", "turn_radius", "avoidance_radius",
        "start_acceleration", "timestep", "duration", "y_offset", "module", "footprint",
    }),
}

#: The ``constant_el_scan`` keys that steer the field form's crossing search, which
#: the window form (``az_min`` and ``az_max`` given) does not run. ``lsa_window`` is
#: not among them: the window form refuses it.
CONSTANT_EL_UNUSED_IN_WINDOW_FORM: frozenset[str] = frozenset({
    "rising", "angle", "max_search_hours", "step_seconds",
})

#: The ``constant_el_scan`` key the field form has no use for: it sweeps for as long
#: as the field takes to cross. ``rising`` beside ``lsa_window`` is unused there too.
CONSTANT_EL_UNUSED_IN_FIELD_FORM: frozenset[str] = frozenset({"duration"})

#: The one ``scan_params`` key the agent reads itself, before the build.
AGENT_SCAN_PARAM_KEYS: frozenset[str] = frozenset({"max_dispatch_delay_sec"})

#: The ``scan_params`` keys the agent's ``az_scan`` takes, the arguments of
#: ``aculib.observatory_control_system.azimuth_scan``; :func:`parse_az_scan_params`
#: refuses any other.
AZ_SCAN_PARAM_KEYS: frozenset[str] = frozenset({
    "start_time", "azimuth_range", "elevation", "speed", "num_scans", "turnaround_time",
})


def _is_zero_shift(value) -> bool:
    """Return whether an offset is ``None`` or a numeric zero, not a bool or text."""
    if value is None:
        return True
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def unknown_scan_param_keys(job: str, scan_params: dict) -> list[str]:
    """Return, sorted, the ``scan_params`` keys the ``job`` builder does not read.

    ``job`` is the op name (a key of :data:`SCAN_PARAM_KEYS`); any other reads
    nothing, so every key is returned. For ``constant_el_scan`` a key given (not
    ``None``) that the chosen form does not use is returned too: in the window form
    (``az_min`` and ``az_max`` given) those of :data:`CONSTANT_EL_UNUSED_IN_WINDOW_FORM`;
    in the field form those of :data:`CONSTANT_EL_UNUSED_IN_FIELD_FORM`, and
    ``rising`` beside ``lsa_window``. Never returned: :data:`AGENT_SCAN_PARAM_KEYS`,
    and a ``source_scan`` ``eta_offset_deg`` of zero or ``None``, which the
    library's planet-pass dispatch dict always carries and which changes nothing (a
    nonzero one, which the build drops, is returned).
    """
    known = SCAN_PARAM_KEYS.get(job, frozenset()) | AGENT_SCAN_PARAM_KEYS
    unknown = [key for key in scan_params if key not in known]
    if job == "constant_el_scan":
        window_form = (scan_params.get("az_min") is not None
                       and scan_params.get("az_max") is not None)
        if window_form:
            unused = CONSTANT_EL_UNUSED_IN_WINDOW_FORM
        elif scan_params.get("lsa_window") is not None:
            unused = CONSTANT_EL_UNUSED_IN_FIELD_FORM | {"rising"}
        else:
            unused = CONSTANT_EL_UNUSED_IN_FIELD_FORM
        unknown += [key for key in unused if scan_params.get(key) is not None]
    if job == "source_scan":
        unknown = [
            key for key in unknown
            if not (key == "eta_offset_deg" and _is_zero_shift(scan_params[key]))
        ]
    return sorted(str(key) for key in unknown)
