"""Regression test: ``aculib.move_to`` must not crash on HTTP 503.

Go TCS can return 503 for ``/move-to`` (e.g. a prior command still running);
``observatory_control_system.post()`` short-circuits a 503 to ``{}`` (not a
Response), so ``move_to``'s diagnostic ``.json()`` log must be guarded or it
raises ``AttributeError`` inside the client and tears down the calling Process.

An OCS API error (HTTP 500 with FastAPI's plain-text body) is returned, not raised.

ocs-free: ``aculib`` imports no OCS/twisted, so this runs in
the minimal env and is date-independent.
"""

import logging

import pytest

from pcs.agents.acu_interface import aculib


def _client():
    """Build a client with empty certs (plain session, no network at init)."""
    return aculib.observatory_control_system(
        url="http://localhost:0",
        log=logging.getLogger("test_aculib_move_to"),
        server_cert="",
        client_cert="",
        client_key="",
        verify_cert=False,
    )


def test_move_to_503_returns_empty_dict_without_crashing(monkeypatch):
    """A 503 (``post()`` -> ``{}``) returns ``{}`` from move_to, not AttributeError."""
    client = _client()
    monkeypatch.setattr(client, "post", lambda cmd, data: {})
    result = client.move_to(120.0, 45.0)
    assert result == {}


def test_move_to_200_still_returns_response(monkeypatch):
    """On a real Response the 200-path is unchanged (move_to returns it)."""

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": "ok"}

    resp = _Resp()
    client = _client()
    monkeypatch.setattr(client, "post", lambda cmd, data: resp)
    assert client.move_to(120.0, 45.0) is resp


@pytest.fixture
def ocs_api():
    """The ``mock_tcs`` module, for its mock of the OCS API."""
    pytest.importorskip("fastapi", reason="mock_tcs needs FastAPI")
    from pcs.agents.acu_interface.tests import mock_tcs

    return mock_tcs


def test_move_to_returns_the_ocs_api_error_answered_in_plain_text(ocs_api):
    """An OCS API error to a move-to (HTTP 500 with FastAPI's plain-text body)
    comes back as the Response, for the caller's status check, instead of an
    exception from logging a body that is not JSON."""
    app = ocs_api.create_mock_tcs()
    app.state.recorder.refusals["move-to"].append(ocs_api.OCS_API_ERROR)
    client = _client()
    client.session.mount("http://", ocs_api.AppAdapter(app))

    response = client.move_to(120.0, 45.0)

    assert response.status_code == 500
    assert response.text == ocs_api.OCS_API_ERROR
    assert app.state.recorder.move_to_bodies == [{"azimuth": 120.0, "elevation": 45.0}]
