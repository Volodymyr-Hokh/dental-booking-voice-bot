"""FastAPI app: static test page + WebRTC signaling for the voice bot."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import STUN_SERVER, settings
from bot import run_bot

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection  # noqa: E402

# Active connections keyed by pc_id (used for reconnect/renegotiation).
_connections: dict[str, SmallWebRTCConnection] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    for c in list(_connections.values()):
        await c.disconnect()
    _connections.clear()


app = FastAPI(lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
async def client_config():
    """Runtime config for the browser client (keeps the STUN server single-sourced)."""
    return {"stun_server": STUN_SERVER}


class OfferRequest(BaseModel):
    sdp: str
    type: str
    pc_id: str | None = None
    restart_pc: bool = False


@app.post("/api/offer")
async def offer(payload: OfferRequest, background_tasks: BackgroundTasks):
    pc_id = payload.pc_id

    if pc_id and pc_id in _connections:
        conn = _connections[pc_id]
        logger.info("Renegotiating connection %s", pc_id)
        await conn.renegotiate(sdp=payload.sdp, type=payload.type, restart_pc=payload.restart_pc)
    else:
        conn = SmallWebRTCConnection(ice_servers=[STUN_SERVER])
        await conn.initialize(sdp=payload.sdp, type=payload.type)

        @conn.event_handler("closed")
        async def _closed(c: SmallWebRTCConnection):
            logger.info("Connection %s closed", c.pc_id)
            _connections.pop(c.pc_id, None)

        _connections[conn.pc_id] = conn
        background_tasks.add_task(run_bot, conn)

    answer = conn.get_answer()
    return answer


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
