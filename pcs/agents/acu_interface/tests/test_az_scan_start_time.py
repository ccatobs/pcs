"""az_scan start_time: both documented forms, then the Go TCS minimum-lead floor.

The Go TCS's /azimuth-scan decodes start_time as a number only and refuses a start
under 9.8 s out (checkProgramTrackStartTime); from TCS b5666ae on, a 0 means about
10 s after its move to the range start, and older builds refuse it. The task
converts an ISO-8601 UTC string to Unix seconds, floors a relative start (below
TCS_RELATIVE_TIME_MAX) other than 0 to SCAN_DISPATCH_BUFFER_SEC and an absolute one
to now plus that buffer, passes 0 unchanged, and fails without sending on any other
start_time, on a missing one or on a key it does not take; it runs in a worker
thread.

Drives the real ACUAgent.az_scan with a recording fake as the aculib client;
without the SO ocs framework the agent's three ocs imports are stubbed.
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

    from pcs.agents.acu_interface import agent as _AGENT
except Exception as exc:  # pragma: no cover - environment-dependent
    _IMPORT_ERR = exc

pytestmark = pytest.mark.skipif(
    _AGENT is None, reason=f"agent module not importable ({_IMPORT_ERR})"
)

#: An ordinary azimuth scan; each test supplies only ``start_time``.
SCAN = dict(
    azimuth_range=[100.0, 140.0],
    elevation=50.0,
    num_scans=2,
    turnaround_time=2.0,
    speed=0.5,
)

#: 2099-01-01T00:00:00 UTC as Unix seconds, written out rather than computed so
#: the conversion is checked against an independent value.
UNIX_2099 = 4070908800.0


class _Log:
    def info(self, *a, **k):
        pass

    warn = warning = error = debug = info


class _Resp:
    """The OCS API's answer to an accepted command, or with ``body`` the one it
    relays, also with HTTP 200, for a refused one."""

    status_code = 200
    text = "ok"

    def __init__(self, body=None):
        self.body = {"status": "ok"} if body is None else body

    def json(self):
        return self.body


class _FakeLock:
    def __init__(self):
        self.job = None

    @contextmanager
    def acquire_timeout(self, timeout, job=None):
        self.job = job
        yield True


class _RecordingTCS:
    """Stands in for the aculib client and records each azimuth_scan call."""

    def __init__(self):
        self.scans = []
        self.reply = _Resp()

    def azimuth_scan(self, **kwargs):
        self.scans.append(kwargs)
        return self.reply


@pytest.fixture
def az_scan_agent(monkeypatch):
    """An agent for the real az_scan, with a recording fake as its aculib client."""
    tcs = _RecordingTCS()
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system",
                        lambda *a, **k: tcs)
    # These tests run at the wall clock, so a site without Sun avoidance keeps
    # them independent of where the Sun is; test_az_scan_sun_check.py covers it.
    no_sun_site = _AGENT.get_fyst_site(sun_avoidance_enabled=False)
    monkeypatch.setattr(_AGENT, "get_fyst_site", lambda *a, **k: no_sun_site)
    agent = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    agent.log = _Log()
    agent.azel_lock = _FakeLock()
    agent.acu_conf = {
        "base_url": "http://tcs.invalid",
        "certs": {"server_cert": "", "client_cert": "", "client_key": "",
                  "verify": False},
    }
    return agent, tcs


@pytest.fixture
def sent_start_time(az_scan_agent):
    """Run the real az_scan with a ``start_time``; return what reached the TCS."""
    agent, tcs = az_scan_agent

    def run(start_time):
        ok, _ = agent.az_scan(None, {"scan_params": dict(SCAN, start_time=start_time)})
        assert ok is True
        sent = tcs.scans[-1]
        # Every other scan parameter passes through untouched.
        assert {k: v for k, v in sent.items() if k != "start_time"} == SCAN
        return sent["start_time"]

    return run


def test_absolute_start_in_the_past_is_floored_to_now_plus_buffer(sent_start_time):
    before = time.time()
    sent = sent_start_time(before - 3600.0)
    after = time.time()
    buffer = _AGENT.SCAN_DISPATCH_BUFFER_SEC
    assert before + buffer <= sent <= after + buffer


def test_absolute_start_in_the_future_is_kept(sent_start_time):
    start = time.time() + 3600.0
    assert sent_start_time(start) == start


@pytest.mark.parametrize("start_time, sent", [
    (0.0, 0.0), (5.0, 10.0), (-5.0, 10.0), (10.0, 10.0), (30.0, 30.0), (60.0, 60.0),
])
def test_relative_start_is_floored_to_the_buffer_except_zero(sent_start_time, start_time, sent):
    """Below TCS_RELATIVE_TIME_MAX the value is seconds from now, so the floor
    is the buffer itself (10 s), in the same relative units; 0, the TCS's
    "start after the move", goes through unchanged."""
    assert _AGENT.SCAN_DISPATCH_BUFFER_SEC == 10.0
    assert sent_start_time(start_time) == sent


def test_zero_start_refused_by_an_older_build_names_the_build(az_scan_agent):
    """A Go TCS before b5666ae refuses a start of 0 as too soon; the failure
    says that 0 needs b5666ae or later and to give a start time instead."""
    pytest.importorskip("fastapi", reason="mock_tcs needs FastAPI")
    from pcs.agents.acu_interface.tests import mock_tcs

    agent, tcs = az_scan_agent
    tcs.reply = _Resp(mock_tcs.gateway_refusal(mock_tcs.GO_START_TOO_SOON))

    ok, msg = agent.az_scan(None, {"scan_params": dict(SCAN, start_time=0)})

    assert ok is False
    assert mock_tcs.GO_START_TOO_SOON in msg and "b5666ae" in msg, msg
    assert tcs.scans[-1]["start_time"] == 0.0


@pytest.mark.parametrize("start_time, message", [
    (60.0, "program track starts too soon (< 10 seconds from now)"),
    (0, "busy"),
], ids=["nonzero-start-refused", "zero-start-busy"])
def test_the_zero_start_hint_needs_a_zero_start_refused_as_too_soon(az_scan_agent, start_time, message):
    """The hint about b5666ae goes only on a start of 0 refused as too soon: a
    refused nonzero start, or a 0 met by a busy TCS, carries none."""
    agent, tcs = az_scan_agent
    tcs.reply = _Resp({"status": "error", "message": message})

    ok, msg = agent.az_scan(None, {"scan_params": dict(SCAN, start_time=start_time)})

    assert ok is False
    assert message in msg and "b5666ae" not in msg, msg


def test_iso_string_with_z_is_sent_as_unix_seconds(sent_start_time):
    """The documented ``%Y-%m-%dT%H:%M:%SZ`` form reaches the TCS as a number."""
    sent = sent_start_time("2099-01-01T00:00:00Z")
    assert isinstance(sent, float)
    assert sent == UNIX_2099


def test_naive_iso_string_is_read_as_utc(sent_start_time, monkeypatch):
    """A string with no offset is UTC, not the host's local time. Where it can,
    the test moves the host off UTC first, so a UTC host cannot hide the error."""
    try:
        with monkeypatch.context() as m:
            if hasattr(time, "tzset"):
                m.setenv("TZ", "America/Santiago")
                time.tzset()
            sent = sent_start_time("2099-01-01T00:00:00")
    finally:
        if hasattr(time, "tzset"):
            time.tzset()
    assert sent == UNIX_2099


def test_iso_string_in_the_past_is_floored_like_a_number(sent_start_time):
    before = time.time()
    sent = sent_start_time("2020-01-01T00:00:00Z")
    after = time.time()
    buffer = _AGENT.SCAN_DISPATCH_BUFFER_SEC
    assert before + buffer <= sent <= after + buffer


@pytest.mark.parametrize("start_time", ["tomorrow", "2099-13-01T00:00:00Z", [60.0],
                                        float("nan"), float("inf")])
def test_malformed_start_time_fails_the_task_and_sends_nothing(az_scan_agent,
                                                               start_time):
    """A start_time that is neither a number nor an ISO-8601 string fails the
    task with a message naming it, rather than raising out of it."""
    agent, tcs = az_scan_agent
    ok, msg = agent.az_scan(None, {"scan_params": dict(SCAN, start_time=start_time)})
    assert ok is False
    assert "start_time" in msg
    assert tcs.scans == []


@pytest.mark.parametrize("scan_params, named", [
    (SCAN, "start_time is missing"),
    (dict(SCAN, start_time=60.0, mode="fast"), "unexpected key(s) ['mode']"),
], ids=["start-time-missing", "unknown-key"])
def test_a_missing_or_unknown_key_fails_the_task_naming_it(az_scan_agent, scan_params, named):
    """aculib.azimuth_scan takes exactly the six keys and the OCS API requires
    start_time, so any other set fails the task, naming the key, and sends nothing."""
    agent, tcs = az_scan_agent
    ok, msg = agent.az_scan(None, {"scan_params": dict(scan_params)})
    assert ok is False
    assert named in msg and msg.endswith("not launched."), msg
    assert tcs.scans == []


#: How long (s) the stubbed Sun check advances the clock.
SLOW_SUN_CHECK_SEC = 1.0


def test_an_absolute_start_is_floored_after_the_sun_check(az_scan_agent, monkeypatch):
    """The Go TCS times the minimum lead from its receipt, so the floor comes after
    the Sun check: a slow check leaves the posted start the buffer past the post."""
    agent, tcs = az_scan_agent
    clock = [UNIX_2099]
    monkeypatch.setattr(_AGENT, "time",
                        types.SimpleNamespace(time=lambda: clock[0], sleep=time.sleep))
    sun_check = _AGENT.az_scan_sun_breach

    def slow_sun_check(**kwargs):
        clock[0] += SLOW_SUN_CHECK_SEC
        return sun_check(**kwargs)

    monkeypatch.setattr(_AGENT, "az_scan_sun_breach", slow_sun_check)
    posted_at = []
    record = tcs.azimuth_scan

    def azimuth_scan(**kwargs):
        posted_at.append(clock[0])
        return record(**kwargs)

    tcs.azimuth_scan = azimuth_scan

    ok, msg = agent.az_scan(None, {"scan_params": dict(SCAN, start_time=UNIX_2099 - 3600.0)})

    assert ok is True, msg
    assert tcs.scans[-1]["start_time"] >= posted_at[-1] + _AGENT.SCAN_DISPATCH_BUFFER_SEC


class _Registry:
    """Stands in for the OCSAgent and keeps the keyword arguments of each registration."""

    log = _Log()

    def __init__(self):
        self.ops = {}

    def register_process(self, name, *a, **k):
        self.ops[name] = k

    register_task = register_process

    def register_feed(self, *a, **k):
        pass


def test_az_scan_is_registered_to_run_off_the_reactor(monkeypatch):
    """az_scan's Sun check and its HTTP post are synchronous, so the task runs in a
    worker thread (blocking=True), as go_to does."""
    datasets = [[name, name] for name in ("status", "pointing", "shutter", "hvac", "faults")]
    config = {
        "devices": {"acu-sim": {
            "base_url": "http://tcs.invalid",
            "streams": {"main": {"schema": "v0"}},
            "certs": {"server_cert": "", "client_cert": "", "client_key": ""},
        }},
        "datasets": {"latp": {"default_dataset": "status", "datasets": datasets}},
    }
    monkeypatch.setattr(_AGENT.aculib, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(_AGENT.aculib, "get_stream_schema", lambda *a, **k: {})
    monkeypatch.setattr(_AGENT.aculib, "observatory_control_system", lambda *a, **k: None)
    monkeypatch.setattr(_AGENT, "earth_orientation_note", lambda now_unix: (True, "ok"))
    registry = _Registry()

    _AGENT.ACUAgent(registry, "acu_config.yaml", device="acu-sim")

    assert registry.ops["az_scan"]["blocking"] is True
    assert registry.ops["go_to"]["blocking"] is True
