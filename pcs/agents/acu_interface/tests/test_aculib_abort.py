"""``aculib.abort``: no body, and the answer it returns on each route.

The Go TCS's ``/abort`` refuses any body (``checkNoBody`` at TCS ``b7bbbf9``);
the OCS API's ``/abort`` posts to it with none and relays its answer with HTTP
200 (``abort`` in ``api/app/telescope.py`` at OCS ``c515a51``). ocs-free; the
mock needs FastAPI.
"""

import logging

import pytest

from pcs.agents.acu_interface import aculib

BASE_URL = "http://tcs.test"


@pytest.fixture
def mock_tcs():
    """The ``mock_tcs`` module, for its mocks of the Go TCS's own ``/abort`` and of the OCS API."""
    pytest.importorskip("fastapi", reason="mock_tcs needs FastAPI")
    from pcs.agents.acu_interface.tests import mock_tcs

    return mock_tcs


def _client(adapter, tcs_direct=False):
    """A client whose session ``adapter`` serves: on the OCS API's route
    (``/api/v1/telescope``) by default, on the Go TCS's own with ``tcs_direct``."""
    client = aculib.observatory_control_system(
        url=BASE_URL,
        log=logging.getLogger("test_aculib_abort"),
        server_cert="",
        client_cert="",
        client_key="",
        tcs_direct=tcs_direct,
        verify_cert=False,
    )
    client.session.mount(BASE_URL, adapter)
    return client


def test_direct_abort_stops_the_running_command(mock_tcs):
    """The Go TCS takes a direct client's abort: no body reaches checkNoBody."""
    app = mock_tcs.create_go_tcs(running=True)

    reply = _client(mock_tcs.AppAdapter(app), tcs_direct=True).abort()

    assert reply.get("status") == "ok", reply
    assert app.state.recorder.abort_bodies == [b""]


def test_direct_abort_with_nothing_running_reads_nothing_to_abort(mock_tcs):
    """With no command running the Go TCS answers "nothing to abort" with HTTP
    409, and abort returns that body for the caller to read."""
    app = mock_tcs.create_go_tcs(running=False)

    reply = _client(mock_tcs.AppAdapter(app), tcs_direct=True).abort()

    assert reply.get("message") == mock_tcs.GO_NOTHING_TO_ABORT, reply


def test_ocs_api_abort_relays_nothing_to_abort_as_200(mock_tcs):
    """On the default route the OCS API relays the Go TCS answer with HTTP 200:
    "nothing to abort" while no command runs, ok while an accepted /path runs;
    abort returns the relayed body either way, for the agent to read."""
    app = mock_tcs.create_mock_tcs()
    client = _client(mock_tcs.AppAdapter(app))

    idle = client.abort()
    client.scan_pattern({"start_time": 1_800_000_060.0, "coordsys": "Horizon",
                         "points": [[0.0, 120.0, 45.0, 0.0, 0.0]]})
    running = client.abort()

    assert idle == mock_tcs.gateway_refusal(mock_tcs.GO_NOTHING_TO_ABORT)
    assert running == {"status": "ok"}
    assert app.state.recorder.abort_count == 2


def test_abort_names_a_503_from_the_ocs_api_front(mock_tcs):
    """An HTTP 503 to an abort (``post`` returns ``{}``) raises a RuntimeError that
    names the status, not an AttributeError from reading a body ``{}`` lacks."""
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse

    app = FastAPI()

    @app.post(f"{mock_tcs.PREFIX}/abort")
    async def abort():
        return PlainTextResponse("Service Unavailable", status_code=503)

    client = _client(mock_tcs.AppAdapter(app))

    with pytest.raises(RuntimeError, match="HTTP 503, no answer from the TCS"):
        client.abort()
