"""The agent checks astropy's Earth-orientation (IERS) data once, at start.

Every sky-to-horizon transform the typed scans and az_scan's Sun check make needs
UT1-UTC from astropy's IERS table. :func:`earth_orientation_note` asks the library
whether the table reaches :data:`EARTH_ORIENTATION_MIN_COVERAGE_DAYS` past now and
returns what the agent logs. It never raises: the library's verdict and any other
error of the check both become an error note, and only ``KeyboardInterrupt`` and
``SystemExit`` pass through. The agent logs the note from ``__init__`` and starts
either way. The library function is replaced by a stub, so nothing here depends on
this machine's network or on how old its table is.
"""

import importlib.util
import sys
import types

import pytest
from astropy.time import Time

from fyst_trajectories import PointingError
from pcs.agents.acu_interface import trajectory as traj

#: A start instant (2026-11-14T12:00:00 UTC) as Unix seconds.
NOW_UNIX = 1794657600.0

#: The last instant the stub's table covers.
COVERED_UNTIL = Time("2027-07-03T00:00:00", scale="utc")

#: The reason the stub's library refuses the table with.
REASON = (
    "astropy's Earth-orientation (IERS) table does not cover 2026-11-14 12:00:00.000 to "
    "2026-11-16 12:00:00.000: it runs from 1962-01-01 00:00:00.000 to 2026-10-02 00:00:00.000."
)


def _stub(monkeypatch, result=COVERED_UNTIL, raises=None):
    """Replace the library check in ``trajectory``; return the list of its calls."""
    calls = []

    def check(start, *, days=0.0):
        calls.append((start, days))
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(traj, "check_earth_orientation_data", check)
    return calls


def test_good_data_name_the_date_they_reach(monkeypatch):
    _stub(monkeypatch)

    ok, note = traj.earth_orientation_note(NOW_UNIX)

    assert ok is True
    assert "2027-07-03" in note


def test_the_span_asked_for_is_now_plus_the_coverage_constant(monkeypatch):
    calls = _stub(monkeypatch)

    traj.earth_orientation_note(NOW_UNIX)

    [(start, days)] = calls
    assert start.unix == pytest.approx(NOW_UNIX, abs=1e-6)
    assert start.scale == "utc"
    assert days == traj.EARTH_ORIENTATION_MIN_COVERAGE_DAYS == 2.0


def test_refused_data_give_the_reason_and_what_fails(monkeypatch):
    _stub(monkeypatch, raises=PointingError(REASON))

    ok, note = traj.earth_orientation_note(NOW_UNIX)

    assert ok is False
    assert REASON in note
    assert "typed scans" in note.lower()
    assert "az_scan Sun check" in note


@pytest.mark.parametrize(
    "error", [ValueError("Column year failed to convert"), RuntimeError("boom")]
)
def test_any_other_error_becomes_a_note_naming_it(monkeypatch, error):
    """Astropy reading a page that is not a table raises a plain ValueError; that, or
    anything else, is reported with its type and text instead of stopping the agent."""
    _stub(monkeypatch, raises=error)

    ok, note = traj.earth_orientation_note(NOW_UNIX)

    assert ok is False
    assert "could not run" in note
    assert type(error).__name__ in note
    assert str(error) in note


def test_a_keyboard_interrupt_propagates(monkeypatch):
    _stub(monkeypatch, raises=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        traj.earth_orientation_note(NOW_UNIX)


# --- the agent ----------------------------------------------------------------------

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

needs_agent = pytest.mark.skipif(
    _AGENT is None, reason=f"agent module not importable ({_IMPORT_ERR})"
)


class _Log:
    """Keeps each line by level, formatted from its keyword arguments as the agent's
    Twisted logger formats them, so a note passed as the format string itself fails."""

    def __init__(self):
        self.lines = {"info": [], "warn": [], "error": []}

    def info(self, fmt, **kw):
        self.lines["info"].append(fmt.format(**kw))

    def warn(self, fmt, **kw):
        self.lines["warn"].append(fmt.format(**kw))

    def error(self, fmt, **kw):
        self.lines["error"].append(fmt.format(**kw))

    def debug(self, fmt, **kw):
        pass

    warning = warn


class _Registry:
    """Stands in for the OCSAgent: keeps each registered operation's name."""

    def __init__(self):
        self.log = _Log()
        self.ops = set()

    def register_process(self, name, *a, **k):
        self.ops.add(name)

    register_task = register_process

    def register_feed(self, *a, **k):
        pass


def _start_agent(monkeypatch, verdict=None):
    """Construct the real ACUAgent with the start check returning ``verdict``; return
    the registry and the instants the check was asked about. With ``verdict`` None
    the real :func:`earth_orientation_note` runs and nothing is recorded."""
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
    asked = []

    def note(now_unix):
        asked.append(now_unix)
        return verdict

    if verdict is not None:
        monkeypatch.setattr(_AGENT, "earth_orientation_note", note)
    registry = _Registry()
    _AGENT.ACUAgent(registry, "acu_config.yaml", device="acu-sim")
    return registry, asked


#: Braces in a note pin the keyword style: Twisted reads a format string's braces.
BAD_NOTE = "IERS data {stale}: typed scans fail"
GOOD_NOTE = "IERS data reach {2027-07-03}"


@needs_agent
def test_the_agent_starts_and_logs_an_error_on_bad_data(monkeypatch):
    before = _AGENT.time.time()
    registry, asked = _start_agent(monkeypatch, (False, BAD_NOTE))
    after = _AGENT.time.time()

    assert registry.log.lines["error"] == [BAD_NOTE]
    assert BAD_NOTE not in registry.log.lines["info"]
    [now_unix] = asked
    assert before <= now_unix <= after
    # Status, the broadcast, go_to and abort need no astronomy and are still served.
    assert {"monitor", "broadcast", "go_to", "abort"} <= registry.ops


@needs_agent
def test_the_agent_logs_an_info_line_on_good_data(monkeypatch):
    registry, asked = _start_agent(monkeypatch, (True, GOOD_NOTE))

    assert GOOD_NOTE in registry.log.lines["info"]
    assert registry.log.lines["error"] == []
    assert len(asked) == 1


@needs_agent
def test_the_agent_starts_when_the_check_itself_fails(monkeypatch):
    _stub(monkeypatch, raises=ValueError("Column year failed to convert"))

    registry, _ = _start_agent(monkeypatch)

    [line] = registry.log.lines["error"]
    assert "could not run" in line
    assert not any("Earth-orientation" in info for info in registry.log.lines["info"])
    assert {"monitor", "broadcast", "go_to", "abort"} <= registry.ops
