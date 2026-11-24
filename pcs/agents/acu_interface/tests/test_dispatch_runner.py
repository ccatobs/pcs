"""Drive-the-runner test for the shared scan body.

ACTUALLY EXECUTES ``ACUAgent._dispatch_scan_process``, the single runtime path
for the four typed scans, rather than only
inspecting its source. The sibling files unit-test the ocs-free primitives; this
covers the COMPOSITION the runner wires: the ``latch.update(...)`` call, the
slew-arrival comparison, the post-slew re-floor, and the ``_safe_abort`` routing.

The agent imports the SO ``ocs`` framework, absent on a dev box (``ocs`` resolves
to the single-file OCS REST client, so ``ocs.ocs_agent`` is unimportable).
``twisted`` IS present, so we stub ONLY the three ``ocs`` names the module
imports, then drive the ``@inlineCallbacks`` body synchronously (``deferToThread``
-> ``maybeDeferred``, ``dsleep`` -> fired no-op). Where the ocs framework is installed the stub is skipped
and the same tests run against the genuine module. The stub gives ``ocs`` no
package ``__path__``, so ``find_spec('ocs.ocs_agent')`` still fails and the
source-inspection skip-guards elsewhere stay skipped.
"""

import importlib.util
import logging
import math
import sys
import time
import types
from contextlib import contextmanager

import pytest

_AGENT = None
_IMPORT_ERR = None
try:
    try:
        _real_ocs = importlib.util.find_spec("ocs.ocs_agent") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        _real_ocs = False
    if not _real_ocs:
        import ocs  # the single-file REST client

        ocs.ocs_agent = types.SimpleNamespace(param=lambda *a, **k: (lambda f: f))
        ocs.site_config = types.SimpleNamespace()
        _otw = types.ModuleType("ocs.ocs_twisted")
        _otw.TimeoutLock = type("TimeoutLock", (), {})
        sys.modules.setdefault("ocs.ocs_twisted", _otw)
        ocs.ocs_twisted = _otw
    from twisted.internet import defer
    from twisted.python.failure import Failure

    from pcs.agents.acu_interface import agent as _AGENT
except Exception as exc:  # pragma: no cover - environment-dependent
    _IMPORT_ERR = exc

pytestmark = pytest.mark.skipif(
    _AGENT is None, reason=f"agent module not importable ({_IMPORT_ERR})"
)

ENC_AZ, ENC_EL = 120.0, 45.0


class _Log:
    def info(self, *a, **k):
        pass

    warn = error = debug = info


class _WarnLog(_Log):
    """Keeps the warnings the runner logs, formatted from their keyword arguments
    as the agent's Twisted logger formats them."""

    def __init__(self):
        self.warnings = []

    def warn(self, msg, *a, **k):
        self.warnings.append(msg.format(**k))


#: The body the OCS API answers an accepted command with.
ACCEPTED = {"status": "ok"}


class _Resp:
    """An HTTP answer; ``body`` None is one that is not JSON."""

    def __init__(self, code=200, text="ok", body=ACCEPTED):
        self.status_code = code
        self.text = text
        self.body = body

    def json(self):
        if self.body is None:
            raise ValueError("no JSON body")  # as requests does on a non-JSON body
        return self.body


class _FakeSession:
    def __init__(self, status="running"):
        self.status = status


class _FakeLock:
    def __init__(self):
        self.job = None

    @contextmanager
    def acquire_timeout(self, timeout, job=None):
        self.job = job
        yield True


def _acu(free, mode, vaz=0.0, position=None, commanded=None):
    """One ACU status read as the OCS API's /status serves it: ``free`` free
    program-track stack positions, both axes in ``mode`` (no mode keys when it
    is None), the azimuth moving at ``vaz``, the elevation stopped and, when
    ``position`` is given, the encoder ``(az, el)`` the dish is at and the
    ``commanded`` one, by default the same. A drained
    stack reads 9999 (the Go TCS's done test, startPattern's isDone at TCS ``b7bbbf9``),
    and so can a cleared one: the ACU simulator keeps one point through a
    clear."""
    status = {
        "QtyOfFreeProgramTrackStackPositions": free,
        "AzimuthCurrentVelocity": vaz,
        "ElevationCurrentVelocity": 0.0,
    }
    if mode is not None:
        status["AzimuthMode"] = status["ElevationMode"] = mode
    if position is not None:
        status["AzimuthCurrentPosition"], status["ElevationCurrentPosition"] = position
        status["AzimuthCommandedPosition"], status["ElevationCommandedPosition"] = (
            position if commanded is None else commanded)
    return status


def _without(status, *keys):
    """``status`` without ``keys``: a read that does not carry those fields."""
    return {k: v for k, v in status.items() if k not in keys}


#: The encoder (az, el) a status read gives while the ACU plays a track the Go
#: TCS has let go of.
HELD_AT = (123.4, 56.7)

#: A status read while the ACU plays such a track: points on its stack (5000
#: free) with both axes in ProgramTrack, the dish at HELD_AT.
HELD = _acu(5000, "ProgramTrack", position=HELD_AT)

#: Go's answer to an /abort while it runs no command, as the OCS API relays it
#: with HTTP 200 (the /abort handler at TCS b7bbbf9).
NOTHING_TO_ABORT = {"status": "error", "message": "nothing to abort"}


def _stopped_by_a_move_to(status):
    """``status`` as the ACU shows it once a move-to has stopped its track: a
    read with an axis in ProgramTrack shows both axes in Preset instead, as
    Telescope.MoveTo at TCS ``b7bbbf9`` sets Stop and then Preset, while the stack
    keeps its points and the dish its position. A read without the modes stays
    without them."""
    if "ProgramTrack" in (status.get("AzimuthMode"), status.get("ElevationMode")):
        return dict(status, AzimuthMode="Preset", ElevationMode="Preset")
    return dict(status)


class _FakeTCS:
    """Records calls and answers status reads in the order the ACU goes through
    a scan. Until a /path POST is accepted the reads show the slew: both axes in
    Preset, the stack at 9999 and the azimuth moving for the first
    ``settle_after`` reads, then stopped, with the dish on its commanded
    position, the last move-to's target plus ``raw_offset`` (the Go TCS's
    Sky2Raw correction); ``slew_read``, when given, rewrites each such read.
    After it they answer ``track`` in
    order, the last one repeating, an entry that is an exception being raised
    as aculib raises on a failed read: by default this track's points on the
    stack in ProgramTrack (5000 free) for one read, then the stack drained
    (9999) with both axes stopped. A move-to after the POST, the one that stops
    a track the Go TCS has let go of, is accepted and the ACU acts on it: the
    reads after it show the axes out of ProgramTrack
    (:func:`_stopped_by_a_move_to`). ``on_move_to``, ``on_status_read`` and
    ``on_path_accepted`` let a test act at those points, say to flip the session
    to 'stopping'; ``abort`` returns ``abort_reply``, or raises it when it is an
    exception."""

    def __init__(self, on_move_to=None, settle_after=1, track=None, on_status_read=None,
                 on_path_accepted=None, abort_reply=None, raw_offset=(0.0, 0.0),
                 slew_read=None):
        self.calls = []
        self.raw_offset = raw_offset
        self.slew_read = slew_read
        self._poll = 0
        self.slew_reads = 0
        self.track_reads = 0
        self.posted = None  # the accepted /path body
        self.track_stopped = False  # a move-to after the POST stopped the track
        self.on_move_to = on_move_to
        self.settle_after = settle_after
        self.track = list(track or [_acu(5000, "ProgramTrack"), _acu(9999, "ProgramTrack")])
        self.on_status_read = on_status_read
        self.on_path_accepted = on_path_accepted
        self.abort_reply = {"status": "ok"} if abort_reply is None else abort_reply

    def move_to(self, az, el):
        self.calls.append(("move_to", az, el))
        if self.on_move_to:
            self.on_move_to()
        if self.posted is not None:
            self.track_stopped = True
        return _Resp(200)

    def scan_pattern(self, payload):
        self.calls.append(("scan_pattern", dict(payload)))
        self._accept(payload)
        return _Resp(200)

    def _accept(self, payload):
        """The Go TCS took the path: later status reads describe the track."""
        self.posted = dict(payload)
        if self.on_path_accepted:
            self.on_path_accepted()

    def abort(self):
        self.calls.append(("abort",))
        if isinstance(self.abort_reply, BaseException):
            raise self.abort_reply
        return self.abort_reply

    def get_status(self):
        self._poll += 1
        if self.on_status_read:
            self.on_status_read()
        if self.posted is None:
            self.slew_reads += 1
            status = _acu(9999, "Preset", vaz=0.5 if self.slew_reads <= self.settle_after else 0.0,
                          position=self.commanded())
            return self.slew_read(status) if self.slew_read else status
        self.track_reads += 1
        read = self.track[min(self.track_reads, len(self.track)) - 1]
        if isinstance(read, BaseException):
            raise read
        return _stopped_by_a_move_to(read) if self.track_stopped else dict(read)

    def commanded(self):
        """The raw ``(az, el)`` the ACU holds as commanded: the last move-to's
        target plus ``raw_offset``, or ``None`` before any move-to."""
        moves = [c for c in self.calls if c[0] == "move_to"]
        if not moves:
            return None
        _, az, el = moves[-1]
        return az + self.raw_offset[0], el + self.raw_offset[1]


def _canned(start_in=5.0, span=1.0, encoder_az=ENC_AZ):
    """A canned build: the slew target (``encoder_az``, ``ENC_EL``) and a
    two-point path that starts ``start_in`` s after the build's now and spans
    ``span`` s. The default near-now start makes the post-slew re-floor a near
    no-op (drift well under the cap); a start far in the past drives the drift
    over it."""

    def build(**kwargs):
        now = kwargs.get("now_unix", time.time())
        return {
            "encoder_az": encoder_az,
            "encoder_el": ENC_EL,
            "payload": {
                "start_time": now + start_in,
                "coordsys": "Horizon",
                "points": [[0.0, encoder_az, ENC_EL, 0.0, 0.0],
                           [span, encoder_az + 0.1, ENC_EL, 0.1, 0.0]],
            },
        }

    return build


_canned_build = _canned()


class _TimedTCS(_FakeTCS):
    """A fake whose track runs on the test ``clock``. After the /path POST is
    accepted the first reads answer ``before_upload`` in order (what the ACU can
    show before the Go TCS's first upload lands). Then, when ``uploaded``, this
    track's points sit on the stack in ProgramTrack (5000 free), the dish still
    until the posted start and moving after it, until the posted end, after
    which the stack reads drained (9999) with both axes stopped; without
    ``uploaded`` the last ``before_upload`` read repeats. ``leaves_track``
    reads from 5 s after the start show this track's points with both axes in
    Stop (the ACU has left the track, as a fault or a stop sent from elsewhere
    leaves it), after which the track plays on. A move-to after the POST stops
    the track as it does for :class:`_FakeTCS`. Each read after the POST is
    kept in ``track_log`` with the clock and the phase ``session`` then
    showed."""

    def __init__(self, clock, before_upload=(), uploaded=True, session=None, leaves_track=0,
                 **kwargs):
        super().__init__(**kwargs)
        self.clock = clock
        self.before_upload = list(before_upload)
        self.uploaded = uploaded
        self.session = session
        self.leaves_track = leaves_track
        self.track_log = []

    def get_status(self):
        if self.posted is None:
            return super().get_status()
        self._poll += 1
        self.track_reads += 1
        now = self.clock[0]
        start = self.posted["start_time"]
        end = start + self.posted["points"][-1][0]
        if self.track_reads <= len(self.before_upload) or not self.uploaded:
            status = dict(self.before_upload[min(self.track_reads, len(self.before_upload)) - 1])
        elif self.leaves_track and now >= start + 5.0:
            self.leaves_track -= 1
            status = _acu(5000, "Stop")
        elif now < end:
            status = _acu(5000, "ProgramTrack", vaz=0.2 if now >= start else 0.0)
        else:
            status = _acu(9999, "ProgramTrack")
        if self.track_stopped:
            status = _stopped_by_a_move_to(status)
        phase = self.session.data.get("phase") if self.session is not None else None
        self.track_log.append((now, phase, status))
        return status


def _make_agent(tcs):
    a = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    a.log = _Log()
    a.acu_read = tcs
    a.azel_lock = _FakeLock()
    # The position read before the build: the dish at the slew target (the gate
    # itself reads the fake's status). The runner calls this off-reactor with the per-scan ``tcs``
    # (deferToThread -> maybeDeferred(fn, tcs)), so the stub must accept it.
    a._current_encoder_azel = lambda *a, **k: (ENC_AZ, ENC_EL, "broadcast")
    return a


@pytest.fixture
def sync_reactor(monkeypatch):
    """Run the @inlineCallbacks body synchronously, no real reactor: deferToThread
    runs the fn inline (maybeDeferred captures exceptions like the real one);
    dsleep is an already-fired no-op."""
    monkeypatch.setattr(
        _AGENT,
        "threads",
        types.SimpleNamespace(
            deferToThread=lambda fn, *a, **k: defer.maybeDeferred(fn, *a, **k)
        ),
    )
    monkeypatch.setattr(_AGENT, "dsleep", lambda *a, **k: defer.succeed(None))


def _run(d):
    out = []
    d.addBoth(out.append)
    assert out, "Deferred did not fire synchronously"
    res = out[0]
    if isinstance(res, Failure):
        res.raiseException()
    return res


@pytest.fixture
def clock(sync_reactor, monkeypatch):
    """A test clock for the runner: ``time.time`` reads it, and ``dsleep``
    advances it by the seconds the runner asks for, so each poll of the slew
    gate and of the completion wait moves time on as the agent's own sleeps
    would, and a scheduled start and end pass as the polls go. Returns the
    one-element list that holds the time."""
    now = [time.time()]
    monkeypatch.setattr(_AGENT, "time", types.SimpleNamespace(time=lambda: now[0], sleep=time.sleep))

    def advance(seconds):
        now[0] += seconds
        return defer.succeed(None)

    monkeypatch.setattr(_AGENT, "dsleep", advance)
    return now


@pytest.fixture(autouse=True)
def no_held_track_wait(request, monkeypatch):
    """Zero the wait for a held track to stop, so no test sleeps.

    After the move-to that stops a held track the agent polls the ACU status
    every HELD_TRACK_STOP_POLL_SEC for up to HELD_TRACK_STOP_WAIT_SEC, with
    time.sleep, since it runs off the reactor. With both at zero each wait is
    one status read, which is what the fakes here model: the read after an
    accepted move-to shows the axes out of ProgramTrack. A test that asks for
    ``held_track_clock`` keeps the agent's own values and runs the wait on a
    test clock instead."""
    if _AGENT is None or "held_track_clock" in request.fixturenames:
        return
    monkeypatch.setattr(_AGENT, "HELD_TRACK_STOP_WAIT_SEC", 0.0)
    monkeypatch.setattr(_AGENT, "HELD_TRACK_STOP_POLL_SEC", 0.0)


