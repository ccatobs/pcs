"""The broadcast Process unpacks the ACU's UDP position records with the schema the
device names, drops datagrams of another record layout, and keeps a NaN mean out of
the 1 Hz influx block. Drives the real ACUAgent.broadcast with a fake reactor.
"""

import importlib.util
import math
import pathlib
import re
import struct
import sys
import types

import numpy as np
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

    from pcs.agents.acu_interface import agent as _AGENT
except Exception as exc:  # pragma: no cover - environment-dependent
    _IMPORT_ERR = exc

pytestmark = pytest.mark.skipif(
    _AGENT is None, reason=f"agent module not importable ({_IMPORT_ERR})"
)

CONFIG = pathlib.Path(__file__).resolve().parents[1] / "acu_config.yaml"

#: One record from each ACU simulator layout, encoded by Go's encoding/binary with the
#: simulator's own call, binary.Write(buf, binary.LittleEndian, []BroadcastingEncoders{rec}),
#: on the BroadcastingEncoders struct of simulator/engine.go in antenna-control-unit at
#: v0.6.0 (9918d6e) and v0.7.0 (b3373b4).
GO_RECORD = {
    "v0.6.0": bytes.fromhex(
        "3a01000000000000101ee6400000000000846640000000000040494000000000008266400000000000044940"
        "000000000000f83f00000000000004400000000000000c40000000000000124000000000000016400000000000001a40"),
    "v0.7.0": bytes.fromhex(
        "3a01000000000000101ee6400000000000b066400000000000a04940000000000084664000000000004049400000"
        "0000008266400000000000044940000000000000f83f00000000000004400000000000000c400000000000001240"
        "00000000000016400000000000001a40"),
}

#: The sender's struct order: Day (int32), TimeOfDay, at v0.7.0 AzCommand and ElCommand,
#: then the fields below (Go's AzPosition and ElPosition are the agent's Azimuth and Elevation).
SENDER = {"v0.6.0": struct.Struct("<l11d"), "v0.7.0": struct.Struct("<l13d")}
DAY, TIME_OF_DAY = 314, 45296.5
VALUES = {"Azimuth": 180.125, "Elevation": 50.5, "AzEncoder": 180.0625, "ElEncoder": 50.03125,
          "AzCurrent1": 1.5, "AzCurrent2": 2.5, "AzCurrent3": 3.5, "AzCurrent4": 4.5,
          "ElCurrent1": 5.5, "ElCurrent2": 6.5}
COMMAND = {"AzCommand": 181.5, "ElCommand": 51.25}

#: One second of the stream: an azimuth per record, so a wrong stride cannot pass.
AZIMUTHS = 180.0 + np.arange(200) / 8


def _record(tag, azimuth=VALUES["Azimuth"], command=tuple(COMMAND.values())):
    head = (DAY, TIME_OF_DAY) if tag == "v0.6.0" else (DAY, TIME_OF_DAY, *command)
    return SENDER[tag].pack(*head, *dict(VALUES, Azimuth=azimuth).values())


def _second(tag, command=tuple(COMMAND.values())):
    """Twenty datagrams of ten records, as the simulator at ``tag`` sends them."""
    records = [_record(tag, az, command) for az in AZIMUTHS]
    return [b"".join(records[i:i + 10]) for i in range(0, 200, 10)]


class _Log:
    def __init__(self):
        self.errors = []

    def error(self, fmt, **kw):
        self.errors.append(fmt.format(**kw))

    def info(self, *a, **k):
        pass

    warn = warning = debug = info


class _Session:
    """Reads 'running' for ``turns`` turns of the loop, then 'done'."""

    def __init__(self, turns=1):
        self.data = {}
        self._reads = 0
        self._turns = turns

    @property
    def status(self):
        self._reads += 1
        return "running" if self._reads <= self._turns else "done"


