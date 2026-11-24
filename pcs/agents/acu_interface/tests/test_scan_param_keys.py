"""The ``scan_params`` keys each typed scan's builder reads.

``trajectory.SCAN_PARAM_KEYS`` names, per op, the keys its ``build_*_payload``
reads; the agent warns about and publishes the others
(``unknown_scan_param_keys``). Each set is held equal to the keys the builder's
source reads, found with ``ast`` so text that mentions a key is not counted; any
other use of ``scan_params`` (``.pop``, a computed key, ``**scan_params``, the
dictionary passed on) fails, since it could read a key the comparison cannot
see. ocs-free: imports ``trajectory.py`` only.
"""

import ast
import inspect
import textwrap

import pytest

from pcs.agents.acu_interface import trajectory as traj_mod
from pcs.agents.acu_interface.trajectory import (
    AGENT_SCAN_PARAM_KEYS,
    CONSTANT_EL_UNUSED_IN_FIELD_FORM,
    CONSTANT_EL_UNUSED_IN_WINDOW_FORM,
    SCAN_PARAM_KEYS,
    unknown_scan_param_keys,
)

_BUILDERS = {
    "constant_el_scan": traj_mod.build_constant_el_payload,
    "source_scan": traj_mod.build_source_payload,
    "pong_scan": traj_mod.build_pong_payload,
    "daisy_scan": traj_mod.build_daisy_payload,
}

# The keys a set admits beyond what its builder reads: the planet pass's two
# bookkeeping keys, which describe the sequence and play no part in the build.
_ADMITTED_UNREAD = {"source_scan": {"pass_index", "n_passes"}}