@pytest.fixture
def held_track_clock(monkeypatch):
    """A test clock for the wait for a held track to stop: the agent's
    ``time.time`` reads it and its ``time.sleep`` advances it, so the agent's
    own HELD_TRACK_STOP_WAIT_SEC and HELD_TRACK_STOP_POLL_SEC pace the polls
    while the test does not sleep. Returns the one-element list that holds the
    time."""
    now = [1.8e9]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(_AGENT, "time", types.SimpleNamespace(time=lambda: now[0], sleep=sleep))
    return now


def _dispatch(tcs, build_fn=_canned_build, session=None, agent=None, job="source_scan",
              scan_params=None, scheduled_t0_unix=None):
    """Drive the real runner once as ``job`` with ``tcs`` (and ``agent``, by
    default one built on it), ``scan_params`` (by default ``{"body": "x"}``) and
    ``scheduled_t0_unix``, and return its (ok, message)."""
    agent = _make_agent(tcs) if agent is None else agent
    session = _FakeSession(status="running") if session is None else session
    params = {"scan_params": {"body": "x"} if scan_params is None else scan_params,
              "scheduled_t0_unix": scheduled_t0_unix}
    return _run(
        agent._dispatch_scan_process(
            session, params, build_fn=build_fn, job=job, tcs=tcs
        )
    )


def test_dispatch_runner_happy_path(sync_reactor):
    """End-to-end through the REAL runner: slew arrives, POST 200, latch
    completes. Asserts move_to + scan_pattern were called and the post-slew
    re-floor left a near-now scan POSTable (drift below the cap)."""
    tcs = _FakeTCS()
    agent = _make_agent(tcs)
    session = _FakeSession(status="running")
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build, job="source_scan", tcs=tcs
        )
    )

    assert ok is True
    kinds = [c[0] for c in tcs.calls]
    assert "move_to" in kinds and "scan_pattern" in kinds
    # The POSTed body carries an absolute Unix start_time (the build's near-now
    # start, re-floored a no-op): the runner wires the re-floor and the small
    # drift is under MAX_REFLOOR_DRIFT_SEC, so the scan is NOT refused.
    # (The stale-refusal path, drift over the cap, is its own test below.)
    posted = next(c[1] for c in tcs.calls if c[0] == "scan_pattern")
    assert posted["start_time"] >= 1e9
    assert tcs.track_reads == 2  # this track's points on the stack, then the drain


def test_dispatch_runner_drives_constant_el(sync_reactor):
    """constant_el_scan now shares the runner: driving _dispatch_scan_process
    with build_constant_el_payload's job label exercises the SAME slew/gate/
    re-floor/POST/latch path. This is the net-new execution coverage the fold
    unlocks: the CES Process body had zero execution before. The canned build
    stands in for the real core (date-stable; the core itself is covered in
    test_constant_el_scan.py)."""
    tcs = _FakeTCS()
    agent = _make_agent(tcs)
    session = _FakeSession(status="running")
    params = {"scan_params": {"elevation": 45.0}, "scheduled_t0_unix": None}

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build,
            job="constant_el_scan", tcs=tcs
        )
    )

    assert ok is True
    kinds = [c[0] for c in tcs.calls]
    assert "move_to" in kinds and "scan_pattern" in kinds
    posted = next(c[1] for c in tcs.calls if c[0] == "scan_pattern")
    assert posted["start_time"] >= 1e9  # post-slew re-floor fired


def test_dispatch_runner_aborts_mid_slew(sync_reactor):
    """If the session goes 'stopping' during the slew, the runner routes through
    _safe_abort -> tcs.abort() and never POSTs."""
    session = _FakeSession(status="running")
    # move_to is between the pre-slew check and the slew loop, so tripping
    # 'stopping' there drives the runner into the slew-loop abort branch.
    tcs = _FakeTCS(on_move_to=lambda: setattr(session, "status", "stopping"))
    agent = _make_agent(tcs)
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build, job="pong_scan", tcs=tcs
        )
    )

    assert ok is True
    assert "abort" in msg.lower()
    kinds = [c[0] for c in tcs.calls]
    assert "abort" in kinds, "did not route through _safe_abort -> tcs.abort()"
    assert "scan_pattern" not in kinds, "must not POST after a mid-slew abort"


# ---------------------------------------------------------------------------
# Slew-arrival gate: /path is POSTed only once the move-to is done, judged as
# moveToCmd.isDone at TCS b7bbbf9 judges it (both axes in Preset, on their
# commanded positions, |velocity| < speedTol); the Go TCS refuses a /path sent
# before that as busy.
# ---------------------------------------------------------------------------


def test_slew_gate_waits_for_velocity_to_settle(sync_reactor):
    """The dish is on its commanded position from the first read, but the
    azimuth axis is still moving for two status reads. The gate must NOT POST
    until the velocity drops below tolerance, so /path is POSTed only after the
    third slew status read (the first two are the gate spinning on the
    still-moving axis)."""
    tcs = _FakeTCS(settle_after=2)
    agent = _make_agent(tcs)
    session = _FakeSession(status="running")
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build, job="daisy_scan", tcs=tcs
        )
    )

    assert ok is True
    kinds = [c[0] for c in tcs.calls]
    assert "scan_pattern" in kinds, "gate never released; /path not POSTed"
    # The first two status reads saw a moving azimuth axis (velocity 0.5); the
    # gate could only break on the third (velocity 0.0). A gate without the
    # velocity test would have POSTed after the first read.
    assert tcs.slew_reads == tcs.settle_after + 1


def test_slew_gate_times_out_if_axes_never_stop(sync_reactor, monkeypatch):
    """If the mount is position-arrived but its axes never stop, the gate must
    NOT POST: it fails the Process on the slew timeout instead of POSTing into
    a still-settling mount (which the Go TCS refuses as busy), after one
    /abort that stops the move before the lock goes. Drive the deadline into
    the past so the bounded wait expires deterministically."""
    monkeypatch.setattr(_AGENT, "SLEW_TIMEOUT_SEC", -1.0)

    # Position arrived, but azimuth velocity stays high forever.
    tcs = _FakeTCS(settle_after=10**9)
    agent = _make_agent(tcs)
    session = _FakeSession(status="running")
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build, job="source_scan", tcs=tcs
        )
    )

    assert ok is False
    assert "timed out" in msg.lower() and "/abort sent" in msg, msg
    kinds = [c[0] for c in tcs.calls]
    assert "scan_pattern" not in kinds, "must not POST while the axes are moving"
    assert tcs.calls.count(("abort",)) == 1


@pytest.mark.parametrize("abort_reply, outcome", [
    (None, "; /abort sent."),
    (NOTHING_TO_ABORT, "; /abort sent (nothing to abort)."),
    (SystemExit("read timed out"), "; /abort failed (no answer from the TCS"),
], ids=["abort-sent", "nothing-to-abort", "abort-failed"])
def test_slew_gate_timeout_short_of_the_target_sends_abort(sync_reactor, monkeypatch,
                                                           abort_reply, outcome):
    """A slew that never reaches the scan's start fails on the timeout, and as
    the move may still be running the runner first sends one /abort; the
    failure says whether the abort went out, with the abort's note when it has
    one ("nothing to abort": the move had already ended), and that the dish may
    still be moving when it did not."""
    monkeypatch.setattr(_AGENT, "SLEW_TIMEOUT_SEC", -1.0)
    # The dish stays 10 deg short of the target in azimuth.
    tcs = _FakeTCS(abort_reply=abort_reply, slew_read=lambda s: dict(
        s, AzimuthCurrentPosition=s["AzimuthCommandedPosition"] - 10.0))

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "slew to scan start timed out" in msg and outcome in msg, msg
    assert ("the dish may still be moving" in msg) is isinstance(abort_reply, BaseException), msg
    assert tcs.calls.count(("abort",)) == 1
    assert "scan_pattern" not in [c[0] for c in tcs.calls]


def test_slew_gate_waits_for_a_target_one_turn_away(sync_reactor):
    """The gate compares encoder azimuths as they are: with the dish stopped on
    a commanded -170 and the target at 190, the same sky azimuth one turn away,
    it keeps waiting, and POSTs only once the reads show 190. A comparison
    modulo 360 would read the dish as arrived and POST with the dish a full
    turn from the scan's start."""
    reads = iter([-170.0])  # the first gate poll

    def slew_read(status):
        az = next(reads, 190.0)
        tcs.calls.append(("position", az))
        return dict(status, AzimuthCurrentPosition=az, AzimuthCommandedPosition=az)

    tcs = _FakeTCS(settle_after=0, slew_read=slew_read)

    ok, msg = _dispatch(tcs, build_fn=_canned(encoder_az=190.0))

    assert ok is True, msg
    post = [c[0] for c in tcs.calls].index("scan_pattern")
    assert [c[1] for c in tcs.calls[:post] if c[0] == "position"] == [-170.0, 190.0]


def test_axes_stopped_requires_both_velocities_present_and_below_tol():
    """Unit-check the helper the gate uses: both axis velocities must be present
    AND below TCS_SPEED_TOL. A missing velocity is 'not known to be stopped'."""
    agent = _make_agent(_FakeTCS())
    tol = _AGENT.TCS_SPEED_TOL
    assert agent._axes_stopped(
        {"AzimuthCurrentVelocity": 0.0, "ElevationCurrentVelocity": 0.0})
    assert agent._axes_stopped(
        {"AzimuthCurrentVelocity": tol / 2, "ElevationCurrentVelocity": -tol / 2})
    # One axis still moving.
    assert not agent._axes_stopped(
        {"AzimuthCurrentVelocity": 0.5, "ElevationCurrentVelocity": 0.0})
    # Velocity absent (a status record missing the field).
    assert not agent._axes_stopped({"AzimuthCurrentVelocity": 0.0})
    assert not agent._axes_stopped({})


#: A pointing correction (deg) on both axes: what the Go TCS's Sky2Raw adds to
#: the sky target under a commissioned model, well over SLEW_ARRIVAL_TOL_DEG.
RAW_OFFSET = 0.3


def _raw_position_agent(tcs):
    """An agent whose position reads give the dish's raw encoder position, as
    the broadcast does: the fake's commanded position once a move-to is sent."""
    agent = _make_agent(tcs)
    agent._current_encoder_azel = lambda *_: (*(tcs.commanded() or (ENC_AZ, ENC_EL)),
                                              "broadcast")
    return agent


def test_slew_gate_releases_on_a_dish_at_its_pointing_corrected_target(clock):
    """Under a pointing model the Go TCS moves the dish to the raw position of
    the sky target, here RAW_OFFSET away on both axes. With both axes in Preset,
    on their commanded position and stopped, the move is done as the TCS judges
    it (moveToCmd.isDone at TCS b7bbbf9), so the gate posts the /path."""
    tcs = _FakeTCS(settle_after=0, raw_offset=(RAW_OFFSET, RAW_OFFSET))

    ok, msg = _dispatch(tcs, agent=_raw_position_agent(tcs))

    assert ok is True, msg
    assert "scan_pattern" in [c[0] for c in tcs.calls]


@pytest.mark.parametrize("slew_read, unmet", [
    (lambda s: dict(s, AzimuthCurrentPosition=s["AzimuthCommandedPosition"] + 0.2,
                    ElevationCurrentPosition=s["ElevationCommandedPosition"] + 0.2),
     "azimuth current 120.2000 is not on its commanded 120.0000"),
    (lambda s: dict(s, AzimuthMode="Stop", ElevationMode="Stop"),
     "azimuth mode is 'Stop', not Preset"),
    (lambda s: dict(s, AzimuthCurrentPosition=ENC_AZ + 360.0,
                    AzimuthCommandedPosition=ENC_AZ + 360.0),
     "azimuth commanded 480.0000 is not the target 120.0000 (stale preset, or a pointing "
     "correction over 0.5 deg)"),
], ids=["stopped-short", "modes-stop", "commanded-one-wrap-away"])
def test_slew_gate_times_out_on_a_move_not_done(sync_reactor, monkeypatch, slew_read, unmet):
    """A dish stopped 0.2 deg short of its commanded position, axes in Stop on
    the target, or a commanded position one wrap from the target (a stale
    preset) is not a finished move-to: the gate times out, sends one /abort,
    never POSTs, and names the condition that failed with the last reading."""
    monkeypatch.setattr(_AGENT, "SLEW_TIMEOUT_SEC", -1.0)
    tcs = _FakeTCS(settle_after=0, slew_read=slew_read)

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "slew to scan start timed out" in msg and f"last unmet: {unmet}" in msg, msg
    assert "'AzimuthCommandedPosition':" in msg, msg
    assert tcs.calls.count(("abort",)) == 1
    assert "scan_pattern" not in [c[0] for c in tcs.calls]


def test_preset_arrived_names_each_unmet_condition():
    """Unit-check the gate's test on single status reads."""
    agent = _make_agent(_FakeTCS())
    on_target = _acu(9999, "Preset", position=(ENC_AZ + RAW_OFFSET, ENC_EL + RAW_OFFSET))
    assert agent._preset_arrived(on_target, ENC_AZ, ENC_EL) == (True, "arrived")
    arrived, reason = agent._preset_arrived(_acu(9999, "Preset", vaz=0.5, position=(
        ENC_AZ, ENC_EL)), ENC_AZ, ENC_EL)
    assert (arrived, reason) == (False, "axes not stopped")
    arrived, reason = agent._preset_arrived(_acu(9999, "Preset"), ENC_AZ, ENC_EL)
    assert (arrived, reason) == (False, "azimuth current or commanded position missing")
    beyond = ENC_EL + _AGENT.POINTING_CORRECTION_MAX_DEG + 0.01
    arrived, reason = agent._preset_arrived(
        _acu(9999, "Preset", position=(ENC_AZ, beyond)), ENC_AZ, ENC_EL)
    assert not arrived and reason.startswith("elevation commanded"), reason


#: A preset left from an earlier move (deg off the new target on both axes):
#: inside POINTING_CORRECTION_MAX_DEG, so a read showing it passes the gate.
STALE_PRESET_OFFSET = 0.2