def _run_broadcast(monkeypatch, schema, datagrams, turns=1, auto_enable=False, prepare=None):
    """Run the real broadcast for ``turns`` turns after ``datagrams`` arrive; return
    the agent, the session and the published blocks by feed. ``prepare(agent)`` runs
    just before the Process starts."""
    config = _AGENT.aculib.load_config(str(CONFIG), update_cache=False)
    agent = _AGENT.ACUAgent.__new__(_AGENT.ACUAgent)
    agent.log = _Log()
    agent.acu_conf = {"interface_ip": "127.0.0.1", "base_url": "http://tcs.invalid"}
    agent.udp = {"port": 5601, "schema": schema}
    agent.udp_schema = config["stream_schemas"][schema]
    agent.data = {"broadcast": {}}
    published = {"acu_udp_stream": [], "acu_broadcast_influx": []}
    fields = {}

    def publish_to_feed(feed, block):
        # An OCS feed block keeps the fields of its first message (ocs Block.append).
        first = fields.setdefault((feed, block["block_name"]), set(block["data"]))
        assert set(block["data"]) == first, block["block_name"]
        published[feed].append(block)

    agent.agent = types.SimpleNamespace(publish_to_feed=publish_to_feed)

    def listen_udp(port, proto):
        for datagram in datagrams:
            proto.datagramReceived(datagram, ("127.0.0.1", 10000))
        return types.SimpleNamespace(stopListening=lambda: None)

    monkeypatch.setattr(_AGENT, "reactor", types.SimpleNamespace(listenUDP=listen_udp))
    monkeypatch.setattr(_AGENT, "dsleep", lambda *a, **k: defer.succeed(None))
    session = _Session(turns)
    if prepare is not None:
        prepare(agent)
    out = []
    agent.broadcast(session, {"auto_enable": auto_enable}).addBoth(out.append)
    assert out == [(True, "Acquisition exited cleanly.")]
    return agent, session, published


def test_each_shipped_schema_unpacks_one_value_per_field():
    """Each schema in acu_config.yaml unpacks one value per field name, and the
    acu-sim device names a schema that exists."""
    config = _AGENT.aculib.load_config(str(CONFIG), update_cache=False)
    for name, schema in config["stream_schemas"].items():
        values = struct.unpack(schema["format"], bytes(struct.calcsize(schema["format"])))
        assert len(values) == len(schema["fields"]), name
    assert config["devices"]["acu-sim"]["streams"]["main"]["schema"] in config["stream_schemas"]


def test_a_device_naming_an_undefined_schema_is_refused_by_name(monkeypatch):
    """A streams.main.schema that stream_schemas does not define is refused with a
    message naming the schema and where it is chosen, not a bare KeyError."""
    config = _AGENT.aculib.load_config(str(CONFIG), update_cache=False)
    monkeypatch.setattr(_AGENT.aculib, "cache", config)
    assert "v9" not in config["stream_schemas"]

    with pytest.raises(KeyError) as excinfo:
        _AGENT.aculib.get_stream_schema("v9")

    message = excinfo.value.args[0]
    assert "'v9'" in message and "stream_schemas" in message and "streams.main.schema" in message


@pytest.mark.parametrize("tag", sorted(GO_RECORD))
def test_sender_layout_matches_the_go_encoder(tag):
    """The sender's layout written here reproduces Go's record byte for byte."""
    assert _record(tag) == GO_RECORD[tag]


@pytest.mark.parametrize("schema, tag, command", [
    ("v1", "v0.7.0", COMMAND),
    ("v0", "v0.6.0", {}),
], ids=["v1-v0.7.0", "v0-v0.6.0"])
def test_schema_matching_the_sender_reads_every_field(monkeypatch, schema, tag, command):
    """The schema that matches the sender leaves the agent holding the last record,
    publishes every field, and puts the sent means in session.data."""
    agent, session, published = _run_broadcast(monkeypatch, schema, _second(tag))

    last = dict(VALUES, Azimuth=AZIMUTHS[-1], **command)
    held = agent.data["broadcast"]
    assert set(held) == set(last) | {"Time"}
    assert {name: held[name] for name in last} == last
    assert len(published["acu_udp_stream"]) == 200
    assert published["acu_udp_stream"][-1]["data"] == held

    means = dict(VALUES, Azimuth=AZIMUTHS.mean(), **command)
    assert set(session.data) == set(means) | {"Time"}
    assert {name: session.data[name] for name in means} == pytest.approx(means)
    [influx] = published["acu_broadcast_influx"]
    assert set(influx["data"]) == {name + "_bcast_influx" for name in set(means) | {"Time"}}


