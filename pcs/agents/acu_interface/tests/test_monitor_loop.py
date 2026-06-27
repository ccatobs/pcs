"""Execution coverage for the always-on ``monitor`` Process's status read.

Background, the bug this guards
---------------------------------
The ``monitor`` Process is on by default (NOT gated by ``azel_lock``) and polls
the ACU. Before the fix it read ``self.acu_read.get_status()`` with a bare
``yield``, ``get_status`` returns a plain dict, so the synchronous ``requests``
GET ran ON THE REACTOR THREAD. With the Pacemaker throttle commented out, a
slow-but-alive TCS froze the reactor up to the 30 s HTTP read timeout per poll,
stalling the 200 Hz broadcast and every slew/completion gate.

The fix moves that GET off the reactor (``deferToThread``). The handlers
(priming guard + in-loop) trap ``SystemExit`` (``aculib`` does ``sys.exit(-1)`` on
``ConnectionError``) so a hard ACU drop logs + continues instead of tearing down
the monitor (a bare ``except Exception`` does NOT catch ``SystemExit``).

What this file proves, by EXECUTING ``ACUAgent.monitor``
---------------------------------------------------------
``_get_status`` is an un-invokable nested closure, so we drive ``monitor()``
(priming ``_get_status()`` executes the off-reactor GET, one loop turn executes
the in-loop handler):

1. The status read is dispatched via ``deferToThread``, not inline on the
   reactor thread (executed not source-inspected).
2. A ``SystemExit`` from ``get_status`` is CAUGHT (priming guard + in-loop
   handler): the monitor publishes ``acu_error`` / marks disconnected and exits
   cleanly. Negative control: a bare ``except Exception`` handler lets the
   ``SystemExit`` escape, documenting why the handler widens to
   ``(Exception, SystemExit)``.

Harness, same stub as ``test_dispatch_runner`` (runs on Windows + WSL): stub the
three ``ocs`` names, drive ``@inlineCallbacks`` synchronously (``deferToThread`` ->
a RECORDING pass-through via ``maybeDeferred``, which captures ``SystemExit`` as a
``Failure`` like the real one; ``dsleep`` -> fired no-op). A fake session flips
``running -> done`` so the loop turns once.
"""

import importlib.util
import sys
import types

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


class _Log:
    def info(self, *a, **k):
        pass

    warn = warning = error = debug = critical = info


class _FakeSession:
    """A monitor session whose ``status`` the test controls. ``monitor`` reassigns
    ``.data`` itself (agent.py:387), so a plain settable attribute is enough."""

    def __init__(self, status="running"):
        self.status = status
        self.data = {}


class _RecordingThreads:
    """Stand-in for ``agent.threads``: records every ``deferToThread`` call and
    runs the fn via ``maybeDeferred`` (which captures ``SystemExit`` as a Failure,
    matching the real ``deferToThread``)."""

    def __init__(self):
        self.deferred_calls = []

    def deferToThread(self, fn, *a, **k):
        self.deferred_calls.append(fn)
        return defer.maybeDeferred(fn, *a, **k)


def _make_monitor_agent(get_status):
    """A real ``ACUAgent`` carrying only what ``monitor`` touches up to / through
    its status read. ``self.acu_read.get_status`` is the supplied callable."""
    a = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    a.log = _Log()
    a.platform_type = "latp"
    a.scan_params = {}
    a.acu_read = types.SimpleNamespace(get_status=get_status)
    a.agent = types.SimpleNamespace(publish_to_feed=lambda *a, **k: None)
    return a


@pytest.fixture
def sync_reactor(monkeypatch):
    """Drive ``monitor`` synchronously and hand it the recording ``threads``."""
    rec = _RecordingThreads()
    monkeypatch.setattr(_AGENT, "threads", rec)
    monkeypatch.setattr(_AGENT, "dsleep", lambda *a, **k: defer.succeed(None))
    return rec


def _drain(d):
    """Collect a synchronously-fired Deferred's result/Failure (do not re-raise,
    the monitor is expected to RETURN cleanly even on the SystemExit path)."""
    out = []
    d.addBoth(out.append)
    assert out, "monitor Deferred did not fire synchronously"
    return out[0]


# ---------------------------------------------------------------------------
# 1. The status read runs OFF the reactor (via deferToThread).
# ---------------------------------------------------------------------------