def test_slew_gate_sleeps_before_its_first_status_read(sync_reactor, monkeypatch):
    """The Go TCS answers the move-to before MoveTo sets the new preset, so a
    read at once can show an earlier preset near the target, stopped in Preset,
    and pass. The gate sleeps before every read: here reads taken before any
    sleep after the move-to show such a stale preset, and the first gate read
    must come after a sleep."""
    events = []

    def sleep(seconds):
        events.append("sleep")
        return defer.succeed(None)

    monkeypatch.setattr(_AGENT, "dsleep", sleep)

    def slew_read(status):
        events.append("read")
        after_move = events[events.index("move_to") + 1:]
        if "sleep" in after_move[:-1]:
            return status
        return dict(status, **{
            f"{axis}{kind}Position": status[f"{axis}CommandedPosition"] + STALE_PRESET_OFFSET
            for axis in ("Azimuth", "Elevation") for kind in ("Current", "Commanded")})

    tcs = _FakeTCS(settle_after=0, slew_read=slew_read,
                   on_move_to=lambda: events.append("move_to"))

    ok, msg = _dispatch(tcs)

    assert ok is True, msg
    after_move = events[events.index("move_to") + 1:]
    assert after_move[:2] == ["sleep", "read"], events


def test_slew_gate_keeps_polling_after_a_failed_status_read(sync_reactor):
    """A status read that fails once is logged and the gate polls on; the next
    read shows the move done and the /path is POSTed."""
    reads = []

    class _ReadFailsOnceTCS(_FakeTCS):
        def get_status(self):
            if self.posted is None and not reads:
                reads.append("failed")
                raise SystemExit("read timed out")
            return super().get_status()

    tcs = _ReadFailsOnceTCS(settle_after=0)
    agent = _make_agent(tcs)
    agent.log = _WarnLog()

    ok, msg = _dispatch(tcs, agent=agent)

    assert ok is True, msg
    assert "scan_pattern" in [c[0] for c in tcs.calls]
    assert any("status read during slew gate failed: read timed out" in w
               for w in agent.log.warnings), agent.log.warnings


# ---------------------------------------------------------------------------
# The runner reads the current encoder position OFF the reactor and
# hands the position helper the PER-SCAN ``tcs``, so the fallback status read uses
# the scan's own Session, not the shared ``self.acu_read`` (which the monitor
# owns). Proves the wiring: ``_current_encoder_azel`` is invoked with the per-scan
# client.
# ---------------------------------------------------------------------------


def test_runner_calls_current_encoder_azel_off_reactor_with_tcs(sync_reactor):
    """The runner passes the per-scan ``tcs`` to ``_current_encoder_azel``.

    A spy records its positional args; asserting the helper was called with the
    SAME per-scan client the runner was handed is the structural guarantee that the
    fallback status read can never touch the monitor's shared ``self.acu_read``
    Session."""
    tcs = _FakeTCS()
    agent = _make_agent(tcs)
    seen = []

    def _spy(*a):
        seen.append(a)
        return (ENC_AZ, ENC_EL, "broadcast")

    agent._current_encoder_azel = _spy
    session = _FakeSession(status="running")
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    ok, _ = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build, job="source_scan", tcs=tcs
        )
    )

    assert ok is True
    assert seen, "_current_encoder_azel was never called"
    # Every call received exactly the per-scan tcs as its single positional arg.
    assert all(a == (tcs,) for a in seen), (
        f"expected _current_encoder_azel(tcs); got calls {seen!r}")


def test_dispatch_runner_refuses_on_stale_refloor(sync_reactor):
    """If the post-slew re-floor advances start_time past
    MAX_REFLOOR_DRIFT_SEC the runner REFUSES to POST: the baked az/el track is
    stale (tracks the source's old sky position) and the re-floor only shifts WHEN
    the scan plays, not WHERE. A deep-past build start forces drift past the cap;
    assert ``ok is False``, the message names the staleness, and ``scan_pattern``
    was NEVER called. The happy-path test (drift ~ 0 -> POST) is the control."""
    tcs = _FakeTCS()
    agent = _make_agent(tcs)
    session = _FakeSession(status="running")
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    # start_time = now - 200 s: the re-floor bumps it to now+buffer, a drift of
    # ~ 200 + buffer s >> MAX_REFLOOR_DRIFT_SEC (30 s).
    stale_build = _canned(start_in=-200.0)

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=stale_build, job="source_scan", tcs=tcs
        )
    )

    assert ok is False
    low = msg.lower()
    assert "stale" in low or "re-floor" in low, f"unexpected refusal message: {msg!r}"
    kinds = [c[0] for c in tcs.calls]
    assert "scan_pattern" not in kinds, "must not POST a stale (drift > cap) trajectory"


class _AbortAtSettleTCS(_FakeTCS):
    """The first status read shows the move-to done, so the slew gate breaks,
    and flips ``session`` to 'stopping', landing an abort between the gate and
    the /path POST."""

    def __init__(self, session, **kwargs):
        super().__init__(**kwargs)
        self.session = session

    def get_status(self):
        self._poll += 1
        if self._poll == 1:
            self.session.status = "stopping"
        return _acu(9999, "Preset", position=(ENC_AZ, ENC_EL))


def test_dispatch_runner_aborts_in_slew_settle_window(sync_reactor):
    """Regression (HIGH): an abort landing AFTER the slew-arrival gate breaks but
    BEFORE the /path POST must not POST the scan. The gate's final velocity read is
    off-reactor, on which the session can flip to 'stopping'; without the re-check
    the runner would POST the cancelled scan (then the completion loop sends
    /abort, racing /path at the ACU). Assert /path is NEVER POSTed and the runner
    routes through _safe_abort."""
    session = _FakeSession(status="running")
    tcs = _AbortAtSettleTCS(session)
    agent = _make_agent(tcs)
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build, job="source_scan", tcs=tcs
        )
    )

    assert ok is True
    assert "abort" in msg.lower()
    kinds = [c[0] for c in tcs.calls]
    assert "scan_pattern" not in kinds, (
        "POSTed a scan that was aborted in the slew-settle -> POST window")
    assert "abort" in kinds, "did not route through _safe_abort -> tcs.abort()"


def test_abort_before_post_wins_over_a_stale_refloor(sync_reactor):
    """An abort landing in the slew-settle -> POST window returns the abort result
    even when the slew also consumed the scan lead: the abort check runs before
    the re-floor's drift refusal."""
    session = _FakeSession(status="running")
    tcs = _AbortAtSettleTCS(session)

    # A start 200 s in the past: the re-floor would refuse it as stale.
    ok, msg = _dispatch(tcs, build_fn=_canned(start_in=-200.0), session=session)

    assert ok is True, msg
    assert "abort" in msg.lower()
    kinds = [c[0] for c in tcs.calls]
    assert "scan_pattern" not in kinds
    assert "abort" in kinds, "did not route through _safe_abort -> tcs.abort()"


# ---------------------------------------------------------------------------
# /path POST 503 retry. aculib.post() returns a bare {} on HTTP 503 (status None
# via tcs_response_status); a healthy POST can 503 right after the slew settles
# (see agent.TCS_BUSY_RETRY_*). The runner retries a bounded number of times,
# re-flooring start_time each attempt; any other HTTP code fails immediately.
# ---------------------------------------------------------------------------


class _Reject400TCS(_FakeTCS):
    """Refuses every /path POST with HTTP 400."""

    def scan_pattern(self, payload):
        self.calls.append(("scan_pattern", dict(payload)))
        return _Resp(400)


class _Retry503TCS(_FakeTCS):
    """``scan_pattern`` returns the bare {} aculib yields on 503 for the first
    ``fail_times`` calls, then a 200 Response. Records the POSTed body each call
    so a test can assert the re-floored start_time."""

    def __init__(self, fail_times=1):
        super().__init__()
        self.fail_times = fail_times
        self.post_count = 0

    def scan_pattern(self, payload):
        self.post_count += 1
        if self.post_count <= self.fail_times:
            self.calls.append(("scan_pattern", dict(payload)))
            return {}  # the literal {} aculib.post() returns on a 503
        return super().scan_pattern(payload)


def test_path_post_retries_once_on_503_then_succeeds(sync_reactor):
    """A single 503 ({}) is retried, the second POST succeeds, and every POSTed
    start_time clears the Go TCS minimum lead."""
    tcs = _Retry503TCS(fail_times=1)

    before = time.time()
    ok, msg = _dispatch(tcs)

    assert ok is True
    posts = [c for c in tcs.calls if c[0] == "scan_pattern"]
    assert len(posts) == 2, f"expected exactly 2 POSTs (one retry), got {len(posts)}"
    # Every POSTed start_time clears the Go TCS minimum lead; the per-attempt
    # re-floor is pinned by the advancing-clock test below.
    for _, body in posts:
        assert body["start_time"] >= before + _AGENT.SCAN_DISPATCH_BUFFER_SEC


def test_path_post_retry_refloors_start_time_each_attempt(clock):
    """Each attempt re-floors start_time against a fresh now: the retried POST
    starts TCS_BUSY_RETRY_DELAY_SEC after the first."""
    tcs = _Retry503TCS(fail_times=1)

    ok, msg = _dispatch(tcs)

    assert ok is True, msg
    first, second = [c[1]["start_time"] for c in tcs.calls if c[0] == "scan_pattern"]
    assert second - first == pytest.approx(_AGENT.TCS_BUSY_RETRY_DELAY_SEC)


def test_path_post_gives_up_after_max_attempts_on_persistent_503(sync_reactor):
    """A 503 on every POST fails the scan after exactly TCS_BUSY_RETRY_MAX
    attempts."""
    tcs = _Retry503TCS(fail_times=10**9)  # never recovers

    ok, msg = _dispatch(tcs, job="pong_scan")

    assert ok is False
    assert "503" in msg
    posts = [c for c in tcs.calls if c[0] == "scan_pattern"]
    assert len(posts) == _AGENT.TCS_BUSY_RETRY_MAX, (
        f"expected exactly {_AGENT.TCS_BUSY_RETRY_MAX} POSTs, got {len(posts)}")


def test_path_post_abort_between_retries_stops_and_does_not_repost(sync_reactor):
    """An abort that lands between POST attempts sends /abort through _safe_abort
    and does not POST again: the first POST answers {} (503) and sets 'stopping'."""
    session = _FakeSession(status="running")

    class _AbortBetweenRetriesTCS(_Retry503TCS):
        def scan_pattern(self, payload):
            out = super().scan_pattern(payload)
            if self.post_count == 1:
                session.status = "stopping"
            return out

    tcs = _AbortBetweenRetriesTCS(fail_times=10**9)

    ok, msg = _dispatch(tcs, session=session, job="daisy_scan")

    assert ok is True
    assert "abort" in msg.lower()
    posts = [c for c in tcs.calls if c[0] == "scan_pattern"]
    assert len(posts) == 1, f"must not re-POST after an abort; got {len(posts)} POSTs"
    assert "abort" in [c[0] for c in tcs.calls], "did not route through _safe_abort"


def test_path_post_400_fails_immediately_without_retry(sync_reactor):
    """A bodiless 400 (from the OCS API or an intermediary) is final: the runner
    fails after a single POST, without retrying."""
    tcs = _Reject400TCS()

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "400" in msg
    posts = [c for c in tcs.calls if c[0] == "scan_pattern"]
    assert len(posts) == 1, f"a 400 must not be retried; got {len(posts)} POSTs"


# ---------------------------------------------------------------------------
# Transport failures. aculib.post() raises SystemExit on any requests error, and a
# read timeout can come after the Go TCS took the command, so the runner sends one
# /abort and fails the scan without sending the command again.
# ---------------------------------------------------------------------------


class _TransportFailTCS(_FakeTCS):
    """``scan_pattern`` raises ``error``, as aculib's ``post`` raises
    ``SystemExit`` on a ``requests`` error."""

    def __init__(self, error):
        super().__init__()
        self.error = error

    def scan_pattern(self, payload):
        self.calls.append(("scan_pattern", dict(payload)))
        raise self.error


@pytest.mark.parametrize("error", [SystemExit("read timed out"), Exception("connection reset")],
                         ids=["SystemExit", "Exception"])
def test_path_post_transport_failure_aborts_and_fails(sync_reactor, error):
    """A /path POST that fails in transport fails the scan after one /abort and
    no second POST, whether aculib's SystemExit or a plain Exception."""
    tcs = _TransportFailTCS(error)

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "transport" in msg and "scan not launched" in msg, msg
    assert tcs.calls.count(("abort",)) == 1
    posts = [c for c in tcs.calls if c[0] == "scan_pattern"]
    assert len(posts) == 1, f"a path the TCS may have queued was POSTed {len(posts)} times"


def test_path_post_transport_failure_reports_the_abort_note(sync_reactor):
    """When the /abort after a transport failure meets "nothing to abort" while
    the ACU still plays a program track it cannot stop (the reads carry no
    encoder position for the stopping move-to), the failure text gives
    _safe_abort's note in place of "scan not launched"."""

    class _HeldAfterTransportFailTCS(_TransportFailTCS):
        def abort(self):
            self.calls.append(("abort",))
            return NOTHING_TO_ABORT

        def get_status(self):
            # The move-to reads done, so the slew gate releases on the first read;
            # after the POST, 5000 free positions leave points on the stack.
            if "scan_pattern" not in [c[0] for c in self.calls]:
                return _acu(9999, "Preset", position=(ENC_AZ, ENC_EL))
            return _acu(5000, "ProgramTrack")

    tcs = _HeldAfterTransportFailTCS(SystemExit("read timed out"))

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "transport" in msg and ", but " in msg, msg
    assert "still playing a program track (4999 points on its stack)" in msg, msg
    assert "scan not launched" not in msg, msg
    assert tcs.calls.count(("abort",)) == 1
    assert len([c for c in tcs.calls if c[0] == "scan_pattern"]) == 1


def test_a_path_post_that_fails_after_the_path_start_says_the_scan_may_have_run(clock):
    """A /path POST whose reply is lost fails at the 30 s read timeout, by which time a
    dispatch-now path, re-floored to start SCAN_DISPATCH_BUFFER_SEC after the POST, has
    played for 20 s if the Go TCS took it; the /abort is sent after the start, so the
    failure text says the scan may have run and does not say it was not launched."""

    class _LateTransportFailTCS(_TransportFailTCS):
        def scan_pattern(self, payload):
            self.calls.append(("scan_pattern", dict(payload)))
            clock[0] += _AGENT.aculib.TCS_HTTP_TIMEOUT[1]  # requests gives up at the read timeout
            raise self.error

    tcs = _LateTransportFailTCS(SystemExit("read timed out"))

    ok, msg = _dispatch(tcs)

    [(_, posted)] = [c for c in tcs.calls if c[0] == "scan_pattern"]
    assert clock[0] - posted["start_time"] == pytest.approx(
        _AGENT.aculib.TCS_HTTP_TIMEOUT[1] - _AGENT.SCAN_DISPATCH_BUFFER_SEC, abs=1.0)
    assert ok is False
    assert ("/abort was sent, but had not completed before the path start, so the scan may have run; "
            "redispatch.") in msg, msg
    assert "scan not launched" not in msg, msg
    assert tcs.calls.count(("abort",)) == 1


