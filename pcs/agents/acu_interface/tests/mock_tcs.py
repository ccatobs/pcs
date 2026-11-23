"""In-process mock of the OCS API's telescope routes, which relay the Go TCS.

:func:`create_mock_tcs` serves POST ``/api/v1/telescope/move-to``, ``/path`` and
``/abort`` and GET ``.../status`` (``api/app/telescope.py`` at OCS ``c515a51``),
recording each POST on ``app.state.recorder``. It answers as the API relays the
Go TCS: a refusal queued on ``recorder.refusals`` comes back with HTTP 200 and
:func:`gateway_refusal`'s body, and :data:`OCS_API_ERROR` as the API's own
plain-text HTTP 500. Like the Go TCS it tracks whether a command runs: an
accepted ``/move-to`` or ``/path`` starts one, and ``/abort`` stops it or answers
"nothing to abort". ``/status`` serves ``recorder.status``; an accepted
``/move-to`` leaves both axes in Preset on the target, as Telescope.MoveTo does.

:func:`create_go_tcs` mocks the Go TCS's own ``/abort``, with Go's status codes.
:class:`AppAdapter` serves a real ``requests.Session`` from either app. Tests
that import this module skip without FastAPI.
"""

import io
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, conlist
from requests.adapters import HTTPAdapter
from urllib3 import HTTPResponse

PREFIX = "/api/v1/telescope"

#: Go TCS refusal messages, verbatim, with the HTTP status Go sends them with (TCS ``b7bbbf9``).
GO_BUSY = "busy"  # a command not queued within commandBusyTimeout, 503
GO_START_TOO_SOON = (  # checkProgramTrackStartTime, 400
    "program track starts too soon (< 10 seconds from now)")
GO_NOTHING_TO_ABORT = "nothing to abort"  # the /abort handler, 409
GO_NO_PARAMETERS = "command takes no parameters"  # checkNoBody, 400
GO_AZIMUTH_OUT_OF_RANGE = (  # checkAzEl, for a move-to at azimuth 400, 400
    "commanded azimuth (400) out of range [-180,360]")

#: The OCS proxy's own failure: FastAPI answers an error its route does not
#: catch (the hop to the Go TCS raised, or Go's answer was not JSON) with HTTP
#: 500 and this plain-text body.
OCS_API_ERROR = "Internal Server Error"


def gateway_refusal(message: str) -> dict:
    """The body the OCS proxy answers, with HTTP 200, when the Go TCS refuses.

    ``api/app/telescope.py`` passes Go's JSON through its ``CommandResponse``
    model, which keeps ``status``, ``id`` and ``message``, with FastAPI's default
    HTTP 200 whatever status Go sent.
    """
    return {"status": "error", "message": message}


class Recorder:
    """Collects the requests the mock received, for test assertions, and holds
    the state the mock answers from."""

    def __init__(self) -> None:
        self.path_bodies: list[dict] = []
        self.move_to_bodies: list[dict] = []
        self.abort_count: int = 0
        #: The raw body of each ``/abort`` the Go-shaped app received.
        self.abort_bodies: list[bytes] = []
        #: Go TCS refusal messages to relay, oldest first, per endpoint; each
        #: command takes the next one while any remain, then is accepted.
        self.refusals: dict[str, list[str]] = {"path": [], "move-to": []}
        #: Whether a command is running: an accepted ``/move-to`` or ``/path``
        #: sets it and ``/abort`` clears it; a test clears it to model a
        #: command that ran to its end.
        self.running: bool = False
        #: The ACU status ``/status`` serves, under the StatusGeneral8100 field
        #: names (telescope_status in api/app/telescope.py at OCS c515a51); by
        #: default the stack drained (9999 free, maxFreeProgramTrackStack-1)
        #: with both axes in Stop.
        self.status: dict = {
            "QtyOfFreeProgramTrackStackPositions": 9999,
            "AzimuthCurrentVelocity": 0.0,
            "ElevationCurrentVelocity": 0.0,
            "AzimuthCurrentPosition": 0.0,
            "ElevationCurrentPosition": 60.0,
            "AzimuthCommandedPosition": 0.0,
            "ElevationCommandedPosition": 60.0,
            "AzimuthMode": "Stop",
            "ElevationMode": "Stop",
        }

    def next_refusal(self, endpoint: str) -> dict | PlainTextResponse | None:
        """Pop the next queued refusal for ``endpoint`` as the proxy answers it:
        :func:`gateway_refusal`'s body, or for :data:`OCS_API_ERROR` the
        proxy's own plain-text HTTP 500; ``None`` when none is queued."""
        queue = self.refusals[endpoint]
        if not queue:
            return None
        message = queue.pop(0)
        if message == OCS_API_ERROR:
            return PlainTextResponse(OCS_API_ERROR, status_code=500)
        return gateway_refusal(message)

    def answer(self, endpoint: str) -> dict | PlainTextResponse:
        """Answer a command to ``endpoint``: the next queued refusal, else the
        command is accepted and runs."""
        refusal = self.next_refusal(endpoint)
        if refusal is not None:
            return refusal
        self.running = True
        return {"status": "ok"}


