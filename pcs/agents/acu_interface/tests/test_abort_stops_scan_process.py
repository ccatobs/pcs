"""Execution coverage for the standalone ``abort`` Task stopping running scans.

Background, the bug this guards
---------------------------------
Two abort paths:

* NORMAL, stopping a running typed-scan **Process** sets its session to
  ``'stopping'``; the scan's dispatch loop sends its own Go TCS ``/abort`` and
  RETURNS, releasing ``azel_lock``. Already works.
* The standalone ``abort`` **Task** used to ONLY send a bare ``/abort``. Go TCS
  ``/abort`` cancel+Stops but does NOT ProgramTrackClear (telescope.go), so on an
  early abort the stack-drained latch never fires; absent the ``'stopping'``
  signal the scan Process kept spinning, holding ``azel_lock`` to its scan-end
  backstop.

The fix: the ``abort`` Task ALSO stops every running typed scan Process
(``self.agent.stop`` over ``SCAN_PROCESS_OPS``) so an out-of-band abort cannot
strand the lock.

What this file proves, by EXECUTING ``ACUAgent.abort``
-------------------------------------------------------
1. A running scan's session is flipped to ``'stopping'`` AND the bare ``/abort``
   still goes out.
2. NEGATIVE CONTROL: the OLD body (``_safe_abort`` only) leaves the scan
   ``'running'``. The lock would be stranded.
3. Safe with no scan running (every ``stop`` returns an error tuple, none raises).
4. Idempotent when a scan is already ``'stopping'`` (stopper not re-invoked).

Harness: ``OCSAgent.stop`` only touches ``self.tasks`` / ``processes`` /
``sessions`` / ``access_config`` / ``op.{stopper,stopper_blocking,min_privs}``, so
we build a real ``OCSAgent`` via ``__new__`` + the REAL ``register_process`` and a
``'none'`` access policy (``min_privs=0`` + ``password=None`` clears the privilege
gate). ``abort`` calls ``self.agent.stop(op)`` on the reactor (MainThread) thread,
so ``OpSession.set_status`` takes its synchronous in-reactor path. No real
reactor needed; ``sync_reactor`` drives the one off-reactor ``_safe_abort`` call.
``OpSession`` gets a stub ``app`` with a ``.log`` (``app=None`` makes
``add_message`` raise). WSL-only: needs the real ``ocs.ocs_agent`` (shadowed on
Windows), so the module skip-guards like its siblings.
"""

import importlib.util
import sys
import types

import pytest


# --- Real ocs framework required (skip-guard exactly like the sibling files).
_AGENT = None
_IMPORT_ERR = None
try:
    if importlib.util.find_spec("ocs.ocs_agent") is None:
        raise ImportError("ocs.ocs_agent not importable (single-file REST client shadows it)")
    import ocs
    from ocs import access
    from ocs.ocs_agent import OpSession

    from twisted.internet import defer
    from twisted.internet.defer import Deferred
    from twisted.python.failure import Failure

    from pcs.agents.acu_interface import agent as _AGENT
except Exception as exc:  # pragma: no cover - environment-dependent
    _IMPORT_ERR = exc

pytestmark = pytest.mark.skipif(
    _AGENT is None, reason=f"real ocs framework not importable ({_IMPORT_ERR})"
)


class _Log:
    def info(self, *a, **k):
        pass

    warn = warning = error = debug = critical = info


class _App:
    """Stub WAMP app: ``OpSession.add_message`` reads ``app.log``."""

    def __init__(self):
        self.log = _Log()


class _FakeTCS:
    """Records the ``/abort`` so the test can assert the bare abort still fires."""

    def __init__(self):
        self.aborted = False

    def abort(self):
        self.aborted = True
        return {"status": "ok"}


def _make_ocs_agent():
    """A minimal real ``OCSAgent`` carrying only what ``stop()`` reads."""
    from ocs.ocs_agent import OCSAgent

    a = OCSAgent.__new__(OCSAgent)
    a.log = _Log()
    a.tasks = {}
    a.processes = {}
    a.sessions = {}
    a.access_config = access.agent_get_policy_default(None)
    return a


def _simple_process_stop(session, params):
    """Non-blocking stopper: flips a running session to 'stopping', returns a
    Deferred (what _stop_helper expects for a non-blocking stopper)."""
    if session.status == "running":
        session.set_status("stopping")
    return Deferred()


def _make_acu_agent(ocs_agent, tcs):
    """A real ``ACUAgent`` with just enough state to EXECUTE ``abort``."""
    a = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    a.log = _Log()
    a.agent = ocs_agent
    a._make_tcs = lambda: tcs  # avoid needing acu_conf/certs
    return a


def _register_scans(ocs_agent):
    """Register the four typed scans as genuine non-blocking Processes."""
    for op in _AGENT.SCAN_PROCESS_OPS:
        ocs_agent.register_process(
            op, lambda s, p: None, _simple_process_stop,
            blocking=False, min_privs=0)