def test_an_abort_that_completes_after_the_path_start_says_the_scan_may_have_run(clock):
    """A /path POST that fails 2 s before the path's start, whose /abort takes 5 s: whether
    the Go TCS stopped the path before it played is not known, so the text says the scan
    may have run and does not say it was not launched."""

    class _SlowAbortTCS(_TransportFailTCS):
        def scan_pattern(self, payload):
            self.calls.append(("scan_pattern", dict(payload)))
            clock[0] = payload["start_time"] - 2.0
            raise self.error

        def abort(self):
            clock[0] += 5.0
            return super().abort()

    tcs = _SlowAbortTCS(Exception("connection reset"))

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "but had not completed before the path start, so the scan may have run" in msg, msg
    assert "scan not launched" not in msg, msg


def test_a_path_post_that_fails_after_the_path_start_with_no_abort_says_the_dish_may_still_be_moving(clock):
    """As above the first of the two, but the /abort gets no answer either: the text keeps the
    abort's note and says the dish may still be moving, not that the scan may have run."""

    class _LateTransportFailTCS(_TransportFailTCS):
        def scan_pattern(self, payload):
            self.calls.append(("scan_pattern", dict(payload)))
            clock[0] += _AGENT.aculib.TCS_HTTP_TIMEOUT[1]  # requests gives up at the read timeout
            raise self.error

    tcs = _LateTransportFailTCS(SystemExit("read timed out"))
    tcs.abort_reply = SystemExit("read timed out")

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "/abort was sent, but no answer from the TCS" in msg, msg
    assert msg.endswith("; the dish may still be moving; redispatch."), msg
    assert "the scan may have run" not in msg, msg


def test_slew_move_to_transport_failure_aborts_and_fails(sync_reactor):
    """A slew move-to that fails in transport, which the Go TCS may have taken,
    fails the scan after one /abort and never POSTs /path."""

    class _SlewTransportFailTCS(_FakeTCS):
        def move_to(self, az, el):
            self.calls.append(("move_to", az, el))
            raise SystemExit("read timed out")

    tcs = _SlewTransportFailTCS()

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "slew move-to failed in transport" in msg and "scan not launched" in msg, msg
    assert tcs.calls.count(("abort",)) == 1
    assert "scan_pattern" not in [c[0] for c in tcs.calls]


class _CommandFailsTCS(_FakeTCS):
    """``command`` (``move_to`` or ``scan_pattern``) fails with ``failure``,
    raised when it is an exception (aculib's ``post`` raises ``SystemExit`` on a
    ``requests`` error) and returned as the answer otherwise; the other command
    is the fake's."""

    def __init__(self, command, failure, **kwargs):
        super().__init__(**kwargs)
        self.command = command
        self.failure = failure

    def _fail(self, call):
        self.calls.append(call)
        if isinstance(self.failure, BaseException):
            raise self.failure
        return self.failure

    def move_to(self, az, el):
        if self.command != "move_to":
            return super().move_to(az, el)
        return self._fail(("move_to", az, el))

    def scan_pattern(self, payload):
        if self.command != "scan_pattern":
            return super().scan_pattern(payload)
        return self._fail(("scan_pattern", dict(payload)))


@pytest.mark.parametrize("command, failure, failed, queued", [
    ("move_to", SystemExit("read timed out"), "slew move-to failed in transport (read timed out)",
     "move"),
    ("move_to", _Resp(500, text="Internal Server Error", body=None),
     "slew move-to failed at the OCS API (OCS API error HTTP 500 (the TCS may or may not have taken "
     "the command))", "move"),
    ("scan_pattern", SystemExit("read timed out"), "/path POST failed in transport (read timed out)",
     "path"),
    ("scan_pattern", _Resp(500, text="Internal Server Error", body=None),
     "/path POST failed at the OCS API (OCS API error HTTP 500 (the TCS may or may not have taken "
     "the command))", "path"),
], ids=["move-to-transport", "move-to-ocs-api-error", "path-transport", "path-ocs-api-error"])
def test_a_command_failure_reports_the_abort_note_when_the_abort_is_taken(sync_reactor, command,
                                                                          failure, failed, queued):
    """A slew move-to or /path POST that fails in transport or at the OCS API
    (HTTP 500), either of which the Go TCS may have taken, fails the scan after
    one /abort; when the abort is taken with a note ("nothing to abort": the
    command never reached the Go TCS or had already ended) the failure carries
    that note after "scan not launched", so the operator sees what the abort
    found."""
    tcs = _CommandFailsTCS(command, failure, abort_reply=NOTHING_TO_ABORT)

    ok, msg = _dispatch(tcs)

    assert (ok, msg) == (False, f"source_scan: {failed}; the Go TCS may have queued the {queued}, so "
                                f"/abort was sent; scan not launched (nothing to abort); redispatch.")
    assert tcs.calls.count(("abort",)) == 1
    assert len([c for c in tcs.calls if c[0] == "scan_pattern"]) == (command == "scan_pattern")


@pytest.mark.parametrize("command, failure", [
    ("move_to", SystemExit("read timed out")),
    ("move_to", _Resp(500, text="Internal Server Error", body=None)),
    ("scan_pattern", SystemExit("read timed out")),
    ("scan_pattern", _Resp(500, text="Internal Server Error", body=None)),
], ids=["move-to-transport", "move-to-ocs-api-error", "path-transport", "path-ocs-api-error"])
def test_a_command_failure_whose_abort_fails_says_the_dish_may_still_be_moving(
        sync_reactor, command, failure):
    """When the /abort sent after a failed slew move-to or /path POST gets no
    answer either, the failure carries the abort's note and says the dish may
    still be moving, since neither command is known not to have been taken."""
    tcs = _CommandFailsTCS(command, failure, abort_reply=SystemExit("read timed out"))

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "/abort was sent, but no answer from the TCS" in msg, msg
    assert msg.endswith("; the dish may still be moving; redispatch."), msg
    assert tcs.calls.count(("abort",)) == 1


# ---------------------------------------------------------------------------
# Truncated scans. The Go TCS can end a track after its HTTP 200 (a point refused
# at upload), draining the stack before the scheduled end: a drain more than
# SCAN_EARLY_DRAIN_TOL_SEC early fails the scan after one /abort. The wall-clock
# backstop returns success flagged unconfirmed, or a failure when no read ever
# showed this track's points.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("abort_reply, outcome", [
    (None, "; /abort sent. Redispatch."),
    (NOTHING_TO_ABORT, "; /abort sent (nothing to abort). Redispatch."),
    (SystemExit("read timed out"), "; /abort failed (no answer from the TCS"),
], ids=["abort-sent", "nothing-to-abort", "abort-failed"])
def test_early_drain_reports_truncated_scan(sync_reactor, abort_reply, outcome):
    """A stack drained about an hour before the scheduled end fails the scan as
    truncated, after one /abort, since the dish may still be playing what is
    left of the track; the failure says whether the abort went out, with the
    abort's note when it has one ("nothing to abort": the Go TCS had already
    ended the track), and that the dish may still be moving when it did not."""
    tcs = _FakeTCS(abort_reply=abort_reply)

    ok, msg = _dispatch(tcs, build_fn=_canned(span=3600.0))

    assert ok is False
    assert "scan truncated" in msg and outcome in msg, msg
    assert ("the dish may still be moving" in msg) is isinstance(abort_reply, BaseException), msg
    assert tcs.calls.count(("abort",)) == 1


def test_drain_within_tolerance_reports_success(sync_reactor):
    """A drain within SCAN_EARLY_DRAIN_TOL_SEC of the scheduled end is a
    completion: the canned scan ends ~11 s out (1 s of points after the 10 s
    floor), inside the 30 s tolerance."""
    tcs = _FakeTCS()  # running-then-drained; near-now scheduled end

    ok, msg = _dispatch(tcs)

    assert ok is True
    assert "drained at scheduled end" in msg.lower()


class _DrainAtTCS(_FakeTCS):
    """Drains ``before_end`` s before the POSTed scan's scheduled end: the drained
    poll moves the test clock there."""

    def __init__(self, clock, before_end):
        super().__init__()
        self.clock = clock
        self.before_end = before_end
        self.end = None

    def scan_pattern(self, payload):
        self.end = payload["start_time"] + payload["points"][-1][0]
        return super().scan_pattern(payload)

    def get_status(self):
        status = super().get_status()
        if self.end is not None and status["QtyOfFreeProgramTrackStackPositions"] == 9999:
            self.clock[0] = self.end - self.before_end
        return status


@pytest.mark.parametrize("margin, ok_expected", [(-5.0, True), (5.0, False)])
def test_early_drain_tolerance_is_the_edge(clock, margin, ok_expected):
    """A drain 5 s inside SCAN_EARLY_DRAIN_TOL_SEC of the scheduled end is a
    completion, and one 5 s outside it a truncation."""
    tcs = _DrainAtTCS(clock, _AGENT.SCAN_EARLY_DRAIN_TOL_SEC + margin)

    ok, msg = _dispatch(tcs, build_fn=_canned(span=600.0))

    assert ok is ok_expected, msg
    assert ("scan truncated" in msg) is not ok_expected, msg


def test_backstop_reports_unconfirmed_completion(sync_reactor, monkeypatch):
    """With no drained signal, the time backstop ends the scan as a success
    flagged unconfirmed; the deadline is put in the past so it trips on the first
    completion poll."""
    # The canned scan ends about 11 s out, so any settle below -11 s puts the
    # deadline in the past; -60 s does so with room.
    monkeypatch.setattr(_AGENT, "SCAN_SETTLE_SEC", -60.0)
    # This track's points sit on the stack in ProgramTrack (the latch arms) but
    # never drain, so only the backstop can end the wait.
    tcs = _FakeTCS(track=[_acu(5000, "ProgramTrack")])

    ok, msg = _dispatch(tcs)

    assert ok is True
    assert "unconfirmed" in msg.lower()


@pytest.mark.parametrize("cleared", [9999, 10000])
def test_backstop_fails_a_track_never_uploaded(sync_reactor, monkeypatch, cleared):
    """A stack that never shows this track's points from the POST to the
    deadline, read as a cleared stack with both axes in Stop (9999 where the ACU
    keeps one point through a clear, as its simulator does, else 10000), means
    the Go TCS never uploaded or never started the track: the backstop fails the
    scan rather than report it done."""
    monkeypatch.setattr(_AGENT, "SCAN_SETTLE_SEC", -60.0)
    tcs = _FakeTCS(track=[_acu(cleared, "Stop")])

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "no status read showed its points on the ACU program-track stack" in msg, msg


def test_a_defect_in_the_completion_decision_is_not_read_as_a_failed_status_read(
        clock, monkeypatch):
    """Only the status read itself is trapped in the completion wait: an error
    raised while the runner decides on a read it got (here the latch) leaves the
    Process, rather than being logged as a failed read every poll until the time
    backstop reports the scan done."""
    def broken_update(self, **kwargs):
        raise KeyError("free")

    monkeypatch.setattr(_AGENT.ScanCompletionLatch, "update", broken_update)

    with pytest.raises(KeyError):
        _dispatch(_FakeTCS())


# ---------------------------------------------------------------------------
# What the completion wait counts as this track: a read with points on the stack
# and an axis in ProgramTrack. Other reads after the POST can show a cleared stack
# in ProgramTrack (startPattern at TCS dd7151b set the mode before its upload
# landed; from 884aede it waits for the first batch) or an earlier track's points
# in Stop or Preset. Two reads in a row with this track's points and neither axis
# in ProgramTrack mean the ACU left it. These tests run on the test clock, so the
# scheduled start and end pass as the polls go.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("before_upload, after_tolerance", [
    ([_acu(9999, "Preset"), _acu(9999, "ProgramTrack")], False),
    ([_acu(9999, "Preset"), _acu(10000, "ProgramTrack")], True),
    ([_acu(9999, "Preset"), _acu(9999, "Stop")], False),
    ([_acu(6000, "Preset"), _acu(6000, "Stop")], True),
    ([_acu(6000, "Preset")], True),
], ids=["cleared-9999-in-program-track", "cleared-10000-in-program-track",
        "never-in-program-track", "old-points-never-in-program-track", "old-points-in-preset"])
def test_a_path_whose_points_never_reach_the_stack_fails_as_never_uploaded(clock, before_upload,
                                                                           after_tolerance):
    """A path the Go TCS accepted whose points never show on the stack while an
    axis is in ProgramTrack fails as never uploaded or never started, not as
    truncated and not as done, after one /abort in case the Go TCS still holds
    the command: a cleared stack in ProgramTrack to the end (the upload never
    landed), or the axes never in ProgramTrack (the track never started), the
    stack cleared or holding an earlier track's points.

    It fails as soon as the wait can tell: a stack that reads drained (9999)
    reads as done once the scheduled start has passed, so it fails then, and
    one that never does (10000 after a clear, or an earlier track's points in
    Stop or Preset) fails SCAN_EARLY_DRAIN_TOL_SEC after the start, not at the
    deadline the scan's length sets, which would hold the telescope for the
    length of a scan that is not running."""
    tcs = _TimedTCS(clock, before_upload=before_upload, uploaded=False)

    ok, msg = _dispatch(tcs, build_fn=_canned(span=120.0))

    assert (ok, msg) == (False, "source_scan: the Go TCS accepted the path but no status read "
                                "showed its points on the ACU program-track stack (never uploaded, "
                                "never started, or played while the reads failed; see the Go TCS "
                                "log); /abort sent. Redispatch.")
    assert tcs.calls.count(("abort",)) == 1
    fails_at = tcs.posted["start_time"] + (_AGENT.SCAN_EARLY_DRAIN_TOL_SEC if after_tolerance else 0.0)
    assert fails_at <= clock[0] < fails_at + 2 * _AGENT.SCAN_COMPLETION_POLL_SEC


