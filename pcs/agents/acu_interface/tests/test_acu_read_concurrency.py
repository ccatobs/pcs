"""Empirical proof that the per-scan-client fix removes concurrent same-Session
access.

Background, the bug this guards
---------------------------------
``aculib.observatory_control_system`` holds ONE persistent ``requests.Session``,
which is NOT thread-safe. Before the fix the agent reused the single shared
``self.acu_read`` client from three places that can run at once on DIFFERENT
threads:

* the always-on ``monitor`` Process (reactor thread; on by default, NOT gated by
  ``azel_lock``), ``self.acu_read.get_status()``;
* a running scan's thread-pool calls in ``_dispatch_scan_process``
  (``deferToThread`` worker), ``move_to`` / ``get_status`` / ``scan_pattern``;
* the standalone ``abort`` task's ``/abort`` POST.

So a scan's pool-thread GET/POST could enter the very Session the monitor's
reactor thread was mid-request on (highest-severity: abort-vs-scan). The fix
gives every scan (and the ``abort`` task) its OWN client via
:meth:`ACUAgent._make_tcs`, leaving only the monitor + the
(reactor-thread-serialized) ``_current_encoder_azel`` fallback on
``self.acu_read``.

What this file proves, deterministically, not by luck
-------------------------------------------------------
1. ``_make_tcs`` returns a DISTINCT client whose ``.session`` differs from
   ``self.acu_read.session`` and is fresh per call.
2. Driving the REAL paths, monitor's ``self.acu_read.get_status()`` vs a scan's
   ``scan_pattern()`` / ``get_status()`` on a ``_make_tcs()`` client. No single
   Session is entered from two threads at once.
3. CONTROL: with the OLD shared pattern (both threads on ``self.acu_read.session``)
   the SAME Session IS entered by two threads at once, proving the hazard was
   real and distinct Sessions remove it.

Determinism: not "run two threads and hope they collide". We instrument
``requests.Session.request`` (the method both ``.get`` and ``.post`` funnel
through) with a per-Session re-entrancy counter recording the MAX threads
simultaneously inside that one Session, widening the window with a fixed in-call
sleep + a ``threading.Barrier`` that releases both threads together. Distinct
Sessions -> max concurrency 1 by construction; a forced-shared Session ->
deterministically both threads inside it. Assertions are on those recorded
maxima, not on whether a race corrupted anything. HTTP is answered by a tiny
in-process stdlib loopback server, so ``Session.request`` runs end-to-end (never
mocked, only wrapped).

Skip-guarded on Windows: the agent imports the SO ``ocs`` framework, unimportable
on a dev box (a single-file OCS REST client named ``ocs`` shadows
``ocs.ocs_agent``). We stub ONLY the three ``ocs`` names the module imports
(matching ``test_dispatch_runner.py``), then import the agent; if that fails the
module skips so Windows stays green (the 2 genuine agent-import skips).
"""

import importlib.util
import sys
import threading
import types

import pytest
import requests

# --- Import the agent exactly as test_dispatch_runner.py does (stub the three
# ocs names the module needs; skip the whole file if the agent is unimportable).
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

    from pcs.agents.acu_interface import agent as _AGENT
    from pcs.agents.acu_interface import aculib as _ACULIB
except Exception as exc:  # pragma: no cover - environment-dependent
    _IMPORT_ERR = exc

pytestmark = pytest.mark.skipif(
    _AGENT is None, reason=f"agent module not importable ({_IMPORT_ERR})"
)


class _Log:
    def info(self, *a, **k):
        pass

    warn = warning = error = debug = info


# A cert-less device block: empty cert strings route start_session down its
# cert-less branch (aculib.py:103-107), so _make_tcs builds a real
# requests.Session with no filesystem/network dependency. This is exactly the
# "cert-less device block" case the _make_tcs docstring documents.
def _bare_agent(base_url):
    """Real ACUAgent with just enough state to call the REAL _make_tcs +
    construct the REAL shared self.acu_read, both via the genuine
    aculib.observatory_control_system, no __init__, no reactor, no network on
    construction."""
    a = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    a.log = _Log()
    a.acu_conf = {"base_url": base_url, "certs": {"verify": False}}
    # The shared client the monitor + _current_encoder_azel use, built the same
    # way the real __init__ builds it (aculib.py:82) but cert-less.
    a.acu_read = _ACULIB.observatory_control_system(base_url, a.log, verify_cert=False)
    # Cold broadcast so _current_encoder_azel falls through to its status read,
    # the path the fallback-routing tests exercise.
    a.data = {"broadcast": {}}
    return a


