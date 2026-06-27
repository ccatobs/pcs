"""Drive-the-runner test for the shared scan body.

ACTUALLY EXECUTES ``ACUAgent._dispatch_scan_process``, the single runtime path
for ``source_scan`` / ``pong_scan`` / ``daisy_scan``, rather than only
inspecting its source. The sibling files unit-test the ocs-free primitives; this
covers the COMPOSITION the runner wires: the ``latch.update(...)`` call, the
slew-arrival comparison, the post-slew re-floor, and the ``_safe_abort`` routing.

The agent imports the SO ``ocs`` framework, absent on a dev box (``ocs`` resolves
to the single-file OCS REST client, so ``ocs.ocs_agent`` is unimportable).
``twisted`` IS present, so we stub ONLY the three ``ocs`` names the module
imports, then drive the ``@inlineCallbacks`` body synchronously (``deferToThread``
-> ``maybeDeferred``, ``dsleep`` -> fired no-op). On Linux/CI the stub is skipped
and the same tests run against the genuine module. The stub gives ``ocs`` no
package ``__path__``, so ``find_spec('ocs.ocs_agent')`` still fails and the
source-inspection skip-guards elsewhere stay skipped.
"""

import importlib.util
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


class _Resp:
    def __init__(self, code=200, text="ok"):
        self.status_code = code
        self.text = text


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


class _FakeTCS:
    """Records calls. ``get_status`` returns a 'running' status (free < 9999,
    which arms the latch) then a 'drained' one (free == 9999 + zero velocity,
    which completes it). ``move_to`` optionally trips an injected callback so a
    test can flip the session to 'stopping' mid-slew."""

    def __init__(self, on_move_to=None):
        self.calls = []
        self._poll = 0
        self.on_move_to = on_move_to

    def move_to(self, az, el):
        self.calls.append(("move_to", az, el))
        if self.on_move_to:
            self.on_move_to()
        return _Resp(200)

    def scan_pattern(self, payload):
        self.calls.append(("scan_pattern", dict(payload)))
        return _Resp(200)

    def abort(self):
        self.calls.append(("abort",))
        return {"status": "ok"}

    def get_status(self):
        self._poll += 1
        running = self._poll == 1
        return {
            "Qty of free program track stack positions": 5000 if running else 9999,
            "Azimuth current velocity": 0.4 if running else 0.0,
            "Elevation current velocity": 0.0,
            "Azimuth mode": "ProgramTrack",
            "Elevation mode": "ProgramTrack",
        }


def _canned_build(**kwargs):
    # Default to a near-now start so the post-slew re-floor is a ~no-op
    # (drift ~ 0) for the happy/gate POST paths. The stale-refusal test
    # overrides start_time into the deep past to drive drift > the cap.
    now = kwargs.get("now_unix", time.time())
    start = kwargs.pop("_start_time", now + 5.0)
    return {
        "encoder_az": ENC_AZ,
        "encoder_el": ENC_EL,
        "payload": {
            "start_time": start,
            "coordsys": "Horizon",
            "points": [[0.0, ENC_AZ, ENC_EL, 0.0, 0.0],
                       [1.0, ENC_AZ + 0.1, ENC_EL, 0.1, 0.0]],
        },
    }


def _make_agent(tcs):
    a = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    a.log = _Log()
    a.acu_read = tcs
    a.azel_lock = _FakeLock()
    # Converge the slew-arrival loop on the first poll: current == slew target.
    # The runner now calls this off-reactor with the per-scan ``tcs``
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
    assert tcs._poll >= 2  # latch needed the running->drained transition


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
# Slew-arrival gate: the dish must be position-arrived AND velocity-settled before
# /path is POSTed. Go TCS dequeues the next command only once its move is done
# (position within tol AND |velocity| < speedTol, commands.go:127-130); a /path
# POST while the mount is still settling returns 503 and the scan is dropped.
# These pin that the gate waits for the axes to stop. The broadcast carries no
# velocity, so the gate's velocity half is a status read; _current_encoder_azel is
# stubbed to report the dish already at the target, isolating the velocity gate.
# ---------------------------------------------------------------------------


