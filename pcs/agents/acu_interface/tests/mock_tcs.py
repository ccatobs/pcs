"""Minimal mock of the FYST Go TCS REST surface for testing.

A tiny FastAPI app mirroring the four endpoints the typed scan tasks drive (``POST
/api/v1/telescope/path``, ``/move-to``, ``/abort``, ``GET .../acu/status``), shaped
after ``observatory-control-system/api/app/telescope.py``. Each handler records the
JSON it received on ``app.state.recorder`` and returns 200, so a test can assert
what the task POSTed. The ``/api/v1/telescope`` prefix matches the PCS client's
``url_prefix``, so a client at this app's base URL hits the same routes as the real
OCS proxy. Used by the dispatch tests via FastAPI's ``TestClient`` (in-process, no
live server); import is guarded so the suite runs without FastAPI.
"""

from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, conlist

PREFIX = "/api/v1/telescope"


class Recorder:
    """Collects the requests the mock received, for test assertions."""

    def __init__(self) -> None:
        self.path_bodies: list[dict] = []
        self.move_to_bodies: list[dict] = []
        self.abort_count: int = 0


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
        request.app.state.recorder.move_to_bodies.append(param.model_dump())
        return {"status": "ok", "message": "moving"}

    @app.post(f"{PREFIX}/path")
    async def path(param: PathParameters, request: Request):
        request.app.state.recorder.path_bodies.append(param.model_dump())
        return {"status": "ok", "message": "path accepted"}

    @app.post(f"{PREFIX}/abort")
    async def abort(request: Request):
        request.app.state.recorder.abort_count += 1
        return {"status": "ok", "message": "aborted"}

    @app.get(f"{PREFIX}/acu/status")
    async def acu_status(request: Request):
        # Drained StatusGeneral8100-shaped dict (raw ACU aliases) so a
        # completion-poll test can assert the normal-exit path: free stack
        # at maxFreeProgramTrackStack-1 (9999) with zero axis velocities is the
        # "scan complete (stack drained)" signal the constant_el_scan poll uses.
        return {
            "Qty of free program track stack positions": 9999,
            "Azimuth current velocity": 0.0,
            "Elevation current velocity": 0.0,
            "Azimuth current position": 0.0,
            "Elevation current position": 60.0,
        }

    return app