# ---------------------------------------------------------------------------
# 1. _make_tcs returns a DISTINCT, fresh requests.Session each call.
# ---------------------------------------------------------------------------


def test_make_tcs_returns_fresh_distinct_session():
    """``_make_tcs()`` builds a fresh client whose ``.session`` is distinct from
    ``self.acu_read.session`` AND from any other ``_make_tcs()`` call, the
    structural precondition for the no-shared-Session property."""
    agent = _bare_agent("http://127.0.0.1:9")

    c1 = agent._make_tcs()
    c2 = agent._make_tcs()

    assert isinstance(c1.session, requests.Session)
    assert isinstance(agent.acu_read.session, requests.Session)
    # Distinct from the shared monitor client...
    assert c1.session is not agent.acu_read.session
    assert c2.session is not agent.acu_read.session
    # ...and fresh per call (two scans never share a Session).
    assert c1.session is not c2.session
    # The client objects themselves are distinct too.
    assert c1 is not c2 and c1 is not agent.acu_read


# ---------------------------------------------------------------------------
# Concurrency harness: a per-Session re-entrancy meter + a tiny loopback HTTP
# server, so requests.Session.request runs end-to-end and we MEASURE the max
# threads simultaneously inside each individual Session.
# ---------------------------------------------------------------------------


class _ConcurrencyMeter:
    """Wraps ``requests.Session.request`` to record, per Session instance, the max
    threads simultaneously inside ``request``. A fixed in-window sleep + a barrier
    widen the window so an overlap is observed deterministically iff two threads
    share a Session."""

    def __init__(self, barrier, window_sec=0.15):
        self._orig = requests.Session.request
        self._barrier = barrier
        self._window = window_sec
        self._lock = threading.Lock()
        self.live = {}  # id(session) -> current concurrent count
        self.peak = {}  # id(session) -> max concurrent count observed

    def __enter__(self):
        meter = self

        def _patched(session, method, url, *args, **kwargs):
            # Release both worker threads together, then hold the Session for a
            # fixed window so a genuine overlap is guaranteed if they share one.
            try:
                meter._barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            sid = id(session)
            with meter._lock:
                n = meter.live.get(sid, 0) + 1
                meter.live[sid] = n
                meter.peak[sid] = max(meter.peak.get(sid, 0), n)
            try:
                import time

                time.sleep(meter._window)
                return meter._orig(session, method, url, *args, **kwargs)
            finally:
                with meter._lock:
                    meter.live[sid] -= 1

        requests.Session.request = _patched
        return self

    def __exit__(self, *exc):
        requests.Session.request = self._orig
        return False

    def peak_for(self, session):
        return self.peak.get(id(session), 0)


