"""OCS-free trajectory helpers for the PCS ACU-interface agent.

Dispatch-time *core* of the FYST typed scan tasks. Imports only
:mod:`fyst_trajectories` and the standard library, deliberately **no** ``ocs``
/ ``twisted``, so it is unit-testable in the minimal environment where the OCS
operations framework (and hence the agent module that wraps these helpers) is
unavailable.

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
for constant-el, :func:`_escalate_hardware_dynamics` for the multi-axis tasks),
with constant-el and source adding an analytic ``1.5 * az_accel`` quintic
turnaround guard.
"""

import dataclasses
import warnings

import numpy as np
from astropy.time import Time

from fyst_trajectories.dispatch import choose_encoder_solution
from fyst_trajectories.exceptions import VelocityLimitWarning
from fyst_trajectories.planning import (
    FieldRegion,
    plan_constant_el_scan,
    plan_daisy_scan,
    plan_pong_scan,
    plan_source_ces,
)
from fyst_trajectories.site import Site
from fyst_trajectories.trajectory_utils import to_path_payload, validate_trajectory

#: Minimum lead time (seconds) applied to a scan's start time at dispatch.
#: The Go TCS ``/path`` receiver hard-rejects a ``start_time`` less than 9.8 s
#: in the future (``commands.go:253``), and the Vertex ProgramTrack ICD wants
#: the track to start ~5-10 s ahead to pre-fill its stack; 10 s clears both. The
#: task computes ``actual_t0 = max(scheduled_t0, now + SCAN_DISPATCH_BUFFER_SEC)``.
SCAN_DISPATCH_BUFFER_SEC: float = 10.0

#: Go TCS *hardware* acceleration ceilings (deg/s^2). These are the HARDWARE
#: bounds, NOT the conservative operational limits in fyst_trajectories.site
#: (1.0/0.5); the dispatch accel guard checks hardware so nominal scans pass.
TCS_AZ_MAX_ACCELERATION: float = 6.0  # commands.go:21 azimuthAccelMax
TCS_EL_MAX_ACCELERATION: float = 1.5  # commands.go:27 elevationAccelMax

#: Go TCS *hardware* velocity ceilings (deg/s), the quantity ``checkAzEl``
#: actually enforces (strict ``> max``). Used by the multi-axis pong/daisy/source
#: dynamics escalation, whose elevation MOVES: the site el-velocity limit (1.0)
#: is TIGHTER than this hardware ceiling (1.5), so escalating the library
#: ``VelocityLimitWarning`` would wrongly reject an el rate in (1.0, 1.5] the Go
#: TCS would accept. The guard instead compares realised az/el velocity against
#: THESE ceilings, over the WHOLE trajectory (Go TCS validates only the first
#: 100 points). ``build_constant_el_payload`` escalates the library warning
#: directly instead, because a CE scan holds el fixed (el_vel == 0) and the site
#: az-velocity limit (3.0) already equals this hardware az ceiling.
TCS_AZ_MAX_VELOCITY: float = 3.0  # commands.go:20 azimuthSpeedMax
TCS_EL_MAX_VELOCITY: float = 1.5  # commands.go:26 elevationSpeedMax

#: Free-program-track-stack count that signals the scan has DRAINED.
#: ``maxFreeProgramTrackStack`` is 10000; ``startPattern`` calls
#: ``ProgramTrackClear()`` (free -> 10000) and only THEN spawns the upload
#: goroutine (``telescope.go:164-172``), so a freshly cleared, not-yet-uploaded
#: stack reads 10000. A played-out track drains to exactly one point left, i.e.
#: free == 9999 (Go's own ``isDone`` keys on strict ``== 9999``,
#: ``commands.go:195``). The completion check MUST use strict equality to 9999,
#: not ``>= 9999``: the empty pre-upload stack (10000) satisfies ``>= 9999`` and
#: would be a false "complete" on the first poll after POST, before the scan ran.
TCS_PROGRAM_TRACK_DRAINED: int = 9999  # maxFreeProgramTrackStack-1 (commands.go:16,195)

#: Axis-velocity magnitude (deg/s) below which an axis is "stopped".
TCS_SPEED_TOL: float = 1e-4  # commands.go:15 speedTol

#: Maximum allowed delay (s) between dispatch and the resolved scan start.
#: plan_constant_el_scan treats start_time as a forward-search anchor, so a
#: below-horizon field can resolve a crossing many hours out (up to
#: max_search_hours, default 12 h). The PCS task slews to the scan start
#: immediately and holds azel_lock until start_time, so an unbounded delay parks
#: the dish on empty sky for hours and blocks all other azel ops. Applied to both
#: dispatch paths. The root-semantics fix (take the elevation from upstream so
#: the resolve is always ~now and this cap rarely binds) is deferred to the
#: survey-to-execution handoff; this is the interim guard, not its substitute.
MAX_DISPATCH_DELAY_SEC: float = 1800.0  # 30 min