@pytest.mark.parametrize("schema, tag, size, record", [
    ("v0", "v0.7.0", 1080, 92),
    ("v1", "v0.6.0", 920, 108),
])
def test_datagrams_of_another_layout_are_dropped_and_reported_once(
        monkeypatch, schema, tag, size, record):
    """Datagrams of the other layout are not unpacked, and one error for the second
    names the datagram size, the record size and the schema."""
    agent, session, published = _run_broadcast(monkeypatch, schema, _second(tag))

    assert agent.data["broadcast"] == {}
    assert published == {"acu_udp_stream": [], "acu_broadcast_influx": []}
    assert session.data == {}
    [message] = agent.log.errors
    numbers = re.findall(r"\d+", message)
    assert str(size) in numbers and str(record) in numbers
    assert schema in message and "streams.main.schema" in message


def test_nan_commanded_position_stays_out_of_the_influx_block(monkeypatch):
    """A NaN commanded position for the whole second leaves the position read, keeps
    the commanded fields out of the influx block, and reaches session.data as NaN."""
    nan = float("nan")
    agent, session, published = _run_broadcast(monkeypatch, "v1", _second("v0.7.0", (nan, nan)))

    assert agent.data["broadcast"]["Azimuth"] == AZIMUTHS[-1]
    assert agent.data["broadcast"]["Elevation"] == VALUES["Elevation"]
    [influx] = published["acu_broadcast_influx"]
    assert all(math.isfinite(value) for value in influx["data"].values())
    assert "AzCommand_bcast_influx" not in influx["data"]
    assert "ElCommand_bcast_influx" not in influx["data"]
    assert influx["data"]["Azimuth_bcast_influx"] == pytest.approx(AZIMUTHS.mean())
    assert math.isnan(session.data["AzCommand"]) and math.isnan(session.data["ElCommand"])


@pytest.mark.parametrize("first", ["finite", "nan"])
def test_a_commanded_position_that_turns_nan_or_back_keeps_each_block_whole(monkeypatch, first):
    """A second with a finite commanded position and one with NaN (a move-to, then
    a program track, or the reverse) each publish an influx block, and no block name
    changes its fields."""
    nan = float("nan")
    commands = [tuple(COMMAND.values()), (nan, nan)]
    if first == "nan":
        commands.reverse()
    datagrams = _second("v0.7.0", commands[0]) + _second("v0.7.0", commands[1])
    agent, session, published = _run_broadcast(monkeypatch, "v1", datagrams, turns=2)

    assert len(published["acu_broadcast_influx"]) == 2
    assert all(math.isfinite(v) for block in published["acu_broadcast_influx"]
               for v in block["data"].values())


@pytest.mark.parametrize("post_fails", [False, True], ids=["post-ok", "post-fails"])
def test_the_stream_enable_post_runs_off_the_reactor(monkeypatch, post_fails):
    """With no datagrams and auto_enable, the stream-enable POST goes through
    threads.deferToThread, never on the reactor thread; a failed POST is caught and
    logged, and the next request waits a minute, so four turns send one POST."""
    clock = [1000.0]

    def tick():
        clock[0] += 5.0  # past the 3 s outage threshold on the first turn
        return clock[0]

    posts, deferred = [], []

    def post(url, **kwargs):
        posts.append(url)
        if post_fails:
            raise ConnectionError("API unreachable")

    def defer_to_thread(func, *args, **kwargs):
        deferred.append(func)
        return defer.maybeDeferred(func, *args, **kwargs)

    infos = []

    def prepare(agent):
        agent._make_tcs = lambda: types.SimpleNamespace(session=types.SimpleNamespace(post=post))
        agent.log.info = lambda fmt, **kw: infos.append(fmt.format(**kw))

    monkeypatch.setattr(_AGENT, "time", types.SimpleNamespace(time=tick))
    monkeypatch.setattr(_AGENT, "threads", types.SimpleNamespace(deferToThread=defer_to_thread))
    _run_broadcast(monkeypatch, "v1", [], turns=4, auto_enable=True, prepare=prepare)

    assert deferred == [post]
    assert posts == ["http://tcs.invalid/api/v1/telescope/acu/position-broadcast"]
    failed = [m for m in infos if m.startswith("Exception while trying to enable stream")]
    assert failed == (["Exception while trying to enable stream: API unreachable"] if post_fails else [])