class MoveToParameters(BaseModel):
    azimuth: float
    elevation: float


class PathParameters(BaseModel):
    start_time: float
    coordsys: Literal["Horizon", "ICRS"]
    points: list[conlist(float, min_length=5, max_length=5)]


def create_mock_tcs() -> FastAPI:
    """Build a mock-TCS FastAPI app with a fresh :class:`Recorder` on state."""
    app = FastAPI()
    app.state.recorder = Recorder()

    @app.post(f"{PREFIX}/move-to")
    async def move_to(param: MoveToParameters, request: Request):
        recorder = request.app.state.recorder
        recorder.move_to_bodies.append(param.model_dump())
        answer = recorder.answer("move-to")
        if answer == {"status": "ok"}:
            # Telescope.MoveTo at TCS b7bbbf9 sets both axes to Stop, presets
            # the target and sets Preset, so the ACU leaves a track it was
            # playing; the stack keeps its points. The mock's pointing model is
            # zero, so the commanded position is the request.
            recorder.status.update(AzimuthMode="Preset", ElevationMode="Preset",
                                   AzimuthCommandedPosition=param.azimuth,
                                   ElevationCommandedPosition=param.elevation)
        return answer

    @app.post(f"{PREFIX}/path")
    async def path(param: PathParameters, request: Request):
        recorder = request.app.state.recorder
        recorder.path_bodies.append(param.model_dump())
        return recorder.answer("path")

    @app.post(f"{PREFIX}/abort")
    async def abort(request: Request):
        # The Go TCS's /abort handler at TCS b7bbbf9 stops the running command
        # or, with none running, refuses with "nothing to abort", which the
        # proxy relays as HTTP 200.
        recorder = request.app.state.recorder
        recorder.abort_count += 1
        if recorder.running:
            recorder.running = False
            return {"status": "ok"}
        return gateway_refusal(GO_NOTHING_TO_ABORT)

    @app.get(f"{PREFIX}/status")
    async def telescope_status(request: Request):
        # The route aculib.get_status reads; it serves recorder.status as the test sets it.
        return dict(request.app.state.recorder.status)

    return app


def create_go_tcs(running: bool = False) -> FastAPI:
    """Build a mock of the Go TCS's own ``/abort``, the route a client built with
    ``tcs_direct=True`` posts to, with a fresh :class:`Recorder` on state.

    It answers as the ``/abort`` handler at TCS ``b7bbbf9`` does: ``checkNoBody``
    refuses any body with HTTP 400 and :data:`GO_NO_PARAMETERS`; otherwise the
    abort stops the running command, HTTP 200, or with ``running`` False finds
    none, HTTP 409 and :data:`GO_NOTHING_TO_ABORT`. Each answer carries its
    command ID, as Go's does.
    """
    app = FastAPI()
    app.state.recorder = Recorder()

    @app.post("/abort")
    async def abort(request: Request):
        recorder = request.app.state.recorder
        body = await request.body()
        recorder.abort_bodies.append(body)
        recorder.abort_count += 1
        cmd_id = recorder.abort_count
        if body:
            return JSONResponse(
                {"status": "error", "id": cmd_id, "message": GO_NO_PARAMETERS},
                status_code=400)
        if running:
            return {"status": "ok", "id": cmd_id}
        return JSONResponse(
            {"status": "error", "id": cmd_id, "message": GO_NOTHING_TO_ABORT},
            status_code=409)

    return app


class AppAdapter(HTTPAdapter):
    """Serve a ``requests.Session`` from an in-process app, not the network.

    Mount it with ``session.mount(base_url, AppAdapter(app))``: each request
    ``requests`` prepared goes to ``app`` through FastAPI's ``TestClient`` with its
    body bytes and headers as encoded, and the app's answer comes back as a
    ``requests.Response``.
    """

    def __init__(self, app: FastAPI) -> None:
        super().__init__()
        from fastapi.testclient import TestClient

        self._client = TestClient(app)

    def send(self, request, **kwargs):
        reply = self._client.request(
            request.method, request.url, content=request.body,
            headers=dict(request.headers))
        raw = HTTPResponse(
            body=io.BytesIO(reply.content), headers=dict(reply.headers),
            status=reply.status_code, preload_content=False)
        return self.build_response(request, raw)

    def close(self) -> None:
        self._client.close()
        super().close()
