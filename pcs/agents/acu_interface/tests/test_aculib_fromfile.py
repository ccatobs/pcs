"""Regression test: ``aculib.scan_pattern_from_file`` must return its response.

The method previously ended in a bare ``return`` (None), so the legacy
``fromfile_scan`` Task's ``msg.status_code`` log crashed with ``AttributeError``
on EVERY call (and a real 503 -> ``{}`` would crash too). It now returns whatever
``scan_pattern`` returns (the Response on success, ``{}`` on a 503
short-circuit), so the caller can read the status via ``tcs_response_status``.

ocs-free (``aculib`` imports no OCS/twisted), so this runs in the minimal env and
is date-independent.
"""

import logging

from pcs.agents.acu_interface import aculib


def _client():
    """Build a client with empty certs (plain session, no network at init)."""
    return aculib.observatory_control_system(
        url="http://localhost:0",
        log=logging.getLogger("test_aculib_fromfile"),
        server_cert="",
        client_cert="",
        client_key="",
        verify_cert=False,
    )


def _write_points(tmp_path):
    """A minimal two-point Horizon path file (az el per line)."""
    p = tmp_path / "path.txt"
    p.write_text("120.0 45.0\n121.0 45.0\n", encoding="utf-8")
    return str(p)


def test_scan_pattern_from_file_returns_response(tmp_path, monkeypatch):
    """On a real Response it returns that Response (not None)."""

    class _Resp:
        status_code = 200
        text = "ok"

    resp = _Resp()
    client = _client()
    # scan_pattern is the only thing that would touch the network; stub it.
    monkeypatch.setattr(client, "scan_pattern", lambda data: resp)
    result = client.scan_pattern_from_file(_write_points(tmp_path))
    assert result is resp


def test_scan_pattern_from_file_503_returns_empty_dict(tmp_path, monkeypatch):
    """A 503 (``scan_pattern`` -> ``{}``) propagates ``{}``, never None."""
    client = _client()
    monkeypatch.setattr(client, "scan_pattern", lambda data: {})
    result = client.scan_pattern_from_file(_write_points(tmp_path))
    assert result == {}
    # The old bug: a bare ``return`` yielded None, crashing ``None.status_code``.
    assert result is not None


def test_scan_pattern_from_file_passes_parsed_points(tmp_path, monkeypatch):
    """The file is parsed into float rows and handed to scan_pattern as 'points'."""
    captured = {}
    client = _client()
    monkeypatch.setattr(
        client, "scan_pattern", lambda data: captured.update(data) or {})
    client.scan_pattern_from_file(_write_points(tmp_path))
    assert captured["coordsys"] == "Horizon"
    assert captured["points"] == [[120.0, 45.0], [121.0, 45.0]]