#: Maximum tolerated post-slew re-floor advance (s) of a sidereal-tracked scan's
#: start_time before its baked az/el track is too stale to POST. The az/el samples
#: are frozen at the build-time start; advancing start_time by ``delta`` replays
#: them ``delta`` s late, so the boresight lags the sky by ~az_rate*delta (az_rate
#: ~3-27 arcsec/s at FYST fixed el). 30 s caps the worst-case lag at ~0.014 deg
#: (~50 arcsec), inside az_padding and a small fraction of a PrimeCam module FOV.
#: delta is normally 0 (scheduled / comfortably-future scans); it grows only for a
#: dispatch-now scan whose crossing landed near the buffer floor and whose slew ran
#: long. A breach means the slew ate the lead so badly the geometry is stale.
#: Refuse (retryable) rather than scan drifted sky. Re-deriving on re-floor is the
#: proper fix but reshapes the dispatch flow (it would re-pick the slew target
#: after the dish already slewed); deferred. Tune against commissioning slews.
MAX_REFLOOR_DRIFT_SEC: float = 30.0


class TrajectoryValidationError(RuntimeError):
    """Raised when a built trajectory would breach a Go-TCS-enforced limit.

    A velocity/acceleration breach anywhere in the trajectory is turned into a
    hard failure before the body is POSTed, because the Go TCS validates only the
    first 100 points and ``validate_trajectory`` only warns.
    """


class DispatchDelayError(RuntimeError):
    """Raised when the resolved scan start_time is too far past dispatch."""