@pytest.mark.parametrize("before_upload", [[_acu(10000, "ProgramTrack")], [_acu(6000, "Preset")]],
                         ids=["cleared-10000-in-program-track", "old-points-in-preset"])
def test_a_short_path_whose_points_never_reach_the_stack_fails_after_an_abort(clock, before_upload):
    """A path whose deadline (its end plus SCAN_SETTLE_SEC) comes before
    SCAN_EARLY_DRAIN_TOL_SEC after its start, here a 5 s path, meets the
    deadline first. Its points never reached the stack either and the Go TCS
    may still hold the command, so it too fails as never uploaded after one
    /abort, as a longer path does at the tolerance: no never-uploaded failure
    releases the telescope without one."""
    tcs = _TimedTCS(clock, before_upload=before_upload, uploaded=False)

    ok, msg = _dispatch(tcs, build_fn=_canned(span=5.0))

    assert (ok, msg) == (False, "source_scan: the Go TCS accepted the path but no status read "
                                "showed its points on the ACU program-track stack (never uploaded, "
                                "never started, or played while the reads failed; see the Go TCS "
                                "log); /abort sent. Redispatch.")
    assert tcs.calls.count(("abort",)) == 1


@pytest.mark.parametrize("before_upload", [
    [_acu(9999, "ProgramTrack")],
    [_acu(6000, "Stop"), _acu(9999, "ProgramTrack")],
], ids=["cleared-in-program-track", "old-points-in-stop"])
def test_reads_before_the_upload_lands_do_not_end_a_healthy_scan(clock, before_upload):
    """A first read after the POST that shows the cleared stack in ProgramTrack
    with the axes stopped, or an earlier track's points in Stop, is not this
    track: the scan runs on once the new points fill the stack and completes
    when they drain at the scheduled end, with no truncation and no /abort."""
    tcs = _TimedTCS(clock, before_upload=before_upload)

    ok, msg = _dispatch(tcs, build_fn=_canned(span=120.0))

    assert ok is True, msg
    assert msg == "scan complete (stack drained at scheduled end)"
    assert ("abort",) not in tcs.calls


def test_a_track_the_acu_leaves_with_points_unplayed_fails_the_scan(clock):
    """Once this track has been on the stack and the start has passed, two
    reads in a row with its points left and neither axis in ProgramTrack mean
    the ACU left the track (a fault, or a stop sent from elsewhere): the scan
    fails as interrupted at the second such read, after one /abort, naming the
    points left, rather than wait out the scan's length for a drain that never
    comes and end it at the backstop as if it had run."""
    tcs = _TimedTCS(clock, leaves_track=1000)  # out of the track at every read from then on

    ok, msg = _dispatch(tcs, build_fn=_canned(span=120.0))

    assert (ok, msg) == (False, "source_scan: the ACU left program track with 4999 points "
                                "unplayed; scan interrupted (/abort sent). Redispatch.")
    assert tcs.calls.count(("abort",)) == 1
    assert 1000 - tcs.leaves_track == 2  # it failed at the second read out of the track
    left_at = tcs.posted["start_time"] + 5.0
    assert left_at <= clock[0] < left_at + 2 * _AGENT.SCAN_COMPLETION_POLL_SEC


def test_one_read_out_of_program_track_does_not_end_the_scan(clock):
    """A single read with this track's points and both axes out of ProgramTrack,
    followed by ProgramTrack again, is a mode caught changing, not the ACU
    leaving the track: the scan runs on and completes when the stack drains at
    the scheduled end, with no /abort."""
    tcs = _TimedTCS(clock, leaves_track=1)

    ok, msg = _dispatch(tcs, build_fn=_canned(span=120.0))

    assert (ok, msg) == (True, "scan complete (stack drained at scheduled end)")
    assert ("abort",) not in tcs.calls
    assert tcs.leaves_track == 0  # the read out of the track did happen


def test_a_stop_that_meets_a_held_track_stops_it_with_a_move_to(sync_reactor):
    """A mid-scan stop whose /abort is answered "nothing to abort" while the ACU
    still plays the track (points on its stack with the axes in ProgramTrack:
    the Go TCS let go of a track the dish is playing) stops it with one move-to
    to the encoder position the same status read gives, the one command the
    OCS API relays that stops the ACU. The stop counts once a later read shows
    the axes out of ProgramTrack (here the next one, in Preset after the
    move-to), and the scan ends aborted, its message naming the stopped track."""
    session = _FakeSession(status="running")
    tcs = _FakeTCS(on_path_accepted=lambda: setattr(session, "status", "stopping"),
                   track=[HELD], abort_reply=NOTHING_TO_ABORT)

    ok, msg = _dispatch(tcs, session=session)

    assert (ok, msg) == (True, "source_scan aborted; /abort sent (nothing to abort; held program "
                               "track (4999 points) stopped via a move-to at the encoder position "
                               "read (az=123.400, el=56.700)).")
    # The slew's move-to, then the one that stops the held track.
    moves = [c for c in tcs.calls if c[0] == "move_to"]
    assert moves == [("move_to", ENC_AZ, ENC_EL), ("move_to", *HELD_AT)]


def test_abort_fails_while_the_acu_still_holds_a_program_track(sync_reactor):
    """A mid-scan stop whose /abort is answered "nothing to abort" while the ACU
    still plays a program track it cannot stop, the status read carrying no
    encoder position to send the stopping move-to to, fails the scan, naming
    the track, and sends no move-to after the slew's."""
    session = _FakeSession(status="running")
    tcs = _FakeTCS(on_path_accepted=lambda: setattr(session, "status", "stopping"),
                   track=[_acu(5000, "ProgramTrack")],
                   abort_reply=NOTHING_TO_ABORT)

    ok, msg = _dispatch(tcs, session=session)

    assert ok is False
    assert "stop requested, but /abort failed" in msg, msg
    assert ("still playing a program track (4999 points on its stack), and its position is "
            "not in the status, so it was not stopped") in msg, msg
    assert [c for c in tcs.calls if c[0] == "move_to"] == [("move_to", ENC_AZ, ENC_EL)]


def test_a_stop_whose_abort_cannot_read_the_acu_fails_the_scan(sync_reactor):
    """A mid-scan stop whose /abort is answered "nothing to abort" while the ACU
    status cannot be read, on the first read or the one retry, fails the scan,
    saying the dish may still be moving: a flaky ACU read is the state in which
    the Go TCS lets go of a track the dish plays on, so "nothing to abort" alone
    does not show the dish stopped. No move-to is sent after the slew's."""
    session = _FakeSession(status="running")
    tcs = _FakeTCS(on_path_accepted=lambda: setattr(session, "status", "stopping"),
                   track=[SystemExit("connection refused")], abort_reply=NOTHING_TO_ABORT)

    ok, msg = _dispatch(tcs, session=session)

    assert (ok, msg) == (False, "source_scan: stop requested, but /abort failed (nothing to abort; "
                                "the ACU status could not be read, so whether a program track still "
                                "plays is unknown); the dish may still be moving.")
    assert tcs.track_reads == 2
    assert [c for c in tcs.calls if c[0] == "move_to"] == [("move_to", ENC_AZ, ENC_EL)]


@pytest.mark.parametrize("site, aborted", [
    ("slew", "source_scan aborted during slew; /abort sent (nothing to abort)."),
    ("before_post", "source_scan aborted before /path POST; /abort sent (nothing to abort)."),
    ("wait", "source_scan aborted; /abort sent (nothing to abort; axes not in program track, "
             "5999 stale points on the stack)."),
], ids=["slew", "before-post", "wait"])
def test_a_stop_whose_abort_finds_the_dish_already_stopped_succeeds(sync_reactor, site, aborted):
    """A stop whose /abort is answered "nothing to abort" while the ACU plays no
    track is a success at each of the runner's three stop checks, its message
    carrying the abort's note: another /abort, say the abort task's, got there
    first. During the wait the reads show both axes in Stop with points left on
    the stack, which the Go TCS's /abort leaves (Telescope.Stop at TCS b7bbbf9);
    no move-to is sent to stop them."""
    session = _FakeSession(status="running")
    kwargs = _stop_at(site, session)
    if site == "wait":
        kwargs["track"] = [_acu(4000, "Stop")]
    tcs = _FakeTCS(abort_reply=NOTHING_TO_ABORT,
                   **kwargs)

    ok, msg = _dispatch(tcs, session=session)

    assert (ok, msg) == (True, aborted)
    assert [c for c in tcs.calls if c[0] == "move_to"] == [("move_to", ENC_AZ, ENC_EL)]


def _stop_at(site, session):
    """The fake's keyword arguments that flip ``session`` to 'stopping' at one of
    the runner's three stop checks: during the slew (once the move-to is sent),
    before the /path POST (on the slew gate's status read, which then releases
    the gate) or during the wait (once the POST is accepted)."""
    def stop():
        session.status = "stopping"

    return {"slew": {"on_move_to": stop},
            "before_post": {"on_status_read": stop, "settle_after": 0},
            "wait": {"on_path_accepted": stop}}[site]


@pytest.mark.parametrize("site, handler", [
    ("slew", "stop requested during slew, but /abort failed ("),
    ("before_post", "stop requested before /path POST, but /abort failed ("),
    ("wait", "stop requested, but /abort failed ("),
], ids=["slew", "before-post", "wait"])
@pytest.mark.parametrize("abort_reply, note", [
    (SystemExit("read timed out"), "no answer from the TCS (the abort may have taken effect); see log"),
    # Go's refusal of an /abort with a body (checkNoBody at TCS b7bbbf9) stands in
    # for any refusal the OCS API relays.
    ({"status": "error", "message": "command takes no parameters"},
     "refused: command takes no parameters"),
    ({"detail": "Not Found"}, "unexpected reply: {'detail': 'Not Found'}"),
], ids=["no-answer", "refused", "unexpected-reply"])
def test_a_stop_whose_abort_fails_fails_the_scan(sync_reactor, site, handler, abort_reply, note):
    """A stop at any of the runner's three stop checks whose /abort is not taken
    (no answer, a relayed refusal, or a reply that is neither ok nor a refusal)
    fails the scan with the abort's note and says the dish may still be moving,
    rather than report it aborted while the move or the track may run on."""
    session = _FakeSession(status="running")
    tcs = _FakeTCS(abort_reply=abort_reply, **_stop_at(site, session))

    ok, msg = _dispatch(tcs, session=session)

    assert ok is False, msg
    assert handler + note in msg and "the dish may still be moving" in msg, msg
    assert tcs.calls.count(("abort",)) == 1


# ---------------------------------------------------------------------------
# A Go TCS refusal relayed by the OCS API, which answers HTTP 200 with Go's JSON
# body whatever Go sent (api/app/telescope.py at OCS c515a51). The runner retries
# Go's busy, as it does aculib's {} for a 503, and fails at once on any other
# refusal; go_to, az_scan, fromfile_scan and abort fail on it with its message.
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway():
    """The ``mock_tcs`` module: the OCS-API-shaped mock and Go's messages."""
    pytest.importorskip("fastapi", reason="mock_tcs needs FastAPI")
    from pcs.agents.acu_interface.tests import mock_tcs

    return mock_tcs


def _ocs_api_client(gateway, app):
    """A real aculib client on the OCS API route whose requests ``app`` serves."""
    client = _AGENT.aculib.observatory_control_system(
        url="http://tcs.test", log=logging.getLogger("test_dispatch_runner"),
        verify_cert=False)
    client.session.mount("http://tcs.test", gateway.AppAdapter(app))
    return client


class _GatewayTCS(_FakeTCS):
    """Sends ``move_to`` and ``/path`` through a real aculib client that
    ``mock_tcs``'s app serves, relaying each queued Go TCS refusal as the OCS API
    does: HTTP 200 and Go's body (or, for ``OCS_API_ERROR``, the API's own HTTP
    500). Status reads and ``/abort`` stay the fake's."""

    def __init__(self, gateway, path=(), move_to=(), **kwargs):
        super().__init__(**kwargs)
        self.app = gateway.create_mock_tcs()
        self.app.state.recorder.refusals["path"].extend(path)
        self.app.state.recorder.refusals["move-to"].extend(move_to)
        self.client = _ocs_api_client(gateway, self.app)

    def move_to(self, az, el):
        self.calls.append(("move_to", az, el))
        return self.client.move_to(az, el)

    def scan_pattern(self, payload):
        self.calls.append(("scan_pattern", dict(payload)))
        reply = self.client.scan_pattern(payload)
        if _AGENT.tcs_response_status(reply) == 200:
            self._accept(payload)
        return reply


def test_path_post_retries_a_busy_relayed_as_200_then_succeeds(sync_reactor, gateway):
    """Go's busy, relayed as HTTP 200 with the message 'busy', is retried and the
    second POST is accepted."""
    tcs = _GatewayTCS(gateway, path=[gateway.GO_BUSY])

    ok, msg = _dispatch(tcs)

    assert ok is True, msg
    assert len(tcs.app.state.recorder.path_bodies) == 2


def test_path_post_fails_at_once_on_a_refusal_relayed_as_200(sync_reactor, gateway):
    """Any other refusal relayed as HTTP 200 fails after a single POST, with the
    TCS's message in the failure text."""
    tcs = _GatewayTCS(gateway, path=[gateway.GO_START_TOO_SOON])

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert gateway.GO_START_TOO_SOON in msg
    assert len(tcs.app.state.recorder.path_bodies) == 1


def test_slew_retries_a_move_to_busy_relayed_as_200_then_succeeds(sync_reactor, gateway):
    """A move-to refused as busy is retried, as a /path POST is (pcs can see a
    scan's stack drained up to a status tick before the Go TCS retires that
    command), and the scan runs once the second move-to is accepted."""
    tcs = _GatewayTCS(gateway, move_to=[gateway.GO_BUSY])

    ok, msg = _dispatch(tcs)

    assert ok is True, msg
    assert len(tcs.app.state.recorder.move_to_bodies) == 2
    assert len(tcs.app.state.recorder.path_bodies) == 1


def test_slew_fails_at_once_on_a_move_to_refusal_relayed_as_200(sync_reactor, gateway):
    """Any other move-to refusal relayed as HTTP 200 fails the scan after one
    move-to, naming the refusal, before any /path POST."""
    tcs = _GatewayTCS(gateway, move_to=[gateway.GO_AZIMUTH_OUT_OF_RANGE])

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert "move-to to scan start refused" in msg and gateway.GO_AZIMUTH_OUT_OF_RANGE in msg, msg
    assert len(tcs.app.state.recorder.move_to_bodies) == 1
    assert tcs.app.state.recorder.path_bodies == []


