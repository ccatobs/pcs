"""Wiring guards: the agent + the dispatch-window centralization.

Two light-weight, date-independent concerns:

1. **Dispatch-window centralization (ocs-free).** Confirm
   ``build_constant_el_payload`` and all three scan cores call the shared
   :func:`floor_dispatch_start` + :func:`enforce_dispatch_delay` helpers (no
   divergent inline copy), by source inspection of ``trajectory.py``. Catches a
   future edit that re-inlines the buffer/delay logic and lets the four cores drift
   apart.

2. **Agent wiring (skip-guarded).** Where the ocs framework is importable, confirm
   the agent references the three new ``build_*`` cores, defines the three Process
   methods + the ``abort`` task, and registers them. Skipped where ocs is
   unavailable; the per-task logic is unit-tested ocs-free in the sibling files.

Imports ``trajectory.py`` directly; the agent import is guarded.
"""

import importlib.util
import inspect
import re
from pathlib import Path

import pytest

from pcs.agents.acu_interface import trajectory as traj_mod


# ---------------------------------------------------------------------------
# 1. Dispatch-window centralization (ocs-free): the shared helpers exist and
#    every core calls them, no divergent inline copy.
# ---------------------------------------------------------------------------


def test_shared_dispatch_helpers_exist():
    """The three shared dispatch-window helpers are public on trajectory.py."""
    assert callable(traj_mod.floor_dispatch_start)
    assert callable(traj_mod.enforce_dispatch_delay)
    assert callable(traj_mod.refloor_payload_start_time)


@pytest.mark.parametrize(
    "fn_name",
    [
        "build_constant_el_payload",
        "build_source_payload",
        "build_pong_payload",
        "build_daisy_payload",
    ],
)
def test_every_core_calls_the_centralized_f4_helpers(fn_name):
    """All four payload cores call the shared floor + delay helpers.

    Source-level check: each ``build_*_payload`` must reference
    ``floor_dispatch_start`` (the buffer floor) and ``enforce_dispatch_delay``
    (the too-far-out guard), so the dispatch-window logic lives in ONE place and
    the four cores cannot diverge. ``build_constant_el_payload`` in particular
    must NOT carry a re-inlined copy of the old ``max(scheduled_t0 or 0, now +
    buffer)`` expression.
    """
    src = inspect.getsource(getattr(traj_mod, fn_name))
    assert "floor_dispatch_start(" in src, f"{fn_name} does not call floor_dispatch_start"
    assert "enforce_dispatch_delay(" in src, f"{fn_name} does not call enforce_dispatch_delay"


def test_constant_el_has_no_divergent_inline_floor():
    """The CE core was refactored: it floors via the shared helper, not inline.

    Guards the refactor. If someone re-inlines the buffer floor in the CE core,
    the centralization regresses. The old inline form built ``Time(actual_t0
    _unix, ...)`` from a local ``max(...)``; the refactored core gets its anchor
    from ``floor_dispatch_start`` instead.
    """
    src = inspect.getsource(traj_mod.build_constant_el_payload)
    assert "actual_t0 = floor_dispatch_start(" in src
    assert "actual_t0_unix = max(" not in src


def test_shared_dispatch_runner_wires_the_process_guards():
    """The shared ``_dispatch_scan_process`` body references every Process-level
    guard, caught even where ocs is unavailable.

    All four typed scans now run through ``agent._dispatch_scan_process``; no
    ocs-free test can EXECUTE it (the agent needs the operations framework). Read
    ``agent.py`` as text (no import) and assert every Process-level guard lives
    in the ``_dispatch_scan_process`` body specifically (incl.
    ``threads.deferToThread``, the off-reactor wrap), so a regression that
    strips a guard from the SHARED runner is caught here.
    """
    agent_src = Path(traj_mod.__file__).with_name("agent.py").read_text(encoding="utf-8")
    start = agent_src.index("def _dispatch_scan_process(")
    nxt = re.search(r"\n    (?:@|def )", agent_src[start + 1:])
    body = agent_src[start: start + 1 + nxt.start()] if nxt else agent_src[start:]
    for guard in (
        "ScanCompletionLatch(",
        "tcs_response_status(",
        "refloor_payload_start_time(",
        "_safe_abort",
        "threads.deferToThread",
    ):
        assert guard in body, f"_dispatch_scan_process is missing {guard!r}"