def test_monitor_get_status_runs_off_reactor(sync_reactor):
    """Drive ``monitor`` with a recorder ``get_status`` and a session that is
    already 'done' (so the priming ``_get_status()`` runs the off-reactor GET but
    the loop turns zero times). Assert the GET was dispatched THROUGH
    ``deferToThread``, i.e. it did not run inline on the reactor thread (executed)."""
    polls = []

    def _get_status():
        polls.append(1)
        return {"Azimuth current position": 1.0}

    agent = _make_monitor_agent(_get_status)
    # 'done' from the start: the priming call still runs (executing the GET), then
    # ``while session.status in ['running']`` is immediately false -> no loop
    # iteration -> we never reach the brittle field-mapping block.
    session = _FakeSession(status="done")

    result = _drain(agent.monitor(session, {}))

    assert not isinstance(result, Failure), f"monitor raised: {result}"
    assert polls, "get_status was never called"
    # The decisive assertion: the recorder saw the bound get_status go through
    # deferToThread. A bare ``yield self.acu_read.get_status()`` would have
    # called it inline and the recorder would be empty.
    assert agent.acu_read.get_status in sync_reactor.deferred_calls, \
        "monitor's get_status did NOT run via deferToThread (reactor-block regression)"


# ---------------------------------------------------------------------------
# 2. A SystemExit from get_status is caught, not propagated.
# ---------------------------------------------------------------------------


def test_monitor_survives_systemexit_from_get_status(sync_reactor):
    """``aculib.get_status`` does ``sys.exit(-1)`` on a ConnectionError. The
    monitor must CATCH that ``SystemExit`` (priming guard + in-loop handler),
    mark disconnected / publish ``acu_error``, and exit cleanly, never
    let it tear the Process down. Drive: get_status raises SystemExit on the
    priming call AND the first in-loop poll, then the session flips to 'done'."""
    published = []
    poll = {"n": 0}
    session = _FakeSession(status="running")

    def _get_status():
        poll["n"] += 1
        # 1st call = priming; 2nd = in-loop. After the in-loop raise
        # is handled, flip to 'done' so the loop exits after exactly one turn.
        if poll["n"] >= 2:
            session.status = "done"
        raise SystemExit("aculib sys.exit(-1) on ConnectionError")

    agent = _make_monitor_agent(_get_status)
    agent.agent = types.SimpleNamespace(
        publish_to_feed=lambda feed, block: published.append((feed, block)))

    result = _drain(agent.monitor(session, {}))

    assert not isinstance(result, Failure), \
        f"SystemExit escaped the monitor (the handlers did not catch it): {result}"
    # Got past the priming guard AND through one in-loop handler.
    assert poll["n"] >= 2, "monitor did not reach the in-loop SystemExit handler"
    assert session.data.get("connected") is False, \
        "monitor did not mark the session disconnected on the SystemExit path"
    assert any(feed == "acu_error" for feed, _ in published), \
        "in-loop handler did not publish an acu_error block"


def test_control_bare_except_lets_systemexit_escape(sync_reactor):
    """NEGATIVE CONTROL: a monitor-like loop whose in-loop handler is a bare
    ``except Exception`` (the old narrow form) lets a ``SystemExit`` ESCAPE, this
    is exactly what the handler's widening to ``(Exception, SystemExit)`` prevents. We
    reproduce the minimal loop shape locally (driving the real ``monitor`` with a
    narrowed handler would require editing the agent), so the control documents
    the necessity of the widening deterministically."""

    @defer.inlineCallbacks
    def _bare_except_loop(session, get_status):
        # Mirror the monitor's in-loop try/except, but with the OLD narrow handler.
        while session.status in ["running"]:
            try:
                yield sync_reactor.deferToThread(get_status)
            except Exception:  # noqa: BLE001 - deliberately NOT (Exception, SystemExit)
                session.status = "done"
                continue
        return True

    session = _FakeSession(status="running")

    def _se():
        raise SystemExit("boom")

    result = _drain(_bare_except_loop(session, _se))

    assert isinstance(result, Failure) and result.type is SystemExit, (
        "control did not reproduce the escape: a bare ``except Exception`` should "
        f"let SystemExit propagate, got {result!r}")