def test_slew_gives_up_after_max_move_to_attempts_on_persistent_busy(sync_reactor, gateway):
    """A move-to refused as busy on every attempt fails the scan after exactly
    TCS_BUSY_RETRY_MAX attempts, before any /path POST."""
    tcs = _GatewayTCS(gateway, move_to=[gateway.GO_BUSY] * _AGENT.TCS_BUSY_RETRY_MAX)

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert f"stayed busy for {_AGENT.TCS_BUSY_RETRY_MAX} move-to attempts" in msg, msg
    assert len(tcs.app.state.recorder.move_to_bodies) == _AGENT.TCS_BUSY_RETRY_MAX
    assert tcs.app.state.recorder.path_bodies == []


@pytest.mark.parametrize("command, failure, paths_posted", [
    ("move_to", "slew move-to failed at the OCS API (OCS API error HTTP 500", 0),
    ("path", "/path POST failed at the OCS API (OCS API error HTTP 500", 1),
], ids=["move-to", "path"])
def test_an_ocs_api_error_is_handled_as_a_transport_failure(sync_reactor, gateway, command,
                                                            failure, paths_posted):
    """An OCS API error (HTTP 500 with FastAPI's plain-text body: the API's hop to
    the Go TCS raised) can come after the Go TCS took the command, so the runner
    sends one /abort and fails the scan without sending the command again,
    saying the TCS may or may not have taken it."""
    tcs = _GatewayTCS(gateway, **{command: [gateway.OCS_API_ERROR]})

    ok, msg = _dispatch(tcs)

    recorder = tcs.app.state.recorder
    assert ok is False
    assert failure in msg and "may or may not have taken the command" in msg, msg
    assert "scan not launched; redispatch." in msg, msg
    assert tcs.calls.count(("abort",)) == 1
    assert len(recorder.move_to_bodies) == 1
    assert len(recorder.path_bodies) == paths_posted


@pytest.mark.parametrize("command, failure", [
    ("move_to", "slew move-to failed at the OCS API"),
    ("scan_pattern", "/path POST failed at the OCS API"),
], ids=["move-to", "path"])
def test_typed_scan_treats_a_foreign_200_as_an_ocs_api_failure(sync_reactor, command, failure):
    """A 200 that is not the Go TCS's reply (a proxy's login page) reads as 502,
    an intermediary failure after which the command may have reached the TCS:
    the scan fails with the OCS-API wording quoting the reply, after one /abort,
    and the command is not sent again."""
    page = _Resp(200, text="<html>login</html>", body=None)
    tcs = _CommandFailsTCS(command, page)

    ok, msg = _dispatch(tcs, job="pong_scan")

    assert ok is False
    assert (f"{failure} (the reply was not the Go TCS's: '<html>login</html>')"
            in msg), msg
    assert tcs.calls.count(("abort",)) == 1
    assert [c[0] for c in tcs.calls].count(command) == 1


def test_a_bodiless_4xx_to_the_path_post_is_a_refusal_without_an_abort(sync_reactor):
    """A 4xx with no message never came from the Go TCS, whose refusals the OCS
    API relays as HTTP 200 with one: nginx's 413 for a body over its limit
    stands for them. The scan fails after one /path POST, without an /abort,
    saying the POST was refused and naming the status."""
    page = _Resp(413, text="<html>413 Request Entity Too Large</html>", body=None)
    tcs = _CommandFailsTCS("scan_pattern", page)

    ok, msg = _dispatch(tcs)

    assert ok is False
    assert msg.startswith("source_scan: /path POST refused (HTTP 413"), msg
    assert msg.endswith("); scan not launched."), msg
    assert ("abort",) not in tcs.calls
    assert [c[0] for c in tcs.calls].count("scan_pattern") == 1


def _with_acu_conf(agent):
    """Give ``agent`` the acu_conf the legacy tasks read to build their client."""
    agent.acu_conf = {
        "base_url": "http://tcs.invalid",
        "certs": {"server_cert": "", "client_cert": "", "client_key": "",
                  "verify": False},
    }
    return agent


@pytest.fixture
def no_sun_avoidance(monkeypatch):
    """Give the agent a site without Sun avoidance."""
    # These tests run at the wall clock; test_az_scan_sun_check.py covers the Sun.
    no_sun_site = _AGENT.get_fyst_site(sun_avoidance_enabled=False)
    monkeypatch.setattr(_AGENT, "get_fyst_site", lambda *a, **k: no_sun_site)


@pytest.mark.parametrize("task, method, params, command", [
    ("go_to", "move_to", {"az": 120.0, "el": 45.0}, "move-to"),
    ("az_scan", "azimuth_scan", {"scan_params": {
        "start_time": 60.0, "azimuth_range": [100.0, 140.0], "elevation": 50.0,
        "num_scans": 2, "turnaround_time": 2.0, "speed": 0.5}}, "azimuth-scan"),
    ("fromfile_scan", "scan_pattern_from_file", {"scan_filename": "path.txt"}, "/path POST"),
])
def test_legacy_tasks_fail_on_a_refusal_relayed_as_200(monkeypatch, gateway, no_sun_avoidance,
                                                       task, method, params, command):
    """go_to, az_scan and fromfile_scan read their answer with the same helper,
    so a refusal relayed as HTTP 200 fails each, saying the command was refused,
    with the TCS's message."""
    refusal = _Resp(200, body=gateway.gateway_refusal(gateway.GO_START_TOO_SOON))
    client = types.SimpleNamespace(**{method: lambda *a, **k: refusal})
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system",
                        lambda *a, **k: client)
    agent = _with_acu_conf(_make_agent(_FakeTCS()))

    ok, msg = getattr(agent, task)(None, params)

    assert ok is False
    assert msg.startswith(f"{task}: {command} refused ({gateway.GO_START_TOO_SOON}"), msg


def test_go_to_fails_on_a_200_that_is_not_the_go_tcs_reply(monkeypatch):
    """A 200 whose body is not the Go TCS's JSON, such as a proxy's login page,
    is an intermediary's answer: go_to fails as at the OCS API, quoting the
    reply, and says whether the dish moved is unknown."""
    page = _Resp(200, text="<html>login</html>", body=None)
    client = types.SimpleNamespace(move_to=lambda *a, **k: page)
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system",
                        lambda *a, **k: client)
    agent = _with_acu_conf(_make_agent(_FakeTCS()))

    ok, msg = agent.go_to(None, {"az": 120.0, "el": 45.0})

    assert (ok, msg) == (False, "go_to: move-to failed at the OCS API (the reply was not the "
                                "Go TCS's: '<html>login</html>'); whether the dish moved is "
                                "unknown.")


@pytest.mark.parametrize("task, method, command, params, unknown", [
    ("go_to", "move_to", "move-to", {"az": 120.0, "el": 45.0}, "whether the dish moved is unknown"),
    ("az_scan", "azimuth_scan", "azimuth-scan", {"scan_params": {
        "start_time": 60.0, "azimuth_range": [100.0, 140.0], "elevation": 50.0,
        "num_scans": 2, "turnaround_time": 2.0, "speed": 0.5}}, "whether it launched is unknown"),
    ("fromfile_scan", "scan_pattern_from_file", "/path POST", {"scan_filename": "path.txt"},
     "whether it launched is unknown"),
])
def test_legacy_tasks_fail_on_a_transport_failure(monkeypatch, no_sun_avoidance, task, method,
                                                  command, params, unknown):
    """go_to, az_scan and fromfile_scan return a failure naming the command when
    aculib's post raises SystemExit on a requests error, rather than letting it
    out of the task. A read timeout can come after the OCS API passed the
    command on, so the failure says the outcome is unknown."""
    def transport_failure(*a, **k):
        raise SystemExit("connection refused")

    client = types.SimpleNamespace(**{method: transport_failure})
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system",
                        lambda *a, **k: client)
    agent = _with_acu_conf(_make_agent(_FakeTCS()))

    ok, msg = getattr(agent, task)(None, params)

    assert ok is False
    assert msg == f"{task}: {command} failed in transport (connection refused); {unknown}."


def test_go_to_fails_on_an_ocs_api_error_answered_in_plain_text(monkeypatch, gateway):
    """An OCS API error to go_to's move-to (HTTP 500 with FastAPI's plain-text
    body) gives the task's failure tuple, saying the move-to failed at the OCS
    API, naming the status, and that whether the dish moved is unknown, rather
    than an exception out of the task: aculib's move_to hands back the Response
    whatever its body."""
    app = gateway.create_mock_tcs()
    app.state.recorder.refusals["move-to"].append(gateway.OCS_API_ERROR)
    client = _ocs_api_client(gateway, app)
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system",
                        lambda *a, **k: client)
    agent = _with_acu_conf(_make_agent(_FakeTCS()))

    ok, msg = agent.go_to(None, {"az": 120.0, "el": 45.0})

    assert (ok, msg) == (False, "go_to: move-to failed at the OCS API (OCS API error HTTP 500 (the TCS "
                                "may or may not have taken the command)); whether the dish moved is "
                                "unknown.")
    assert len(app.state.recorder.move_to_bodies) == 1


@pytest.mark.parametrize("task, method, params, expected", [
    ("az_scan", "azimuth_scan", {"scan_params": {
        "start_time": 60.0, "azimuth_range": [100.0, 140.0], "elevation": 50.0,
        "num_scans": 2, "turnaround_time": 2.0, "speed": 0.5}},
     "az_scan: azimuth-scan failed at the OCS API (OCS API error HTTP 500 (the TCS may or may not "
     "have taken the command)); whether it launched is unknown."),
    ("fromfile_scan", "scan_pattern_from_file", {"scan_filename": "path.txt"},
     "fromfile_scan: /path POST failed at the OCS API (OCS API error HTTP 500 (the TCS may or may "
     "not have taken the command)); whether it launched is unknown."),
], ids=["az_scan", "fromfile_scan"])
def test_az_scan_and_fromfile_scan_fail_on_an_ocs_api_error_answered_in_plain_text(
        monkeypatch, gateway, no_sun_avoidance, task, method, params, expected):
    """An OCS API error (HTTP 500 with FastAPI's plain-text body: the API's hop
    to the Go TCS raised) can come after the Go TCS took the command, so az_scan
    and fromfile_scan fail saying the command failed at the OCS API and that
    whether the scan launched is unknown, not that the Go TCS rejected it."""
    reply = _Resp(500, text=gateway.OCS_API_ERROR, body=None)
    client = types.SimpleNamespace(**{method: lambda *a, **k: reply})
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system",
                        lambda *a, **k: client)
    agent = _with_acu_conf(_make_agent(_FakeTCS()))

    ok, msg = getattr(agent, task)(None, params)

    assert (ok, msg) == (False, expected)


def test_fromfile_scan_raises_on_an_unreadable_scan_file(tmp_path):
    """Only a transport failure becomes a failed task: a scan file that cannot be
    read raises out of fromfile_scan before anything is sent."""
    agent = _with_acu_conf(_make_agent(_FakeTCS()))

    with pytest.raises(FileNotFoundError):
        agent.fromfile_scan(None, {"scan_filename": str(tmp_path / "missing.txt")})


def _abort_task_on(client):
    """An agent whose ``abort`` task sends its /abort through ``client``, with no
    scan Process running."""
    agent = _make_agent(_FakeTCS())
    agent.agent = types.SimpleNamespace(stop=lambda op: (1, f"{op} not running", {}))
    agent._make_tcs = lambda: client
    return agent