def test_typed_scans_build_fresh_clients_not_shared_acu_read():
    """Each typed scan + the standalone abort task builds a FRESH per-scan Go
    TCS client via _make_tcs(); none reuses self.acu_read. requests.Session is
    not thread-safe. The monitor's reactor-thread status reads must not share
    a Session with a scan's thread-pool POSTs, and the abort task's /abort POST
    must not collide with a running scan's status reads. Source-grep (no import)
    so it runs on a dev box without the ocs operations framework.
    """
    src = Path(traj_mod.__file__).with_name("agent.py").read_text(encoding="utf-8")
    # GAP 1: all four scan wrappers hand the runner a fresh client.
    assert src.count("tcs=self._make_tcs()") >= 4, \
        "expected all four typed scans to pass tcs=self._make_tcs()"
    # GAP 2: the standalone abort task moved off the shared client (EDIT 9).
    assert "self._safe_abort, self._make_tcs()" in src, \
        "abort task must use a fresh _make_tcs() client, not self.acu_read"
    # GAP 3: constant_el_scan actually DELEGATES to the runner (not re-inlined).
    assert "build_fn=build_constant_el_payload" in src, \
        "constant_el_scan must delegate to _dispatch_scan_process"
    # The shared runner must NOT re-introduce the self.acu_read reuse.
    start = src.index("def _dispatch_scan_process(")
    nxt = re.search(r"\n    (?:@|def )", src[start + 1:])
    runner = src[start: start + 1 + nxt.start()] if nxt else src[start:]
    assert "self.acu_read" not in runner, \
        "runner reuses shared self.acu_read (concurrency hazard reintroduced)"


def _method_body(src: str, name: str) -> str:
    """Slice one agent method body: real ``def name(`` -> next def/decorator.

    Anchors on a real 4-space-indented ``def`` (``\\n    def name(``) so the
    commented-out stubs (``#def az_scan():`` etc.) that precede some methods are
    not matched.
    """
    start = src.index(f"\n    def {name}(") + 1
    nxt = re.search(r"\n    (?:@|def )", src[start + 1:])
    return src[start: start + 1 + nxt.start()] if nxt else src[start:]


@pytest.mark.parametrize("op", ["go_to", "az_scan", "fromfile_scan"])
def test_legacy_ops_read_status_503_safe(op):
    """The three legacy Tasks read the HTTP status via ``tcs_response_status``,
    not by dereferencing ``msg.status_code`` / ``msg.text`` directly.

    aculib ``post()`` returns ``{}`` on a 503 (and ``scan_pattern_from_file``
    returns ``{}`` on a 503 too, it formerly returned None on EVERY call), so
    ``msg.status_code`` / ``msg.text`` on that non-Response raised
    ``AttributeError`` and tore the Task down. The behavioural decision
    (``{}``/``None`` -> not-200 -> graceful failure) is executed and covered in
    ``test_constant_el_scan`` via ``tcs_response_status`` directly; this is the
    WIRING regression guard that the legacy ops actually route through it.
    Source-grep (no import) so it runs without the ocs operations framework.
    """
    src = Path(traj_mod.__file__).with_name("agent.py").read_text(encoding="utf-8")
    body = _method_body(src, op)
    assert "tcs_response_status(" in body, \
        f"{op} does not read its status via tcs_response_status"
    # The crash-prone raw dereferences must be gone from the body.
    assert "msg.status_code" not in body, \
        f"{op} still reads msg.status_code (crashes on a {{}}/None 503 return)"
    assert "msg.text" not in body, \
        f"{op} still reads msg.text (crashes on a {{}}/None 503 return)"