def floor_dispatch_start(scheduled_t0_unix: float | None, now_unix: float) -> Time:
    """Apply the dispatch-buffer floor to a scan's start anchor (shared).

    Returns ``max(scheduled_t0_unix or 0, now_unix + SCAN_DISPATCH_BUFFER_SEC)``
    as an absolute ``astropy.time.Time``, the ``start_time`` the planners take.
    The floor guarantees the search anchor (and so the posted ``start_time``,
    which is at or after it) clears the Go TCS minimum lead (``< ~9.8 s``
    rejected, ``commands.go:253``). Used by every typed scan task's payload core.

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
) -> None:
    """Reject a scan whose resolved start is too far past dispatch (shared).

    The elevation-searching planners (``plan_constant_el_scan``,
    ``plan_source_ces``) treat ``start_time`` as a forward-search anchor, so a
    below-horizon field can resolve a crossing many hours out (up to
    ``max_search_hours``). The task slews to the scan start immediately and holds
    ``azel_lock`` until ``start_time``, so an unbounded delay parks the dish on
    empty sky for hours and blocks all other azel ops. Raise before any slew is
    computed. Pong and daisy take ``start_time`` literally (no forward search),
    so their resolved start equals the floored anchor and this passes trivially;
    applied uniformly so all four tasks share one path.

    Pass ``max_dispatch_delay_sec=float("inf")`` to disable (e.g. contract tests
    not concerned with timeliness). ``context`` is woven into the error message.

    Raises
    ------
    DispatchDelayError
        If ``resolved_start_unix - now_unix`` exceeds ``max_dispatch_delay_sec``.
    """
    delay = resolved_start_unix - now_unix
    if delay > max_dispatch_delay_sec:
        raise DispatchDelayError(
            f"Resolved {context} start_time is {delay / 3600:.2f} h after dispatch "
            f"(> {max_dispatch_delay_sec / 3600:.2f} h limit). The target does not "
            f"reach the requested geometry promptly; a scan-now dispatch must pass a "
            f"promptly-reachable elevation. Refusing to slew and hold azel_lock."
        )


class ScanCompletionLatch:
    """Decide when a POSTed ``/path`` scan has genuinely completed.

    The Go TCS ``/path`` endpoint is fire-and-forget: HTTP 200 means "accepted",
    not "done", and the Go TCS never calls back. The PCS Process detects
    completion by polling the ACU status for the stack-drained signal. Two
    hazards make a naive ``free >= 9999`` check wrong on the first poll after POST:

    1. **Empty pre-upload stack.** ``startPattern`` calls ``ProgramTrackClear()``
       (free -> 10000) and only then spawns the upload goroutine
       (``telescope.go:164-172``). For a brief window after the POST returns 200
       the stack is *empty* (free == 10000), which satisfies ``>= 9999``, a
       false "complete". The real drained signal is the strict free == 9999 the
       Go ``isDone`` uses (``commands.go:195``); 10000 means "not uploaded yet".
    2. **No running-observed latch.** Even strict free == 9999 is ambiguous on
       the *first* poll (a transient 9999 read before the upload goroutine pushes
       points). So a drained reading is honored only once the scan has been
       observed RUNNING at least once, after ANY of: a non-empty stack seen
       (``free < 9999``); ACU ProgramTrack mode active; or wall-clock reached the
       absolute ``start_time``. This is the latch.

    A tiny mutable state machine so the agent's poll loop stays a thin wrapper and
    the decision is unit-testable without ``ocs`` / ``twisted``. Feed each poll's
    status to :meth:`update`; it returns ``True`` exactly once the scan is
    confirmed complete. The agent keeps its own absolute-time backstop and abort
    handling around this.

    ``start_time_unix`` is the POSTed trajectory's absolute start; reaching it is
    one of the three arming conditions (covers a status stream that never surfaces
    a non-empty stack or a mode string). ``drained_free`` /``speed_tol`` default
    to :data:`TCS_PROGRAM_TRACK_DRAINED` / :data:`TCS_SPEED_TOL`.
    """

    #: ACU az/el mode strings that count as "ProgramTrack active". Matched
    #: case-insensitively as a substring so vendor variants ("ProgramTrack",
    #: "Program Track", "ProgramTrackTime") all arm the latch.
    _PROGRAM_TRACK_MODE_TOKEN = "programtrack"

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

    def _mode_is_program_track(self, mode) -> bool:
        if mode is None:
            return False
        normalized = str(mode).strip().lower().replace(" ", "")
        return self._PROGRAM_TRACK_MODE_TOKEN in normalized

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

        ``free`` is the free program-track stack count (``Qty of free program
        track stack positions`` / ``Free_upload_positions``); ``vaz``/``vel`` the
        az/el current velocity (deg/s); any may be ``None`` if the read did not
        surface it. ``az_mode``/``el_mode`` ProgramTrack either arms the latch.

        Returns ``True`` exactly when the latch has armed (running observed) AND
        the stack is strictly drained AND both axis velocities are below
        ``speed_tol``.
        """
        # Arm the latch on ANY running-observed signal. A non-empty stack means
        # the upload happened and points remain; the scan is (or was) running.
        if free is not None and free < self.drained_free:
            self.running_observed = True
        if self._mode_is_program_track(az_mode) or self._mode_is_program_track(el_mode):
            self.running_observed = True
        if now_unix >= self.start_time_unix:
            self.running_observed = True

        if not self.running_observed:
            # Pre-run window: the stack may read 10000 (empty, pre-upload) or a
            # transient 9999 before the goroutine pushes points. Do NOT accept a
            # drained signal yet.
            return False

        return (
            free is not None
            and free == self.drained_free
            and vaz is not None
            and abs(vaz) < self.speed_tol
            and vel is not None
            and abs(vel) < self.speed_tol
        )


def _choose_slew_and_align(
    traj,
    *,
    current_az: float,
    current_el: float,
    obstime: Time,
    site: Site,
    sun_safe,
):
    """Pick a sun-safe encoder slew target and wrap-align the trajectory to it.

    Shared step 3 + 4 for every typed scan task's payload core:

    1. Choose a sun-safe encoder ``(az, el)`` for the trajectory's first sample
       via :func:`fyst_trajectories.dispatch.choose_encoder_solution`, sunning
       the wrap against ``obstime``, the RESOLVED scan start the planner found,
       not the dispatch anchor: the Sun moves in the gap and the slew target must
       be safe when the dish actually arrives.
    2. Shift the WHOLE trajectory azimuth by the 360-deg multiple that lands its
       first sample on the chosen encoder azimuth, so the posted ``/path`` and
       the slew target share one azimuth wrap.

    ``current_az``/``current_el`` are the current encoder position (deg, 200 Hz
    broadcast); ``sun_safe`` is forwarded to ``choose_encoder_solution``. Returns
    ``(encoder_az, encoder_el, aligned_traj)`` with ``aligned_traj.az[0] ==
    encoder_az``. Raises ``PointingError`` from ``choose_encoder_solution`` (goal
    el out of range, no in-range wrap, or every wrap sun-blocked).
    """
    enc_az, enc_el = choose_encoder_solution(
        current_az=current_az,
        current_el=current_el,
        goal_az=float(traj.az[0]),
        goal_el=float(traj.el[0]),
        obstime=obstime,
        site=site,
        sun_safe=sun_safe,
    )
    # Trajectory is (effectively) immutable in library use, so replace it rather
    # than mutating in place.
    shift = round((enc_az - float(traj.az[0])) / 360.0) * 360.0
    if shift:
        traj = dataclasses.replace(traj, az=traj.az + shift)
    return enc_az, enc_el, traj