def _abort_agent(reply, status=None, move_to_reply=None, dropped=0):
    """An agent whose ``abort`` task meets ``reply``, aculib's parse of the TCS's
    answer to /abort (raised when it is an exception), with no scan Process
    running. ``status`` gives the ACU status reads that follow: one read, or a
    list read in order with the last repeating, an entry that is an exception
    being raised; by default the stack drained (9999 free) with both axes in
    Stop. A move-to gets ``move_to_reply`` in the same way, by default accepted.
    The ACU acts on an accepted move-to once ``dropped`` accepted ones have gone
    unheeded, as when the Go TCS drops a queued command because its own ACU
    record is stale: from then on the reads show the axes out of ProgramTrack
    (:func:`_stopped_by_a_move_to`). The fake counts its reads in
    ``status_reads``."""
    reads = status if isinstance(status, list) else [_acu(9999, "Stop") if status is None else status]
    answers = move_to_reply if isinstance(move_to_reply, list) else [move_to_reply]

    def nth(items, n):
        item = items[min(n, len(items)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    class _AbortTCS(_FakeTCS):
        status_reads = 0
        accepted = 0  # move-tos the Go TCS accepted

        def get_status(self):
            self.status_reads += 1
            read = nth(reads, self.status_reads)
            return _stopped_by_a_move_to(read) if self.accepted > dropped else dict(read)

        def move_to(self, az, el):
            self.calls.append(("move_to", az, el))
            answer = nth(answers, sum(c[0] == "move_to" for c in self.calls))
            answer = _Resp(200) if answer is None else answer
            if _AGENT.tcs_response_status(answer) == 200:
                self.accepted += 1
            return answer

    tcs = _AbortTCS(abort_reply=reply)
    return _abort_task_on(tcs), tcs


def test_abort_counts_nothing_to_abort_as_success(sync_reactor):
    """Go's 'nothing to abort' (no command running, the state an abort asks for)
    is a success, and the task's message says so."""
    agent, tcs = _abort_agent(NOTHING_TO_ABORT)

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert ok is True
    assert NOTHING_TO_ABORT["message"] in msg
    assert ("abort",) in tcs.calls


@pytest.mark.parametrize("status, note", [
    (_acu(4000, "Stop"), "(nothing to abort; axes not in program track, 5999 stale points on the stack)"),
    (_acu(9999, "ProgramTrack"), "(nothing to abort)"),
], ids=["old-points-in-stop", "completed-scan-in-program-track"])
def test_abort_counts_nothing_to_abort_as_success_while_no_track_plays(sync_reactor, status, note):
    """'nothing to abort' is a success whenever the ACU plays no track: points
    left on the stack with both axes in Stop are a stopped track's leftovers
    (the Go TCS's /abort sets Stop and leaves them), which the message names,
    and ProgramTrack with the stack drained is a completed scan, which leaves
    the axes in that mode."""
    agent, _ = _abort_agent(NOTHING_TO_ABORT, status=status)

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert ok is True, msg
    assert note in msg, msg


def test_abort_stops_a_held_track_with_a_move_to_to_where_the_dish_is(sync_reactor):
    """'nothing to abort' while the ACU still plays a program track (points on
    its stack with an axis in ProgramTrack: the Go TCS let go of a track the
    dish is still playing) is answered with one move-to to the encoder position
    the same status read gives, the command this agent uses to stop
    the ACU. The task succeeds once a later read shows the axes out of
    ProgramTrack, naming the stopped track and where it was stopped."""
    agent, tcs = _abort_agent(NOTHING_TO_ABORT, status=HELD)

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert (ok, msg) == (True, "abort: Go TCS /abort sent (nothing to abort; held program track (4999 "
                               "points) stopped via a move-to at the encoder position read "
                               "(az=123.400, el=56.700)); stop requested on any running scan.")
    assert [c for c in tcs.calls if c[0] == "move_to"] == [("move_to", *HELD_AT)]


@pytest.mark.parametrize("case", ["next-read-in-stop", "first-move-to-dropped",
                                  "busy-while-another-command-stops-it",
                                  "no-answer-while-another-command-stops-it"])
def test_a_held_track_counts_as_stopped_once_the_acu_leaves_program_track(sync_reactor, case):
    """The proof that a held track has stopped is a status read with the axes
    out of ProgramTrack, whichever command put them there, not the move-to's
    200, which means queued: the Go TCS drops a queued command while its own
    ACU record is stale, the state a held track arises in. A read in Stop after
    the move-to counts; a move-to accepted but not acted on is sent once more,
    and the second one stops the track; and when the abort task's move-to met a
    busy Go TCS or got no answer while another command stopped the track (the
    scan's own stop, say), the stop counts, its note saying how."""
    via = "via a move-to at the encoder position read (az=123.400, el=56.700)"
    status, move_to_reply, dropped, how, moves = {
        "next-read-in-stop": ([HELD, _acu(5000, "Stop", position=HELD_AT)], None, 0, via, 1),
        "first-move-to-dropped": (HELD, None, 1, via, 2),
        "busy-while-another-command-stops-it": (
            [HELD, _acu(5000, "Preset", position=HELD_AT)], {}, 0,
            "after the move-to to stop it met a busy Go TCS (by that move-to if it was taken, or by "
            "a concurrent command)", 1),
        "no-answer-while-another-command-stops-it": (
            [HELD, _acu(5000, "Stop", position=HELD_AT)], SystemExit("read timed out"), 0,
            "after the move-to to stop it failed in transport (read timed out) (by that move-to if "
            "it was taken, or by a concurrent command)", 1),
    }[case]
    agent, tcs = _abort_agent(NOTHING_TO_ABORT, status=status,
                              move_to_reply=move_to_reply, dropped=dropped)

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert (ok, msg) == (True, "abort: Go TCS /abort sent (nothing to abort; held program track (4999 "
                               f"points) stopped {how}); stop requested on any running scan.")
    assert [c for c in tcs.calls if c[0] == "move_to"] == [("move_to", *HELD_AT)] * moves


@pytest.mark.parametrize("leaves", [True, False], ids=["leaves-within-the-wait", "never-leaves"])
def test_the_held_track_stop_polls_the_acu_for_a_bounded_time(sync_reactor, held_track_clock, leaves):
    """After the stopping move-to the ACU is read every HELD_TRACK_STOP_POLL_SEC
    for up to HELD_TRACK_STOP_WAIT_SEC, the agent's own values on a test clock:
    a track the ACU leaves within the wait is stopped at the first read that
    shows it, with no second move-to; one it never leaves gets a second move-to
    once the first wait ends, and the abort fails when the second wait ends,
    so a held track holds the abort task for a bounded time."""
    now = held_track_clock
    poll, wait = _AGENT.HELD_TRACK_STOP_POLL_SEC, _AGENT.HELD_TRACK_STOP_WAIT_SEC
    leaves_after = 0.4 * wait if leaves else None  # seconds after the first move-to

    class _SlowStopTCS(_FakeTCS):
        """The ACU leaves the held track ``leaves_after`` s after the first
        move-to, or never; the time of each read after that move-to is kept,
        relative to it, in ``read_at``."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.moved_at = None
            self.read_at = []

        def get_status(self):
            if self.moved_at is None:
                return dict(HELD)
            self.read_at.append(now[0] - self.moved_at)
            if leaves_after is not None and now[0] - self.moved_at >= leaves_after:
                return _stopped_by_a_move_to(HELD)
            return dict(HELD)

        def move_to(self, az, el):
            self.calls.append(("move_to", now[0]))
            if self.moved_at is None:
                self.moved_at = now[0]
            return _Resp(200)

    tcs = _SlowStopTCS(abort_reply=NOTHING_TO_ABORT)

    ok, msg = _run(_abort_task_on(tcs).abort(_FakeSession(), {}))

    def polls(start, until):
        """The read times of one wait: from ``start``, every ``poll``, to ``until``."""
        return [start + k * poll for k in range(math.ceil((until - start) / poll) + 1)]

    moved = [c[1] - tcs.moved_at for c in tcs.calls if c[0] == "move_to"]
    if leaves:
        stopped_at = math.ceil(leaves_after / poll) * poll
        assert ok is True and "stopped via a move-to" in msg, msg
        assert moved == [0.0]
        assert tcs.read_at == pytest.approx(polls(0.0, stopped_at))
    else:
        second = math.ceil(wait / poll) * poll  # the first wait's last read, then the second move-to
        assert ok is False and "was accepted but the ACU still plays the track" in msg, msg
        assert moved == pytest.approx([0.0, second])
        assert tcs.read_at == pytest.approx(polls(0.0, wait) + polls(second, second + wait))


@pytest.mark.parametrize("case", [
    "no-position", "position-not-a-number", "move-to-accepted-but-not-acted-on",
    "no-modes-in-the-reads", "status-unreadable-after-the-move-to", "move-to-busy",
    "move-to-busy-relayed-as-200", "move-to-refused", "move-to-ocs-api-error",
    "move-to-foreign-200", "move-to-no-answer", "move-to-no-answer-naming-busy",
    "move-to-no-answer-then-unreadable", "move-to-busy-then-unreadable",
    "move-to-accepted-then-no-answer",
])
def test_abort_fails_on_nothing_to_abort_while_a_track_is_held(sync_reactor, gateway, case):
    """'nothing to abort' while the ACU still plays a program track fails the
    task, saying the ACU is not known to be stopped, whenever no read shows the
    track stopped: the status read gives no usable encoder position to aim the
    move-to at (and none is sent); the move-to is accepted twice and not acted
    on; the reads after it carry no axis modes or cannot be read, so the stop
    is unconfirmed; the move-to meets a busy Go TCS twice (aculib's {} for an
    HTTP 503, or Go's busy relayed as HTTP 200); or it is refused, gets an OCS
    API error (HTTP 500) or a 200 that is not the Go TCS's reply (after either
    the Go TCS may or may not have taken it) or no answer, none of which is
    sent again, whatever the transport error says. The message names the track
    and why it was not stopped, and an unconfirmed stop says what became of the
    move-to."""
    unplaced = ", and its position is not in the status, so it was not stopped"
    target = "the encoder position read (az=123.400, el=56.700)"
    later = " but no later status read showed the axis modes, so the stop is unconfirmed"
    unconfirmed = f"; a move-to at {target} was sent{later}"
    busy = "; the move-to to stop it met a busy Go TCS"
    unreadable = [HELD, SystemExit("connection refused")]
    status, move_to_reply, dropped, why, moves = {
        "no-position": (_acu(5000, "ProgramTrack"), None, 0, unplaced, 0),
        "position-not-a-number": (_acu(5000, "ProgramTrack", position=("n/a", 56.7)), None, 0,
                                  unplaced, 0),
        "move-to-accepted-but-not-acted-on": (
            HELD, None, 2, f"; the move-to at {target} was accepted but the ACU still plays the track", 2),
        "no-modes-in-the-reads": (_acu(5000, None, position=HELD_AT), None, 0, unconfirmed, 2),
        "status-unreadable-after-the-move-to": (unreadable, None, 0, unconfirmed, 2),
        "move-to-busy": (HELD, {}, 0, busy, 2),
        "move-to-busy-relayed-as-200": (
            HELD, _Resp(200, body=gateway.gateway_refusal(gateway.GO_BUSY)), 0, busy, 2),
        "move-to-refused": (
            HELD, _Resp(200, body=gateway.gateway_refusal(gateway.GO_AZIMUTH_OUT_OF_RANGE)), 0,
            f"; the move-to to stop it was refused ({gateway.GO_AZIMUTH_OUT_OF_RANGE})", 1),
        "move-to-ocs-api-error": (
            HELD, _Resp(500, text=gateway.OCS_API_ERROR, body=None), 0,
            "; the move-to to stop it got no Go TCS answer (OCS API error HTTP 500 (the TCS may or "
            "may not have taken the command))", 1),
        "move-to-foreign-200": (
            HELD, _Resp(200, text="<html>login</html>", body=None), 0,
            "; the move-to to stop it got no Go TCS answer (the reply was not the Go TCS's: "
            "'<html>login</html>')", 1),
        "move-to-no-answer": (HELD, SystemExit("read timed out"), 0,
                              "; the move-to to stop it failed in transport (read timed out)", 1),
        "move-to-no-answer-naming-busy": (
            HELD, SystemExit("resource busy"), 0,
            "; the move-to to stop it failed in transport (resource busy)", 1),
        "move-to-no-answer-then-unreadable": (
            unreadable, SystemExit("read timed out"), 0,
            f"; the move-to to stop it failed in transport (read timed out){later}", 1),
        "move-to-busy-then-unreadable": (unreadable, {}, 0, f"{busy}{later}", 2),
        "move-to-accepted-then-no-answer": (
            unreadable, [None, SystemExit("read timed out")], 0,
            f"; a move-to at {target} was accepted, then the move-to to stop it failed in transport "
            f"(read timed out){later}", 2),
    }[case]
    agent, tcs = _abort_agent(NOTHING_TO_ABORT, status=status,
                              move_to_reply=move_to_reply, dropped=dropped)

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert (ok, msg) == (False, "abort: the Go TCS had nothing to abort, but the ACU is not known to "
                                "be stopped (nothing to abort; ACU still playing a program track "
                                f"(4999 points on its stack){why}).")
    assert len([c for c in tcs.calls if c[0] == "move_to"]) == moves


@pytest.mark.parametrize("status, why, reads", [
    (SystemExit("connection refused"), "the ACU status could not be read", 2),
    (dict(HELD, QtyOfFreeProgramTrackStackPositions="n/a"), "the ACU status is malformed", 1),
    (_without(HELD, "QtyOfFreeProgramTrackStackPositions"),
     "the ACU status is missing its stack count", 1),
    (_without(HELD, "QtyOfFreeProgramTrackStackPositions", "AzimuthMode", "ElevationMode"),
     "the ACU status carries neither the stack count nor the axis modes", 1),
], ids=["unreadable", "stack-count-not-a-number", "no-stack-count-in-program-track",
        "neither-stack-count-nor-modes"])
def test_abort_fails_on_nothing_to_abort_when_the_acu_status_is_unknown(sync_reactor, status, why, reads):
    """'nothing to abort' does not show the dish still: a track the Go TCS has
    let go of plays on while every /abort is answered so. When the status read
    after it fails, on the first try and on the one retry, gives a stack count
    that is not a number, gives none with the axes in ProgramTrack, or carries
    neither the count nor the axis modes, whether a track still plays is
    unknown, and the task fails, saying so, without a move-to."""
    agent, tcs = _abort_agent(NOTHING_TO_ABORT, status=status)

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert (ok, msg) == (False, "abort: the Go TCS had nothing to abort, but the ACU is not known to "
                                f"be stopped (nothing to abort; {why}, so whether a program track "
                                "still plays is unknown).")
    assert tcs.status_reads == reads
    assert [c for c in tcs.calls if c[0] == "move_to"] == []


@pytest.mark.parametrize("second_read, note", [
    (_acu(4000, "Stop"), "nothing to abort; axes not in program track, 5999 stale points on the stack"),
    (HELD, "nothing to abort; held program track (4999 points) stopped via a move-to at the encoder "
           "position read (az=123.400, el=56.700)"),
], ids=["old-points-in-stop", "track-playing"])
def test_abort_reads_the_acu_status_again_after_one_failed_read(sync_reactor, second_read, note):
    """One failed status read after 'nothing to abort' is retried once, and the
    task decides on the read that succeeds: a stopped track's leftovers are a
    success as they stand, and a track still playing is stopped with a
    move-to."""
    agent, _ = _abort_agent(NOTHING_TO_ABORT,
                            status=[SystemExit("connection refused"), second_read])

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert (ok, msg) == (True, f"abort: Go TCS /abort sent ({note}); stop requested on any running scan.")


def test_abort_fails_on_any_other_refusal(sync_reactor, gateway):
    """Any /abort refusal other than "nothing to abort" fails the task, with the
    TCS's message, rather than reporting the abort sent (Go's refusal of a body
    stands in for one)."""
    agent, _ = _abort_agent(gateway.gateway_refusal(gateway.GO_NO_PARAMETERS))

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert ok is False
    assert gateway.GO_NO_PARAMETERS in msg


def test_abort_fails_on_a_reply_that_is_neither_ok_nor_a_refusal(sync_reactor):
    """An answer to /abort whose "status" is not "ok" and that is no refusal
    (here FastAPI's body for a route it does not know) is not taken as sent: the
    task fails, quoting the reply."""
    agent, _ = _abort_agent({"detail": "Not Found"})

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert ok is False
    assert "unexpected reply: {'detail': 'Not Found'}" in msg, msg


def test_a_reply_holding_braces_reaches_the_log_intact(sync_reactor):
    """The agent's logger reads its message as a format string, so a reply goes in
    as a keyword argument and is logged as it came, braces and all."""
    agent, _ = _abort_agent({"detail": "{not found}"})
    agent.log = _WarnLog()

    ok, _ = _run(agent.abort(_FakeSession(), {}))

    assert ok is False
    assert ("/abort: unexpected reply from the TCS: {'detail': '{not found}'}"
            in agent.log.warnings), agent.log.warnings


def test_a_build_refusal_holding_braces_reaches_the_log_intact(sync_reactor):
    """A build refusal whose text holds braces (a footprint of {} on a field scan) is
    logged as it came by Twisted's own Logger, not as "Unable to format event"."""
    from twisted.logger import Logger, formatEvent

    refusal = "pong_scan does not take footprint, got {}: use module."

    def build(**kwargs):
        raise ValueError(refusal)

    events = []
    tcs = _FakeTCS()
    agent = _make_agent(tcs)
    agent.log = Logger(observer=events.append)

    ok, msg = _dispatch(tcs, build_fn=build, agent=agent, job="pong_scan")

    assert (ok, msg) == (False, f"Trajectory build failed: {refusal}")
    logged = [formatEvent(event) for event in events]
    assert f"pong_scan: trajectory build failed: {refusal}" in logged, logged


@pytest.mark.parametrize("status, after", [
    (_acu(9999, "Stop"), "; the ACU reports both axes out of program track"),
    (_acu(5000, "ProgramTrack"), "; the ACU still reports a program track (4999 points)"),
    (_acu(5000, None), "; the ACU still reports a program track (4999 points)"),
    (_acu(9999, "ProgramTrack"), "; the ACU is in program track with no points left"),
    (_without(_acu(5000, "ProgramTrack"), "QtyOfFreeProgramTrackStackPositions"),
     "; the ACU reports program track and its stack count is missing"),
    (SystemExit("connection refused"), ""),
], ids=["axes-stopped", "track-playing", "points-without-modes", "program-track-drained",
        "program-track-without-a-stack-count", "status-unreadable"])
def test_abort_without_an_answer_says_what_the_acu_shows(sync_reactor, status, after):
    """An /abort that gets no answer (aculib raises SystemExit on a requests
    error) fails the task but may still have taken effect, so the note says so
    and adds what one status read shows: both axes out of program track; a
    program track still reported, with the points on its stack (the points
    alone when the read carries no modes); ProgramTrack with the stack drained,
    or with no stack count in the read; or nothing more when that read fails
    too. The task then says the dish may still be moving."""
    agent, _ = _abort_agent(SystemExit("read timed out"), status=status)

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert ok is False
    assert msg == ("abort: Go TCS /abort failed (no answer from the TCS (the abort may have "
                   f"taken effect); see log{after}); the dish may still be moving.")


@pytest.mark.parametrize("status, words, moves", [
    ({}, "(nothing to abort)", []),
    ({"QtyOfFreeProgramTrackStackPositions": 4000}, "5999 stale points on the stack", []),
    ({"QtyOfFreeProgramTrackStackPositions": 5000, "AzimuthMode": "ProgramTrack",
      "ElevationMode": "ProgramTrack"},
     "held program track (4999 points) stopped via a move-to at the encoder position read "
     "(az=0.000, el=60.000)", [{"azimuth": 0.0, "elevation": 60.0}]),
], ids=["idle", "old-points-in-stop", "track-playing"])
def test_abort_task_through_the_ocs_api_with_no_command_running(sync_reactor, gateway, status,
                                                                words, moves):
    """With no command running the OCS API relays the Go TCS answer "nothing to
    abort" as HTTP 200, and the task decides on the ACU status the API serves:
    an idle dish or a stopped track's leftovers is a success as it stands, and
    a track still playing is stopped with a move-to, through the same API, to
    the position that status gives (the mock's dish sits at az 0, el 60), the
    next status showing the axes in Preset, as the Go TCS's MoveTo leaves
    them."""
    app = gateway.create_mock_tcs()
    app.state.recorder.status.update(status)
    agent = _abort_task_on(_ocs_api_client(gateway, app))

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert ok is True, msg
    assert words in msg, msg
    assert app.state.recorder.abort_count == 1
    assert app.state.recorder.move_to_bodies == moves


@pytest.mark.parametrize("refusal, expected, bodies", [
    ("busy", (True, "abort: Go TCS /abort sent (nothing to abort; held program track (4999 points) "
                    "stopped via a move-to at the encoder position read (az=0.000, el=60.000)); stop "
                    "requested on any running scan."), 2),
    ("ocs-api-error", (False, "abort: the Go TCS had nothing to abort, but the ACU is not known to be "
                              "stopped (nothing to abort; ACU still playing a program track (4999 "
                              "points on its stack); the move-to to stop it got no Go TCS answer (OCS "
                              "API error HTTP 500 (the TCS may or may not have taken the command)))."),
     1),
], ids=["busy-then-accepted", "ocs-api-error"])
def test_abort_task_through_the_ocs_api_answers_the_stopping_move_to(sync_reactor, gateway, refusal,
                                                                     expected, bodies):
    """The stopping move-to's answer as the OCS API relays it: Go's busy (HTTP
    200 with the message 'busy') is sent once more, and the second move-to,
    accepted, stops the track; the API's own error (HTTP 500 in plain text),
    after which the Go TCS may or may not have the command, is not sent again,
    and the task fails, saying the ACU is not known to be stopped."""
    app = gateway.create_mock_tcs()
    app.state.recorder.status.update(QtyOfFreeProgramTrackStackPositions=5000,
                                     AzimuthMode="ProgramTrack", ElevationMode="ProgramTrack")
    app.state.recorder.refusals["move-to"].append(
        {"busy": gateway.GO_BUSY, "ocs-api-error": gateway.OCS_API_ERROR}[refusal])
    agent = _abort_task_on(_ocs_api_client(gateway, app))

    ok, msg = _run(agent.abort(_FakeSession(), {}))

    assert (ok, msg) == expected
    assert len(app.state.recorder.move_to_bodies) == bodies


def test_abort_task_through_the_ocs_api_stops_a_running_path_then_finds_none(sync_reactor, gateway):
    """An /abort while an accepted /path runs is taken. Once a path has played
    out (the Go TCS ends the command when the stack drains, leaving the axes in
    ProgramTrack), the next /abort meets "nothing to abort", a success."""
    app = gateway.create_mock_tcs()
    client = _ocs_api_client(gateway, app)
    agent = _abort_task_on(client)
    path = {"start_time": time.time() + 60.0, "coordsys": "Horizon",
            "points": [[0.0, ENC_AZ, ENC_EL, 0.0, 0.0]]}

    client.scan_pattern(path)
    ok, msg = _run(agent.abort(_FakeSession(), {}))
    assert (ok, msg) == (True, "abort: Go TCS /abort sent; stop requested on any running scan.")

    client.scan_pattern(path)
    app.state.recorder.running = False  # the path played out
    app.state.recorder.status.update(AzimuthMode="ProgramTrack", ElevationMode="ProgramTrack")
    ok, msg = _run(agent.abort(_FakeSession(), {}))
    assert ok is True, msg
    assert "(nothing to abort)" in msg, msg
    assert app.state.recorder.abort_count == 2


# ---------------------------------------------------------------------------
# session.data: the runner publishes its phase (building, slewing, holding,
# scanning) with the slew target, the posted start_time and the scan_params keys
# the build does not read, so a caller can tune the readout once the dish holds
# the scan's start and an operator can see where a dispatch stands.
# ---------------------------------------------------------------------------


class _RecordingSession(_FakeSession):
    """Keeps every ``session.data`` the runner assigns, in order."""

    def __init__(self, status="running"):
        super().__init__(status)
        self.snapshots = []

    @property
    def data(self):
        return self.snapshots[-1] if self.snapshots else {}

    @data.setter
    def data(self, value):
        self.snapshots.append(dict(value))


def test_runner_publishes_its_phases_in_session_data(clock):
    """The happy path publishes building, slewing, holding and scanning in that
    order: the slew target and the point count appear with 'slewing', 'holding'
    carries the start_time the POST sent, 'scanning' comes once that start has
    passed, and the op, the caller's scheduled_t0_unix, the ignored keys (none
    here), the module key (None, not sent) and a timestamp ride on every snapshot."""
    session = _RecordingSession()
    tcs = _TimedTCS(clock, session=session)

    ok, _ = _dispatch(tcs, session=session, scheduled_t0_unix=1.7e9)

    assert ok is True
    assert [s["phase"] for s in session.snapshots] == ["building", "slewing", "holding", "scanning"]
    building, slewing, holding, scanning = session.snapshots
    assert "encoder_az" not in building
    assert (slewing["encoder_az"], slewing["encoder_el"], slewing["n_points"]) == (ENC_AZ, ENC_EL, 2)
    posted = next(c[1] for c in tcs.calls if c[0] == "scan_pattern")
    assert holding["start_time"] == posted["start_time"]
    assert scanning["timestamp"] >= posted["start_time"]
    for snap in session.snapshots:
        assert snap["scan"] == "source_scan"
        assert snap["scheduled_t0_unix"] == 1.7e9
        assert snap["ignored_keys"] == []
        assert snap["module"] is None
        assert snap["timestamp"] >= 1e9
    assert session.data["phase"] == "scanning"


def test_the_phase_stays_holding_until_the_scheduled_start(clock):
    """With the track on the ACU from the first read after the POST, the phase
    reads 'holding' at every read before the scheduled start_time and
    'scanning' at every read after it: a caller tuning the readout in the hold
    must not see the scan begun because the stack holds its points."""
    session = _RecordingSession()
    tcs = _TimedTCS(clock, session=session)

    ok, msg = _dispatch(tcs, session=session)

    assert ok is True, msg
    start = tcs.posted["start_time"]
    seen = [(now >= start, phase) for now, phase, _ in tcs.track_log]
    assert all(phase == ("scanning" if begun else "holding") for begun, phase in seen), seen
    assert {phase for _, phase in seen} == {"holding", "scanning"}, seen


def test_a_stop_during_the_build_leaves_the_phase_at_building(sync_reactor):
    """A stop that lands while the trajectory is built ends the scan before any
    move is commanded, and the phase stays 'building': 'slewing' would tell a
    caller the dish is on its way to the scan."""
    session = _RecordingSession()
    tcs = _FakeTCS()

    def build_then_stop(**kwargs):
        session.status = "stopping"
        return _canned_build(**kwargs)

    ok, msg = _dispatch(tcs, build_fn=build_then_stop, session=session)

    assert (ok, msg) == (True, "Aborted before slew.")
    assert [s["phase"] for s in session.snapshots] == ["building"]
    assert tcs.calls == []


def test_a_stop_between_busy_move_to_attempts_leaves_the_phase_at_building(sync_reactor):
    """A stop that lands while the slew's move-to meets a busy Go TCS ends the
    scan at the next attempt, before any move is taken: one move-to, no /abort
    (nothing moved), and the phase stays 'building', since 'slewing' would tell
    a caller the dish is on its way to the scan."""
    session = _RecordingSession()

    class _BusyMoveTCS(_FakeTCS):
        def move_to(self, az, el):
            self.calls.append(("move_to", az, el))
            session.status = "stopping"
            return {}  # aculib's answer to an HTTP 503: the Go TCS is busy

    tcs = _BusyMoveTCS()

    ok, msg = _dispatch(tcs, session=session)

    assert (ok, msg) == (True, "Aborted before slew.")
    assert tcs.calls == [("move_to", ENC_AZ, ENC_EL)]
    assert [s["phase"] for s in session.snapshots] == ["building"]


def test_slewing_is_published_once_the_move_to_is_accepted(sync_reactor):
    """With the first move-to met by a busy Go TCS, the phase reads 'building'
    at both attempts and 'slewing' appears only once the second is accepted,
    followed by 'holding' once the /path POST is."""
    session = _RecordingSession()
    phases = []  # the phase at each move-to

    class _BusyOnceTCS(_FakeTCS):
        def move_to(self, az, el):
            self.calls.append(("move_to", az, el))
            phases.append(session.data.get("phase"))
            return {} if len(self.calls) == 1 else _Resp(200)

    tcs = _BusyOnceTCS()

    ok, msg = _dispatch(tcs, session=session)

    assert ok is True, msg
    assert phases == ["building"] * 2
    assert [s["phase"] for s in session.snapshots] == ["building", "slewing", "holding"]


def test_a_refused_path_post_leaves_the_phase_at_slewing(sync_reactor):
    """'holding' means the POST was accepted: a refused /path leaves the last
    phase at 'slewing', so a caller waiting to tune never sees a hold for a scan
    the Go TCS did not queue."""
    tcs = _Reject400TCS()
    session = _RecordingSession()

    ok, _ = _dispatch(tcs, session=session)

    assert ok is False
    assert [s["phase"] for s in session.snapshots] == ["building", "slewing"]


def test_unknown_scan_params_keys_are_warned_and_published_not_refused(sync_reactor):
    """A key the build does not read is named in a warning and in
    session.data['ignored_keys'] from the first snapshot on, and the scan still
    runs; max_dispatch_delay_sec, the agent's own key, is not named."""
    tcs = _FakeTCS()
    agent = _make_agent(tcs)
    agent.log = _WarnLog()
    session = _RecordingSession()
    scan_params = {"ra_center": 10.0, "elevation": 50.0, "start_time": 5.0,
                   "max_dispatch_delay_sec": 60.0}

    ok, _ = _dispatch(tcs, session=session, agent=agent, job="pong_scan", scan_params=scan_params)

    assert ok is True
    assert session.snapshots[0]["ignored_keys"] == ["elevation", "start_time"]
    assert session.data["ignored_keys"] == ["elevation", "start_time"]
    assert any("pong_scan" in w and "['elevation', 'start_time']" in w for w in agent.log.warnings), \
        agent.log.warnings


def test_a_constant_el_key_the_chosen_form_does_not_use_is_warned_and_published(sync_reactor):
    """A key only the other constant-elevation form reads is named with the unread
    ones (here rising, beside an azimuth window), and the scan still runs."""
    tcs = _FakeTCS()
    agent = _make_agent(tcs)
    agent.log = _WarnLog()
    session = _RecordingSession()
    scan_params = {"az_min": 26.0, "az_max": 154.0, "elevation": 50.0, "velocity": 0.8,
                   "duration": 1300.0, "rising": True, "start_time": 5.0}

    ok, _ = _dispatch(tcs, session=session, agent=agent, job="constant_el_scan",
                      scan_params=scan_params)

    assert ok is True
    assert session.snapshots[0]["ignored_keys"] == ["rising", "start_time"]
    assert session.data["ignored_keys"] == ["rising", "start_time"]
    assert any("constant_el_scan" in w and "['rising', 'start_time']" in w
               for w in agent.log.warnings), agent.log.warnings


def test_the_module_key_is_published_as_sent_on_every_snapshot(clock):
    """The module key rides on every snapshot from 'building' on exactly as the
    caller sent it (a list stays a list), so a caller can tell this agent from
    one that drops the key without a word."""
    session = _RecordingSession()
    tcs = _TimedTCS(clock, session=session)
    module = ["IM5", "IM6"]

    ok, _ = _dispatch(tcs, session=session, job="pong_scan",
                      scan_params={"ra_center": 10.0, "module": module})

    assert ok is True
    assert [s["phase"] for s in session.snapshots] == ["building", "slewing", "holding", "scanning"]
    for snap in session.snapshots:
        assert snap["module"] == module