class _VelGateTCS(_FakeTCS):
    """``get_status`` reports a non-zero azimuth velocity for the first
    ``settle_after`` polls (mount still moving) then a stopped + drained status.
    Position is reported arrived from poll one (via the stubbed
    ``_current_encoder_azel``), so only the velocity condition gates."""

    def __init__(self, settle_after=2):
        super().__init__()
        self.settle_after = settle_after

    def get_status(self):
        self._poll += 1
        moving = self._poll <= self.settle_after
        return {
            "Qty of free program track stack positions": 9999,
            "Azimuth current velocity": 0.5 if moving else 0.0,
            "Elevation current velocity": 0.0,
            "Azimuth mode": "ProgramTrack",
            "Elevation mode": "ProgramTrack",
        }


def test_slew_gate_waits_for_velocity_to_settle(sync_reactor):
    """Position is arrived from the first poll, but the azimuth axis is still
    moving for two status reads. The gate must NOT POST until the velocity drops
    below tolerance, so /path is POSTed only after >= settle_after+1 status
    polls (the extra polls are the gate spinning on the still-moving axis)."""
    tcs = _VelGateTCS(settle_after=2)
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
    # gate could only break on the third (velocity 0.0). A position-only gate
    # would have POSTed after zero status reads.
    assert tcs._poll >= tcs.settle_after + 1


def test_slew_gate_times_out_if_axes_never_stop(sync_reactor, monkeypatch):
    """If the mount is position-arrived but its axes never stop, the gate must
    NOT POST: it fails the Process on the slew timeout instead of POSTing into
    a still-settling mount (which Go TCS would reject with 503). Drive the
    deadline into the past so the bounded wait expires deterministically."""
    monkeypatch.setattr(_AGENT, "SLEW_TIMEOUT_SEC", -1.0)

    # Position arrived, but azimuth velocity stays high forever.
    tcs = _VelGateTCS(settle_after=10**9)
    agent = _make_agent(tcs)
    session = _FakeSession(status="running")
    params = {"scan_params": {"body": "x"}, "scheduled_t0_unix": None}

    ok, msg = _run(
        agent._dispatch_scan_process(
            session, params, build_fn=_canned_build, job="source_scan", tcs=tcs
        )
    )

    assert ok is False
    assert "timed out" in msg.lower()
    kinds = [c[0] for c in tcs.calls]
    assert "scan_pattern" not in kinds, "must not POST while the axes are moving"


def test_axes_stopped_requires_both_velocities_present_and_below_tol():
    """Unit-check the helper the gate uses: both axis velocities must be present
    AND below TCS_SPEED_TOL. A missing velocity is 'not known to be stopped'."""
    agent = _make_agent(_FakeTCS())
    tol = _AGENT.TCS_SPEED_TOL
    assert agent._axes_stopped(
        {"Azimuth current velocity": 0.0, "Elevation current velocity": 0.0})
    assert agent._axes_stopped(
        {"Azimuth current velocity": tol / 2, "Elevation current velocity": -tol / 2})
    # One axis still moving.
    assert not agent._axes_stopped(
        {"Azimuth current velocity": 0.5, "Elevation current velocity": 0.0})
    # Velocity absent (e.g. the broadcast, which carries no velocity).
    assert not agent._axes_stopped({"Azimuth current velocity": 0.0})
    assert not agent._axes_stopped({})


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
    stale_build = lambda **k: _canned_build(**k, _start_time=k["now_unix"] - 200)

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


def test_dispatch_runner_aborts_in_slew_settle_window(sync_reactor):
    """Regression (HIGH): an abort landing AFTER the slew-arrival gate breaks but
    BEFORE the /path POST must not POST the scan. The gate's final velocity read is
    off-reactor, on which the session can flip to 'stopping'; without the re-check
    the runner would POST the cancelled scan (then the completion loop sends
    /abort, racing /path at the ACU). Assert /path is NEVER POSTed and the runner
    routes through _safe_abort."""
    session = _FakeSession(status="running")

    class _AbortAtSettleTCS(_FakeTCS):
        # First status read reports the axes already stopped (so the gate breaks)
        # AND flips the session to 'stopping', landing the abort exactly in the
        # post-break, pre-POST window.
        def get_status(self):
            self._poll += 1
            if self._poll == 1:
                session.status = "stopping"
            return {
                "Qty of free program track stack positions": 9999,
                "Azimuth current velocity": 0.0,
                "Elevation current velocity": 0.0,
                "Azimuth mode": "ProgramTrack",
                "Elevation mode": "ProgramTrack",
            }

    tcs = _AbortAtSettleTCS()
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