def test_abort_task_stops_scan_processes_and_excludes_infra():
    """The standalone ``abort`` Task stops any running typed scan Process so an
    out-of-band abort cannot strand ``azel_lock``.

    Source-grep (no import, Windows-safe). Asserts the ``abort`` body iterates
    ``SCAN_PROCESS_OPS`` and calls ``self.agent.stop(...)`` on each (in addition
    to the bare ``/abort`` it already sent), and that ``SCAN_PROCESS_OPS`` lists
    the four typed scans but NOT the always-on infrastructure Processes
    (``broadcast`` / ``monitor`` must keep running across an abort). Execution
    coverage of the actual stop behaviour lives in
    ``test_abort_stops_scan_process.py`` (needs the real ocs framework)."""
    src = Path(traj_mod.__file__).with_name("agent.py").read_text(encoding="utf-8")
    body = _method_body(src, "abort")
    assert "SCAN_PROCESS_OPS" in body, \
        "abort does not iterate SCAN_PROCESS_OPS to stop running scans"
    assert "self.agent.stop(" in body, \
        "abort does not call self.agent.stop() to release a stranded azel_lock"

    # SCAN_PROCESS_OPS is the four typed scans, and explicitly NOT the always-on
    # broadcast/monitor Processes (stopping those would kill the position feed).
    m = re.search(r"SCAN_PROCESS_OPS\s*=\s*\(([^)]*)\)", src)
    assert m, "SCAN_PROCESS_OPS tuple not found"
    ops = m.group(1)
    for scan in ("constant_el_scan", "source_scan", "pong_scan", "daisy_scan"):
        assert scan in ops, f"SCAN_PROCESS_OPS missing {scan!r}"
    assert "broadcast" not in ops and "monitor" not in ops, \
        "SCAN_PROCESS_OPS must exclude the always-on broadcast/monitor Processes"


# ---------------------------------------------------------------------------
# 2. Agent wiring (skip-guarded on the ocs operations framework).
#
# NB: a module literally named ``ocs`` IS importable here (the OCS REST client),
# but it is NOT the SO operations framework the agent imports. Guard on the
# submodule the agent actually needs (``ocs.ocs_agent``) so the skip is correct.
# ---------------------------------------------------------------------------


def _ocs_framework_available() -> bool:
    try:
        return importlib.util.find_spec("ocs.ocs_agent") is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


@pytest.mark.skipif(
    not _ocs_framework_available(),
    reason="ocs operations framework not importable; agent-wiring test skipped",
)
def test_agent_wires_phase3_tasks_and_cores():
    """The agent imports the three new cores and wires the four new ops.

    Skipped where ocs is unavailable. Does not exercise the Process loops (that
    needs a live reactor + session); guards against the agent drifting out of
    sync with the scan cores / registrations it depends on.
    """
    from pcs.agents.acu_interface import agent as agent_mod

    src = inspect.getsource(agent_mod)
    # The three new ocs-free cores are imported and used.
    for core in ("build_constant_el_payload", "build_source_payload",
                 "build_pong_payload", "build_daisy_payload"):
        assert core in src, f"agent does not reference {core}"
    # The three new Process methods + the standalone abort task are defined.
    for method in ("def constant_el_scan(", "def source_scan(", "def pong_scan(",
                   "def daisy_scan(", "def abort("):
        assert method in src, f"agent does not define {method!r}"
    # ... and registered next to constant_el_scan.
    for reg in (
        "register_process('constant_el_scan'",
        "register_process('source_scan'",
        "register_process('pong_scan'",
        "register_process('daisy_scan'",
        "register_task('abort'",
    ):
        assert reg in src, f"agent does not register {reg!r}"
    # ... as non-blocking (blocking=False) so they stay abortable mid-scan. A
    # blocking=True Process would hold the reactor thread and defeat the
    # cooperative stop the Process model is chosen for.
    for op, kind in (("constant_el_scan", "process"), ("source_scan", "process"),
                     ("pong_scan", "process"), ("daisy_scan", "process"),
                     ("abort", "task")):
        pat = rf"register_{kind}\(\s*'{op}'[^)]*blocking=False"
        assert re.search(pat, src), f"{op} is not registered blocking=False"
    # The three scan Processes wire the abortable stop handler.
    assert "_simple_process_stop" in src
    # The shared abort path is reused by both the standalone task and the
    # in-Process stop handlers.
    assert "_safe_abort(" in src