def _is_str_constant(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _constant_key_read(name, parents):
    """Return the key of a ``scan_params["k"]`` or ``scan_params.get("k"[, default])``
    read of the ``scan_params`` name node ``name``, or ``None`` for any other use."""
    parent = parents.get(name)
    if (isinstance(parent, ast.Subscript) and parent.value is name
            and isinstance(parent.ctx, ast.Load) and _is_str_constant(parent.slice)):
        return parent.slice.value
    if isinstance(parent, ast.Attribute) and parent.value is name and parent.attr == "get":
        call = parents.get(parent)
        if (isinstance(call, ast.Call) and call.func is parent and 1 <= len(call.args) <= 2
                and not call.keywords and _is_str_constant(call.args[0])):
            return call.args[0].value
    return None


def _keys_read(builder):
    """Return the keys ``builder`` reads from ``scan_params`` and its other uses of the name.

    The builder's source is parsed, its docstring skipped, and each appearance of
    the name ``scan_params`` in the body sorted into a read by a string-constant
    key or another use, reported by its line in the builder's file.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(builder)))
    ast.increment_lineno(tree, builder.__code__.co_firstlineno - 1)
    (func,) = tree.body
    body = func.body[1:] if ast.get_docstring(func) is not None else func.body
    parents = {}
    for statement in body:
        for node in ast.walk(statement):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
    keys, other_uses = set(), []
    for statement in body:
        for node in ast.walk(statement):
            if not (isinstance(node, ast.Name) and node.id == "scan_params"):
                continue
            key = _constant_key_read(node, parents)
            if key is not None:
                keys.add(key)
                continue
            use = parents[node]
            if isinstance(use, ast.Attribute) and isinstance(parents.get(use), ast.Call):
                use = parents[use]
            other_uses.append(f"line {node.lineno}: {ast.unparse(use)}")
    return keys, other_uses


@pytest.mark.parametrize("job", sorted(_BUILDERS))
def test_each_set_names_exactly_the_keys_its_builder_reads(job):
    """The set is the builder's reads, plus only the admitted bookkeeping keys.

    The builder must read ``scan_params`` only by string-constant key, since any
    other use could read a key this comparison would not see.
    """
    read, other_uses = _keys_read(_BUILDERS[job])
    assert other_uses == [], f"the {job} builder uses scan_params other than by a constant key"
    assert read, f"no scan_params reads found in the {job} builder"
    assert read - SCAN_PARAM_KEYS[job] == set(), "a key the builder reads is missing from its set"
    assert SCAN_PARAM_KEYS[job] - read == _ADMITTED_UNREAD.get(job, set())


def _reads_by_constant_key(*, scan_params):
    """Mentions scan_params["in_docstring"], which is text, not a read."""
    note = 'scan_params.get("in_message")'
    return note, scan_params["a"], scan_params.get("b"), scan_params.get("c", 0.0), f"{scan_params['d']}"


def _pops_a_key(*, scan_params):
    return scan_params.pop("a", None)


def _reads_a_computed_key(*, scan_params, key):
    return scan_params[key], scan_params.get(key)


def _unpacks_the_dictionary(*, scan_params):
    return dict(**scan_params)


def _passes_the_dictionary_on(*, scan_params):
    return sorted(scan_params)


def test_the_reader_counts_constant_key_reads_and_not_text():
    """Subscript and ``.get`` reads by a string constant are counted, in an
    f-string too; a key named in the docstring or a message is not."""
    assert _keys_read(_reads_by_constant_key) == ({"a", "b", "c", "d"}, [])


@pytest.mark.parametrize(
    "builder",
    [_pops_a_key, _reads_a_computed_key, _unpacks_the_dictionary, _passes_the_dictionary_on],
    ids=lambda builder: builder.__name__,
)
def test_the_reader_reports_any_other_use(builder):
    """A ``.pop``, a computed key, ``**scan_params`` and the dictionary passed
    on are each reported as another use, which fails the comparison above,
    rather than passing unseen."""
    read, other_uses = _keys_read(builder)
    assert read == set()
    assert other_uses
    assert all("scan_params" in use for use in other_uses)


def test_the_sets_cover_the_four_typed_scans_and_the_agent_key_is_apart():
    assert set(SCAN_PARAM_KEYS) == set(_BUILDERS)
    assert AGENT_SCAN_PARAM_KEYS == {"max_dispatch_delay_sec"}
    for keys in SCAN_PARAM_KEYS.values():
        assert not (keys & AGENT_SCAN_PARAM_KEYS)


def test_unknown_keys_are_listed_sorted_and_known_ones_are_not():
    """Known keys and the agent's own key are not listed; the rest are, sorted,
    as strings (a non-string key is listed by its text)."""
    params = {"ra_center": 1.0, "zeta": 1, "alpha": 2, "max_dispatch_delay_sec": 3.0, 7: "x"}
    assert unknown_scan_param_keys("pong_scan", params) == ["7", "alpha", "zeta"]
    assert unknown_scan_param_keys("pong_scan", dict.fromkeys(SCAN_PARAM_KEYS["pong_scan"], 0)) == []


def test_the_planet_pass_bookkeeping_keys_are_admitted_and_eta_offset_is_not():
    """The library's dispatch dict for a planet pass carries pass_index and
    n_passes, which the build does not need, and eta_offset_deg, which it does
    not read: a nonzero shift is named, since a pass carrying it is built
    without its row shift."""
    pass_dict = {"body": "saturn", "el_bore": 40.0, "mode": "setting", "pass_index": 0,
                 "n_passes": 3, "eta_offset_deg": 0.3}
    assert unknown_scan_param_keys("source_scan", pass_dict) == ["eta_offset_deg"]


# A planet pass's dispatch dict as fyst-trajectories v0.14.0 writes it
# (plan_calibration_night under its default policy): the centre module, no
# boresight rotation, the solved throw (az_padding 0.0), and the pass on the
# module's own row (eta_offset_deg 0.0).
_DEFAULT_PLANET_PASS = {
    "body": "saturn", "footprint": "c", "el_bore": 40.0, "mode": "setting",
    "boresight_rot": None, "timestep": 0.1, "eta_offset_deg": 0.0, "pass_index": 0,
    "n_passes": 1, "az_speed": 1.5, "az_accel": 1.0, "az_padding": 0.0,
}


@pytest.mark.parametrize("eta_offset", [0.0, 0, None])
def test_a_zero_eta_offset_on_a_planet_pass_is_not_named(eta_offset):
    """Every planet pass the library writes carries eta_offset_deg, 0.0 for a
    pass on the module's own row; a zero or absent shift changes nothing, so a
    default pass names no ignored key, and the warning is kept for keys that
    change the scan."""
    pass_dict = dict(_DEFAULT_PLANET_PASS, eta_offset_deg=eta_offset)
    assert unknown_scan_param_keys("source_scan", pass_dict) == []


def test_a_zero_eta_offset_is_passed_over_on_a_source_scan_only():
    """The zero-shift pass-over is the source scan's: a pong does not read
    eta_offset_deg either, and names it whatever its value."""
    assert unknown_scan_param_keys("pong_scan", {"ra_center": 1.0, "eta_offset_deg": 0.0}) == ["eta_offset_deg"]


@pytest.mark.parametrize("job", ["constant_el_scan", "pong_scan", "daisy_scan"])
def test_the_other_pointing_key_is_read_to_be_refused_not_named(job):
    """A field scan is aimed with module and a source scan with footprint; each
    builder reads the other key to refuse it before any motion (the refusal
    tests are in test_module_key.py), so neither is named as ignored."""
    assert {"module", "footprint"} <= SCAN_PARAM_KEYS[job]
    assert {"module", "footprint"} <= SCAN_PARAM_KEYS["source_scan"]
    params = {"module": "IM5", "footprint": "IM5"}
    assert unknown_scan_param_keys(job, params) == []
    assert unknown_scan_param_keys("source_scan", params) == []


def test_the_constant_el_per_form_sets_are_keys_its_builder_reads():
    """Each key a form does not use is one the constant-elevation builder reads,
    and no key is unused in both forms."""
    for unused in (CONSTANT_EL_UNUSED_IN_WINDOW_FORM, CONSTANT_EL_UNUSED_IN_FIELD_FORM):
        assert unused < SCAN_PARAM_KEYS["constant_el_scan"]
    assert not CONSTANT_EL_UNUSED_IN_WINDOW_FORM & CONSTANT_EL_UNUSED_IN_FIELD_FORM


def test_a_name_outside_the_typed_scans_reads_nothing():
    assert unknown_scan_param_keys("az_scan", {"a": 1, "max_dispatch_delay_sec": 1}) == ["a"]