@pytest.fixture
def sync_reactor(monkeypatch):
    """Run the @inlineCallbacks ``abort`` body synchronously: ``deferToThread``
    runs the fn inline; ``dsleep`` is an already-fired no-op. ``self.agent.stop``
    runs directly on this (MainThread = reactor-context) thread, so
    ``set_status`` takes its synchronous in-reactor path."""
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


# ---------------------------------------------------------------------------
# 1. abort stops a running scan Process (releases the lock) AND sends /abort.
# ---------------------------------------------------------------------------


def test_abort_stops_running_scan_releases_lock(sync_reactor):
    """A typed scan is running; ``abort`` flips its session to 'stopping' (the
    signal its dispatch loop watches to release ``azel_lock``) AND still sends the
    bare Go TCS /abort. EXECUTES ``ACUAgent.abort``."""
    ocs_agent = _make_ocs_agent()
    _register_scans(ocs_agent)
    app = _App()
    running = OpSession(0, "source_scan", status="running", app=app)
    ocs_agent.sessions["source_scan"] = running

    tcs = _FakeTCS()
    agent = _make_acu_agent(ocs_agent, tcs)
    session = OpSession(1, "abort", status="running", app=app)

    ok, msg = _run(agent.abort(session, {}))

    assert ok is True
    assert running.status == "stopping", \
        "abort did not stop the running scan; azel_lock would be stranded"
    assert tcs.aborted, "abort did not also send the bare Go TCS /abort"


# ---------------------------------------------------------------------------
# 2. NEGATIVE CONTROL: the OLD body (no stop loop) leaves the scan running.
# ---------------------------------------------------------------------------


def test_old_abort_leaves_scan_running(sync_reactor):
    """Reconstruct the OLD ``abort`` body (only ``_safe_abort``, no stop loop) and
    drive it against the same running scan: its session stays 'running', the
    lock is stranded. Proves the fix is load-bearing, deterministically."""
    ocs_agent = _make_ocs_agent()
    _register_scans(ocs_agent)
    app = _App()
    running = OpSession(0, "source_scan", status="running", app=app)
    ocs_agent.sessions["source_scan"] = running

    tcs = _FakeTCS()
    agent = _make_acu_agent(ocs_agent, tcs)

    @defer.inlineCallbacks
    def _old_abort_body():
        # The pre-fix body: bare /abort only, no SCAN_PROCESS_OPS stop loop.
        ok = yield _AGENT.threads.deferToThread(agent._safe_abort, agent._make_tcs())
        return (True, "old") if ok else (False, "old-fail")

    # _safe_abort needs a real send; give it the recording fake.
    agent._safe_abort = lambda t: (t.abort() or True)

    ok, _ = _run(_old_abort_body())

    assert ok is True
    assert tcs.aborted, "control sanity: the bare /abort should still fire"
    assert running.status == "running", \
        "OLD body unexpectedly stopped the scan; control is vacuous"


# ---------------------------------------------------------------------------
# 3. abort is safe with no scan running.
# ---------------------------------------------------------------------------


def test_abort_safe_with_no_scan_running(sync_reactor):
    """No scan active (all sessions None): ``abort`` returns success, and every
    ``self.agent.stop(op)`` returns an error tuple WITHOUT raising."""
    ocs_agent = _make_ocs_agent()
    _register_scans(ocs_agent)  # sessions all None
    tcs = _FakeTCS()
    agent = _make_acu_agent(ocs_agent, tcs)
    app = _App()
    session = OpSession(0, "abort", status="running", app=app)

    ok, msg = _run(agent.abort(session, {}))

    assert ok is True
    assert tcs.aborted
    # Sanity: stop() on a None-session op returns ERROR, no raise.
    status, _, _ = ocs_agent.stop("pong_scan")
    assert status == ocs.ERROR


# ---------------------------------------------------------------------------
# 4. abort is idempotent when a scan is already stopping.
# ---------------------------------------------------------------------------


def test_abort_idempotent_when_already_stopping(sync_reactor):
    """A scan already 'stopping': ``abort`` must not re-invoke the stopper or
    trip the forward-only status assert. The session stays 'stopping' and the op
    returns the 'already stopping' error tuple."""
    ocs_agent = _make_ocs_agent()
    _register_scans(ocs_agent)
    app = _App()
    already = OpSession(0, "daisy_scan", status="running", app=app)
    already.set_status("stopping")  # pre-set
    ocs_agent.sessions["daisy_scan"] = already

    # Spy: ensure the stopper is NOT called again for the already-stopping op.
    called = {"daisy_scan": 0}
    orig = _simple_process_stop

    def _spy_stop(session, params):
        called["daisy_scan"] += 1
        return orig(session, params)

    ocs_agent.processes["daisy_scan"].stopper = _spy_stop

    tcs = _FakeTCS()
    agent = _make_acu_agent(ocs_agent, tcs)
    session = OpSession(1, "abort", status="running", app=app)

    ok, msg = _run(agent.abort(session, {}))

    assert ok is True
    assert already.status == "stopping"
    assert called["daisy_scan"] == 0, \
        "stopper was re-invoked on an already-stopping op (not idempotent)"
    status, m, _ = ocs_agent.stop("daisy_scan")
    assert status == ocs.ERROR and "already" in m.lower()