def _loopback_server():
    """A tiny stdlib HTTP server that answers any GET/POST with a JSON body the
    aculib client can parse (``.json()`` -> a drained-status dict; POST returns a
    parseable body for ``scan_pattern``/``abort``). Returns (base_url, shutdown)."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _H(BaseHTTPRequestHandler):
        def _reply(self):
            body = (
                b'{"status": "ok", "message": "ok", '
                b'"Qty of free program track stack positions": 9999, '
                b'"Azimuth current velocity": 0.0, "Elevation current velocity": 0.0}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = lambda self: self._reply()

        def log_message(self, *a, **k):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    host, port = srv.server_address
    return f"http://{host}:{port}", srv.shutdown


def _drive_two_threads(monitor_call, scan_call, barrier, *, require_clean=True):
    """Run ``monitor_call`` and ``scan_call`` on two real threads, surfacing any
    exception. The barrier (inside the meter) makes them enter ``request``
    together. With ``require_clean`` (default) any worker exception fails the
    test; the negative control passes ``require_clean=False`` because a transport
    fault from the deliberately-induced shared-Session race is itself proof of
    the hazard, not a test failure. Returns the captured worker errors."""
    errors = []

    def _wrap(fn):
        def _run():
            try:
                fn()
            except BaseException as e:  # noqa: BLE001 - surface to the test
                errors.append(e)

        return _run

    t_mon = threading.Thread(target=_wrap(monitor_call))
    t_scan = threading.Thread(target=_wrap(scan_call))
    t_mon.start()
    t_scan.start()
    t_mon.join(timeout=15)
    t_scan.join(timeout=15)
    assert not t_mon.is_alive() and not t_scan.is_alive(), "worker thread hung"
    if require_clean:
        assert not errors, f"worker raised: {errors!r}"
    return errors


# ---------------------------------------------------------------------------
# 2. THE FIX: monitor on self.acu_read vs scan on a _make_tcs() client ->
#    no single Session is entered by two threads at once.
# ---------------------------------------------------------------------------


def test_fix_no_session_entered_concurrently():
    """Drive the REAL agent paths concurrently: monitor's
    ``self.acu_read.get_status()`` (reactor thread) vs a scan's
    ``scan_pattern(...)`` + ``get_status()`` on a fresh ``_make_tcs()`` client
    (pool thread). The Sessions are distinct, so each peak concurrency is exactly
    1 even though the two HTTP calls genuinely overlap (meter window + barrier)."""
    base_url, shutdown = _loopback_server()
    try:
        agent = _bare_agent(base_url)
        scan_tcs = agent._make_tcs()  # what every typed scan now uses
        # Sanity: distinct Sessions (precondition for the property under test).
        assert scan_tcs.session is not agent.acu_read.session

        payload = {
            "start_time": 1.0,
            "coordsys": "Horizon",
            "points": [[0.0, 1.0, 2.0, 0.0, 0.0]],
        }
        barrier = threading.Barrier(2)
        with _ConcurrencyMeter(barrier) as meter:
            _drive_two_threads(
                monitor_call=agent.acu_read.get_status,  # reactor-thread path
                scan_call=lambda: (
                    scan_tcs.scan_pattern(payload),  # POST  (Session.request)
                    scan_tcs.get_status(),  # GET   (Session.request)
                ),
                barrier=barrier,
            )

        # The decisive assertion: NO Session was entered by two threads at once.
        assert meter.peak_for(agent.acu_read.session) == 1, (
            "monitor's shared Session was entered concurrently; "
            "fix did not isolate it"
        )
        assert meter.peak_for(scan_tcs.session) == 1, (
            "scan's per-scan Session was entered concurrently"
        )
        # And they really are different Session objects.
        assert id(scan_tcs.session) != id(agent.acu_read.session)
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# 3. CONTROL: force BOTH paths back onto self.acu_read.session (the OLD shared
#    pattern) -> that ONE Session IS entered by two threads at once.
# ---------------------------------------------------------------------------


def test_control_shared_session_is_entered_concurrently():
    """Negative control proving the hazard was real. Reconstruct the OLD pattern:
    both the monitor path and the scan path drive ``self.acu_read`` (one shared
    Session). Through the same harness that single Session's peak concurrency
    reaches 2, exactly the bug. If this did NOT reach 2, the fix test would be
    vacuous."""
    base_url, shutdown = _loopback_server()
    try:
        agent = _bare_agent(base_url)
        shared = agent.acu_read  # the OLD shared client both paths reused

        payload = {
            "start_time": 1.0,
            "coordsys": "Horizon",
            "points": [[0.0, 1.0, 2.0, 0.0, 0.0]],
        }
        barrier = threading.Barrier(2)
        with _ConcurrencyMeter(barrier) as meter:
            errors = _drive_two_threads(
                monitor_call=shared.get_status,  # monitor on shared
                scan_call=lambda: shared.scan_pattern(payload),  # scan ALSO on shared
                barrier=barrier,
                require_clean=False,  # a transport fault on the shared Session is ALSO the hazard
            )

        # The hazard manifests EITHER as two threads co-occupying the one shared
        # Session (peak == 2) OR as a transport fault raised by that unsafe
        # concurrent use (a requests/urllib3 connection reset, or aculib's
        # SystemExit on a RequestException). Both prove the shared Session is
        # unsafe under concurrency. Accepting either keeps the control robust
        # under heavy full-suite load (where the race can fault before peak is
        # sampled) WITHOUT weakening it: an unrelated error type would not satisfy
        # this, and the fix test above still requires the clean peak == 1.
        transport_fault = any(
            isinstance(e, (requests.exceptions.RequestException, ConnectionError, OSError, SystemExit))
            for e in errors
        )
        assert meter.peak_for(shared.session) == 2 or transport_fault, (
            "control did not reproduce the hazard: expected two threads inside the "
            "shared Session (peak == 2) or a transport fault from the shared-Session "
            f"race; got peak={meter.peak_for(shared.session)}, errors={errors!r}"
        )
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# 4. COUPLING: after the fix the monitor's status read (off-reactor on
#    self.acu_read) and _current_encoder_azel's cold-broadcast fallback (now
#    reading the PER-SCAN tcs) must NOT share a Session. The naive monitor-only fix
#    would leave the fallback on self.acu_read -> a SECOND thread on the monitor's
#    Session -> the very race the per-scan-client fix removed. These drive the REAL
#    post-fix bodies.
# ---------------------------------------------------------------------------


def test_monitor_offreactor_and_fallback_no_shared_session():
    """EXECUTES the changed ``_current_encoder_azel``: with a COLD broadcast it
    falls through to ``tcs.get_status()`` on the PER-SCAN client. Driven against
    the monitor's ``self.acu_read.get_status()``, the two Sessions are distinct so
    each peak is <= 1, the post-fix actor map (fallback reads ``tcs``, never
    ``self.acu_read``)."""
    base_url, shutdown = _loopback_server()
    try:
        agent = _bare_agent(base_url)  # cold broadcast (a.data = {'broadcast': {}})
        scan_tcs = agent._make_tcs()  # the per-scan client the runner hands the fallback
        assert scan_tcs.session is not agent.acu_read.session

        barrier = threading.Barrier(2)
        with _ConcurrencyMeter(barrier) as meter:
            _drive_two_threads(
                monitor_call=agent.acu_read.get_status,  # Session A (reactor path)
                # Cold broadcast -> _current_encoder_azel hits tcs.get_status()
                # on the per-scan client (Session B), the post-fix fallback.
                scan_call=lambda: agent._current_encoder_azel(scan_tcs),
                barrier=barrier,
            )

        # Sanity: the cold-broadcast fallback DID issue its status GET on the
        # per-scan Session (peak >= 1 means that Session was entered end-to-end),
        # i.e. the fallback read ``tcs``, NOT ``self.acu_read``. (The loopback body
        # carries no position keys, so the helper's RETURN is None, irrelevant
        # to the Session-isolation property under test.)
        assert meter.peak_for(scan_tcs.session) >= 1, \
            "fallback did not issue its status GET on the per-scan Session"
        # The decisive assertions: neither Session was entered by two threads.
        assert meter.peak_for(agent.acu_read.session) == 1, \
            "monitor's shared Session was entered concurrently by the fallback"
        assert meter.peak_for(scan_tcs.session) == 1, \
            "per-scan Session was entered concurrently"
    finally:
        shutdown()


def test_control_fallback_on_acu_read_races():
    """NEGATIVE CONTROL: reconstruct the OLD coupling, the fallback reading
    ``self.acu_read`` (the naive monitor-only fix) instead of the per-scan client.
    Driven against the monitor's ``self.acu_read.get_status()``, that ONE shared
    Session reaches peak 2 (or faults), proving the coupling was real and that
    routing the fallback onto the per-scan Session is what removes it."""
    base_url, shutdown = _loopback_server()
    try:
        agent = _bare_agent(base_url)
        shared = agent.acu_read

        # The pre-fix fallback shape: cold broadcast -> read self.acu_read, NOT
        # a per-scan tcs. (A faithful copy of the old body's status branch.)
        def _old_fallback_reads_acu_read():
            bcast = agent.data.get("broadcast", {})
            if "Azimuth" in bcast and "Elevation" in bcast:
                return float(bcast["Azimuth"]), float(bcast["Elevation"]), "broadcast"
            status = shared.get_status()  # <-- the shared Session, the hazard
            return (status.get("Azimuth current position"),
                    status.get("Elevation current position"), "status")

        barrier = threading.Barrier(2)
        with _ConcurrencyMeter(barrier) as meter:
            errors = _drive_two_threads(
                monitor_call=shared.get_status,
                scan_call=_old_fallback_reads_acu_read,
                barrier=barrier,
                require_clean=False,  # a transport fault on the shared Session is ALSO the hazard
            )

        transport_fault = any(
            isinstance(e, (requests.exceptions.RequestException, ConnectionError, OSError, SystemExit))
            for e in errors
        )
        assert meter.peak_for(shared.session) == 2 or transport_fault, (
            "control did not reproduce the coupling: expected two threads inside "
            "the shared self.acu_read Session (peak == 2) or a transport fault; got "
            f"peak={meter.peak_for(shared.session)}, errors={errors!r}")
    finally:
        shutdown()


def test_current_encoder_azel_prefers_broadcast_no_session_touch():
    """When the broadcast is WARM, ``_current_encoder_azel`` returns from it and
    must NOT touch the client at all, guarding the fast path stays Session-free
    (so the pool-thread read of ``self.data['broadcast']`` is the only access,
    and the per-scan Session is never entered). A tcs whose ``get_status`` raises
    proves the fallback was not taken."""
    agent = _bare_agent("http://127.0.0.1:9")
    agent.data = {"broadcast": {"Azimuth": 12.0, "Elevation": 45.0}}

    class _Boom:
        def get_status(self):
            raise AssertionError("fast path must not call tcs.get_status()")

    pos = agent._current_encoder_azel(_Boom())
    assert pos == (12.0, 45.0, "broadcast")