def _escalate_velocity_warning(caught, scan_label: str) -> None:
    """Escalate a library :class:`VelocityLimitWarning` to a hard error.

    Used by :func:`build_constant_el_payload` only. ``validate_trajectory`` only
    *warns* on a velocity breach, but velocity is the quantity the Go TCS
    ``checkAzEl`` enforces, so a breach must abort BEFORE the POST. Safe for a CE
    scan: its elevation is held fixed (el_vel == 0) and the site az-velocity limit
    (3.0) equals the Go TCS hardware az ceiling, so the library warning fires at
    exactly the Go TCS contract. The multi-axis tasks use
    :func:`_escalate_hardware_dynamics` instead (their site el-velocity limit is
    tighter than hardware). ``caught`` is the warnings list captured around
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


def _escalate_hardware_dynamics(traj, scan_label: str) -> None:
    """Escalate a Go-TCS *hardware* velocity / acceleration breach.

    Used by the multi-axis tasks (:func:`build_source_payload`,
    :func:`build_pong_payload`, :func:`build_daisy_payload`) where elevation
    MOVES. Computes realised az/el velocity + acceleration via ``np.gradient``
    over the WHOLE trajectory and raises :class:`TrajectoryValidationError` if any
    exceeds the Go TCS *hardware* ceiling. Three reasons it does NOT just escalate
    the library ``validate_trajectory_dynamics`` warnings:

    1. Velocity: the library warns at the *site* limits, whose el value (1.0
       deg/s) is TIGHTER than the hardware ceiling (1.5); escalating it would
       reject an el rate in (1.0, 1.5] the Go TCS would accept.
    2. Acceleration: ``checkAzEl`` does not check acceleration at all, so the
       library's site accel warning (az 1.0, el 0.5) is far below the hardware
       ceilings (6.0, 1.5): nominal pong/daisy turnarounds already exceed the
       site limit but sit well inside hardware, so the guard MUST compare against
       hardware or it would reject every scan.
    3. Whole-trajectory: the Go TCS validates only the first 100 points
       (commands.go:269), so the agent enforces the ceiling everywhere.

    Pong/daisy turnarounds are smooth and well-resolved at the 0.1 s timestep, so
    np.gradient is right for their acceleration. The CE AND source quintic
    turnaround is NOT (its short, sharp profile np.gradient under-resolves), so
    build_constant_el_payload and build_source_payload each add a separate analytic
    az-acceleration guard (peak = 1.5 * az_accel); this helper still covers their
    velocity (and source's el dynamics).
    """
    times = np.asarray(traj.times, dtype=float)
    if times.size < 2:
        return
    az = np.unwrap(np.asarray(traj.az, dtype=float), period=360.0)
    el = np.asarray(traj.el, dtype=float)
    az_vel = np.gradient(az, times)
    el_vel = np.gradient(el, times)
    max_vaz = float(np.abs(az_vel).max())
    max_vel = float(np.abs(el_vel).max())

    breaches: list[str] = []
    if max_vaz > TCS_AZ_MAX_VELOCITY:
        breaches.append(
            f"peak az velocity {max_vaz:.3f} deg/s exceeds the Go TCS hardware "
            f"ceiling {TCS_AZ_MAX_VELOCITY} deg/s (commands.go:20)."
        )
    if max_vel > TCS_EL_MAX_VELOCITY:
        breaches.append(
            f"peak el velocity {max_vel:.3f} deg/s exceeds the Go TCS hardware "
            f"ceiling {TCS_EL_MAX_VELOCITY} deg/s (commands.go:26)."
        )

    if times.size >= 4:
        az_accel = np.gradient(az_vel, times)
        el_accel = np.gradient(el_vel, times)
        max_aaz = float(np.abs(az_accel).max())
        max_ael = float(np.abs(el_accel).max())
        if max_aaz > TCS_AZ_MAX_ACCELERATION:
            breaches.append(
                f"peak az acceleration {max_aaz:.3f} deg/s^2 exceeds the Go TCS "
                f"hardware ceiling {TCS_AZ_MAX_ACCELERATION} deg/s^2 (commands.go:21)."
            )
        if max_ael > TCS_EL_MAX_ACCELERATION:
            breaches.append(
                f"peak el acceleration {max_ael:.3f} deg/s^2 exceeds the Go TCS "
                f"hardware ceiling {TCS_EL_MAX_ACCELERATION} deg/s^2 (commands.go:27)."
            )

    if breaches:
        raise TrajectoryValidationError(
            f"{scan_label} trajectory breaches a Go TCS hardware dynamics ceiling "
            f"anywhere along the path (the Go TCS only validates the first 100 "
            f"points); refusing to POST. " + " ".join(breaches)
        )


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
       :func:`fyst_trajectories.planning.plan_constant_el_scan`. The scan's
       ``velocity`` is passed straight through as mount-frame
       azimuth-coordinate deg/s (*not* scaled by ``cos(el)``).
    3. Picks a sun-safe encoder ``(az, el)`` to slew to via
       :func:`fyst_trajectories.dispatch.choose_encoder_solution`, current-
       position-aware.
    4. Shifts the whole trajectory azimuth by the 360-deg multiple that aligns
       its first sample to the chosen encoder azimuth (wrap alignment), then
       re-validates.
    5. Escalates a velocity breach to :class:`TrajectoryValidationError`;
       position-bounds breaches already raise.
    6. Returns the encoder target plus the exact three-key ``/path`` body.

    Parameters
    ----------
    scan_params : dict
        Constant-elevation scan spec, modeled on
        :func:`~fyst_trajectories.planning.plan_constant_el_scan`. Required:
        ``ra_center``, ``dec_center``, ``width``, ``height`` (deg, the
        :class:`~fyst_trajectories.planning.FieldRegion`); ``elevation`` (deg);
        ``velocity`` (deg/s, mount-frame azimuth-coordinate, sent to the ACU
        as-is, NOT cos(el)-scaled). Optional (defaults match the planner):
        ``rising``, ``angle``, ``az_accel``, ``timestep``, ``az_padding``,
        ``max_search_hours``, ``step_seconds``, ``lsa_window``.
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
        allows. The planner's *search anchor*, not the literal start:
        ``plan_constant_el_scan`` searches forward for the elevation crossing and
        starts there, so the posted ``start_time`` is the resolved crossing (>=
        the floored anchor >= ``now + buffer``).
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
        If a required ``scan_params`` key is missing.
    PointingError
        If the goal el is out of range or every in-range az wrap is sun-blocked.
    AzimuthBoundsError, ElevationBoundsError
        If the (wrap-shifted) trajectory leaves the telescope limits anywhere.
    TrajectoryValidationError
        If az/el velocity exceeds the site limit anywhere, or the quintic
        turnaround's peak az accel (``1.5 * az_accel``) exceeds the Go TCS
        hardware ceiling.
    DispatchDelayError
        If the resolved ``start_time`` is more than ``max_dispatch_delay_sec``
        after ``now_unix``.
    """
    actual_t0 = floor_dispatch_start(scheduled_t0_unix, now_unix)

    # Build the trajectory. Velocity passes straight through as mount-frame
    # azimuth-coordinate deg/s (no cos(el) scaling).
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
        rising=scan_params.get("rising", True),
        angle=scan_params.get("angle", 0.0),
        az_accel=scan_params.get("az_accel", 1.0),
        timestep=scan_params.get("timestep", 0.1),
        az_padding=scan_params.get("az_padding", 2.0),
        max_search_hours=scan_params.get("max_search_hours", 12.0),
        step_seconds=scan_params.get("step_seconds", 30.0),
        lsa_window=tuple(lsa_window) if lsa_window is not None else None,
    )
    traj = block.trajectory

    # Dispatch-delay guard: reject a crossing that resolves too far out before
    # any slew is computed, else the task slews to it and holds azel_lock until
    # start_time.
    enforce_dispatch_delay(
        float(block.trajectory.start_time.unix),
        now_unix,
        max_dispatch_delay_sec,
        context=(
            f"constant-el scan (ra={scan_params['ra_center']}, "
            f"dec={scan_params['dec_center']}, el={scan_params['elevation']})"
        ),
    )

    # Sun-safe encoder slew target + wrap alignment. Sun the wrap against the
    # resolved scan start, not the dispatch anchor: start_time is a forward-search
    # anchor, so the trajectory can begin many minutes after actual_t0, the Sun
    # moves in that gap, and the slew target must be safe when the dish arrives.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        obstime=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
    )

    # Validate + escalate. validate_trajectory raises on a position breach over
    # the whole trajectory (closing the Go TCS first-100-point gap for position)
    # but only warns on dynamics, so escalate a velocity breach (the quantity
    # checkAzEl enforces) to a hard error. Escalating the library warning directly
    # is safe here: el is fixed (el_vel == 0) and the site az-velocity limit equals
    # the hardware az ceiling. Multi-axis tasks use _escalate_hardware_dynamics.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        validate_trajectory(traj, site)  # may raise Azimuth/ElevationBoundsError
    _escalate_velocity_warning(caught, "Constant-el")

    # Acceleration guard: the CE quintic turnaround peaks at 1.5 * az_accel by
    # design (fyst_trajectories.patterns.turnarounds.quintic_turnaround). Compute
    # it analytically, not via np.gradient, which under-resolves the short
    # turnaround at the 0.1 s timestep and would pass a true >6 deg/s^2 scan.
    # checkAzEl ignores acceleration, so escalate az here before POST (el fixed).
    az_accel = abs(scan_params.get("az_accel", 1.0))
    peak_az_accel = 1.5 * az_accel
    if peak_az_accel > TCS_AZ_MAX_ACCELERATION:
        raise TrajectoryValidationError(
            f"Constant-el scan az_accel={az_accel:.3f} deg/s^2 produces a quintic "
            f"turnaround peak acceleration of {peak_az_accel:.3f} deg/s^2, which "
            f"exceeds the Go TCS hardware ceiling of {TCS_AZ_MAX_ACCELERATION} "
            f"deg/s^2 (commands.go:21); refusing to POST."
        )

    # Assemble the three-key /path body (coordsys defaults to "Horizon"). After
    # the wrap shift, payload["points"][0][1] == enc_az.
    payload = to_path_payload(traj)
    return {"encoder_az": enc_az, "encoder_el": enc_el, "payload": payload}


# Off-centre source_ces is gated on two currently-unconfirmed quantities. See
# build_source_payload for the gate and the full rationale.
_OFF_CENTRE_GATE_NOTE = (
    "off-centre (single-module) source_scan and an explicit boresight_rot both "
    "depend on the Nasmyth port direction and the boresight rotation value, "
    "neither of which is confirmed yet; guessing either rotates the focal "
    "plane by up to 2*el deg, so only centred scans are supported for now."
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
    ``boresight_rot`` left ``None`` (uncommanded rotator). The off-centre
    single-module case and an explicit commanded ``boresight_rot`` are GATED OFF:
    ``plan_source_ces``'s off-centre boresight recovery and cover projection
    rotate the footprint by ``nasmyth_sign * el_bore + boresight_rot``, and both
    the Nasmyth sign and the ``boresight_rot`` value are currently UNCONFIRMED. A
    wrong sign rotates the focal plane by up to ``2 * el`` degrees, so rather than
    guess, this raises :class:`ValueError` for an off-centre ``footprint`` or a
    non-``None`` ``boresight_rot``. Remove the gate once those are confirmed.

    Velocity frame: ``plan_source_ces`` builds from a
    :class:`~fyst_trajectories.ConstantElScanConfig` whose ``az_speed`` is the
    solved per-leg drift in MOUNT-frame azimuth-coordinate deg/s; ``cos(el)`` is
    already implicit in the elevation-fixed azimuth track and is NOT re-applied
    (the solved drift ``v_az`` is likewise a mount-frame rate). So like
    constant-el (unlike pong/daisy), the commanded az velocity is mount-frame.

    Parameters
    ----------
    scan_params : dict
        Source-CES spec, modeled on
        :func:`~fyst_trajectories.planning.plan_source_ces`. Provide a source,
        ``body`` (solar-system name) OR both ``ra`` and ``dec`` (deg), plus
        ``el_bore`` (deg, required). Optional (defaults match the planner):
        ``footprint`` (MUST be ``"c"``/``"center"`` while the gate stands),
        ``boresight_rot`` (MUST be ``None`` while the gate stands), ``mode``
        (``"rising"``/``"setting"``), ``pm_ra``, ``pm_dec``, ``ref_epoch`` (ISO
        str or ``Time``), ``timestep``, ``sampling_step_seconds``, ``az_accel``,
        ``az_padding``, ``az_branch``, ``allow_partial``, ``v_az``. Search window
        is the floored anchor (``night=actual_t0``) with ``mode``; pass a
        ``window`` 2-sequence of ISO strings / ``Time`` to override.
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
        centred-only gate), or an incompatible source/window combo.
    KeyError
        If ``el_bore`` (or a source spec) is missing.
    PointingError, TargetNotObservableError
        If the source never reaches ``el_bore``, the goal el is out of range, or
        every in-range az wrap is sun-blocked.
    AzimuthBoundsError, ElevationBoundsError
        If the (wrap-shifted) trajectory leaves the telescope limits anywhere.
    TrajectoryValidationError
        If az/el velocity or acceleration exceeds a Go TCS hardware ceiling
        anywhere.
    DispatchDelayError
        If the resolved start is more than ``max_dispatch_delay_sec`` out.
    """
    actual_t0 = floor_dispatch_start(scheduled_t0_unix, now_unix)

    # Centred-only gate: a wrong Nasmyth sign / boresight_rot rotates the
    # off-centre footprint by up to 2*el deg, so refuse the off-centre and
    # commanded-rotation paths rather than guess (see docstring).
    footprint = scan_params.get("footprint", "c")
    if footprint not in ("c", "center"):
        raise ValueError(
            f"source_scan currently supports only the centred PrimeCam footprint "
            f'("c"/"center"), got {footprint!r}: {_OFF_CENTRE_GATE_NOTE}'
        )
    if scan_params.get("boresight_rot") is not None:
        raise ValueError(
            f"source_scan currently supports only an uncommanded boresight "
            f"rotator (boresight_rot=None), got "
            f"{scan_params.get('boresight_rot')!r}: {_OFF_CENTRE_GATE_NOTE}"
        )

    # Build the source-tracking CES (centred footprint "c"). Velocity is
    # mount-frame: plan_source_ces bakes the solved drift into a
    # ConstantElScanConfig.az_speed, and the v_az drift is also a mount-frame az
    # rate; neither is cos(el)-scaled here.
    window = scan_params.get("window")
    ref_epoch = scan_params.get("ref_epoch")
    if isinstance(ref_epoch, str):
        ref_epoch = Time(ref_epoch, scale="utc")
    if window is not None:
        w0, w1 = window
        window = (
            Time(w0, scale="utc") if isinstance(w0, str) else w0,
            Time(w1, scale="utc") if isinstance(w1, str) else w1,
        )
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
        footprint=footprint,
        el_bore=scan_params["el_bore"],
        boresight_rot=None,
        window=window,
        night=night,
        mode=mode,
        site=site,
        timestep=scan_params.get("timestep", 0.1),
        sampling_step_seconds=scan_params.get("sampling_step_seconds", 30.0),
        az_accel=scan_params.get("az_accel", 1.0),
        az_padding=scan_params.get("az_padding", 0.5),
        az_branch=scan_params.get("az_branch"),
        allow_partial=scan_params.get("allow_partial", False),
        v_az=scan_params.get("v_az"),
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
        context=f"source scan ({src_label}, el_bore={scan_params['el_bore']})",
    )

    # Sun-safe encoder slew target + wrap alignment. Sun the wrap at the resolved
    # scan start.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        obstime=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
    )

    # Validate position over the whole trajectory (raises on a bounds breach,
    # closing the Go TCS first-100 gap) + escalate a Go TCS hardware velocity or
    # acceleration breach. El moves in a source CES, so the hardware-ceiling check,
    # not the tighter site-limit library warning, is the correct Go TCS contract;
    # see _escalate_hardware_dynamics.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_trajectory(traj, site)  # may raise Azimuth/ElevationBoundsError
    _escalate_hardware_dynamics(traj, "Source-CES")

    # Analytic turnaround-accel guard: plan_source_ces builds on a
    # ConstantElScanConfig, so the az axis uses the same quintic turnaround as a
    # CE scan (peak |az accel| = 1.5 * az_accel by design). np.gradient (in
    # _escalate_hardware_dynamics) under-resolves that short spike at the 0.1 s
    # timestep, so guard az accel analytically here too, mirroring
    # build_constant_el_payload, the only pre-POST gate for the quintic.
    az_accel = abs(scan_params.get("az_accel", 1.0))
    peak_az_accel = 1.5 * az_accel
    if peak_az_accel > TCS_AZ_MAX_ACCELERATION:
        raise TrajectoryValidationError(
            f"Source-CES scan az_accel={az_accel:.3f} deg/s^2 produces a quintic "
            f"turnaround peak acceleration of {peak_az_accel:.3f} deg/s^2, which "
            f"exceeds the Go TCS hardware ceiling of {TCS_AZ_MAX_ACCELERATION} "
            f"deg/s^2 (commands.go:21); refusing to POST."
        )

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
        the planner): ``angle``, ``n_cycles``, ``timestep``.
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
        start equals the floored anchor, so this passes trivially; applied
        uniformly for parity with the elevation-searching tasks.

    Returns
    -------
    dict
        ``{"encoder_az", "encoder_el", "payload"}`` (three-key ``/path`` body);
        ``payload["points"][0][1] == encoder_az``.

    Raises
    ------
    KeyError
        If a required key is missing.
    ValueError
        If ``n_cycles < 1`` (from ``plan_pong_scan``).
    TargetNotObservableError, AzimuthBoundsError, ElevationBoundsError
        If the field is unobservable at the start time or the trajectory leaves
        the telescope limits anywhere.
    PointingError
        If every in-range az wrap is sun-blocked.
    TrajectoryValidationError
        If az/el velocity or acceleration exceeds a Go TCS hardware ceiling
        anywhere.
    DispatchDelayError
        If the resolved start is more than ``max_dispatch_delay_sec`` out.
    """
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
        n_cycles=scan_params.get("n_cycles", 1),
    )
    traj = block.trajectory

    # Dispatch-delay guard: trivially satisfied for Pong (starts at the floored
    # anchor); applied for parity with CE/source.
    enforce_dispatch_delay(
        float(block.trajectory.start_time.unix),
        now_unix,
        max_dispatch_delay_sec,
        context=(
            f"pong scan (ra={scan_params['ra_center']}, dec={scan_params['dec_center']})"
        ),
    )

    # Sun-safe encoder slew target + wrap alignment.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        obstime=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
    )

    # Validate position over the whole trajectory + escalate a Go TCS hardware
    # velocity or acceleration breach. El moves in a Pong, so the hardware ceiling,
    # not the tighter site-limit library warning, is the Go TCS contract.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_trajectory(traj, site)  # may raise Azimuth/ElevationBoundsError
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
        match the planner): ``y_offset``, ``timestep``.
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
        start equals the floored anchor, so this passes trivially; applied
        uniformly for parity with the elevation-searching tasks.

    Returns
    -------
    dict
        ``{"encoder_az", "encoder_el", "payload"}`` (three-key ``/path`` body);
        ``payload["points"][0][1] == encoder_az``.

    Raises
    ------
    KeyError
        If a required key is missing.
    TargetNotObservableError, AzimuthBoundsError, ElevationBoundsError
        If the source is unobservable at the start time or the trajectory leaves
        the telescope limits anywhere.
    PointingError
        If every in-range az wrap is sun-blocked.
    TrajectoryValidationError
        If az/el velocity or acceleration exceeds a Go TCS hardware ceiling
        anywhere.
    DispatchDelayError
        If the resolved start is more than ``max_dispatch_delay_sec`` out.
    """
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
    )
    traj = block.trajectory

    # Dispatch-delay guard: trivially satisfied for Daisy (starts at the floored
    # anchor); applied for parity with CE/source.
    enforce_dispatch_delay(
        float(block.trajectory.start_time.unix),
        now_unix,
        max_dispatch_delay_sec,
        context=f"daisy scan (ra={scan_params['ra']}, dec={scan_params['dec']})",
    )

    # Sun-safe encoder slew target + wrap alignment.
    enc_az, enc_el, traj = _choose_slew_and_align(
        traj,
        current_az=current_az,
        current_el=current_el,
        obstime=block.trajectory.start_time,
        site=site,
        sun_safe=sun_safe,
    )

    # Validate position over the whole trajectory + escalate a Go TCS hardware
    # velocity or acceleration breach. El moves in a Daisy, so the hardware ceiling,
    # not the tighter site-limit library warning, is the Go TCS contract.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        validate_trajectory(traj, site)  # may raise Azimuth/ElevationBoundsError
    _escalate_hardware_dynamics(traj, "Daisy")

    payload = to_path_payload(traj)
    return {"encoder_az": enc_az, "encoder_el": enc_el, "payload": payload}


def refloor_payload_start_time(payload: dict, now_unix: float) -> dict:
    """Re-apply the dispatch-buffer floor to a built payload's ``start_time``.

    :func:`build_constant_el_payload` floors ``start_time`` at *build* time, but
    the PCS task then slews to the scan start and that slew can block up to
    ``SLEW_TIMEOUT_SEC`` (180 s). A dispatch-now scan whose resolved crossing
    landed only just above the 10 s floor can, by the time the slew finishes, be
    *less* than the Go TCS ``pathCmd.Check()`` minimum lead (rejected at
    ``< ~9.8 s`` out, ``commands.go:253``), so the POST would fail. Re-floor with
    a *fresh* ``now`` just before POST so the body still clears the receiver.

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


def tcs_response_status(response) -> int | None:
    """Read an HTTP status code from an ``aculib`` TCS return, 503-safe.

    ``aculib.observatory_control_system.post()`` returns the ``requests``
    ``Response`` on success but short-circuits a **HTTP 503** to a bare ``{}``
    (aculib.py:122-124), the status the Go TCS returns when a command is rejected
    because a prior one is still running (e.g. ``/path`` POSTed before the slew
    settled, or a ``move-to`` the ACU could not accept). A caller doing
    ``response.status_code`` on that ``{}`` raises ``AttributeError`` *before* its
    ``!= 200`` guard can turn the rejection into a graceful failure. This reads the
    code defensively so a non-``Response`` (``{}``, ``None``) maps to ``None``:
    "not 200, treat as rejected". Factored out so the decision is unit-testable
    without ``ocs`` / ``twisted``. Compare the result against ``200``.
    """
    return getattr(response, "status_code", None)
