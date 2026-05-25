"""FastAPI app: static test page + WebRTC signaling for the voice bot."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from bot import run_bot

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
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


@app.post("/api/offer")
async def offer(payload: dict, background_tasks: BackgroundTasks):
    pc_id = payload.get("pc_id")
    sdp = payload["sdp"]
    sdp_type = payload["type"]

    if pc_id and pc_id in _connections:
        conn = _connections[pc_id]
        logger.info("Renegotiating connection %s", pc_id)
        await conn.renegotiate(sdp=sdp, type=sdp_type, restart_pc=payload.get("restart_pc", False))
    else:
        conn = SmallWebRTCConnection(ice_servers=["stun:stun.l.google.com:19302"])
        await conn.initialize(sdp=sdp, type=sdp_type)

        @conn.event_handler("closed")
        async def _closed(c: SmallWebRTCConnection):
            logger.info("Connection %s closed", c.pc_id)
            _connections.pop(c.pc_id, None)

        _connections[conn.pc_id] = conn
        background_tasks.add_task(run_bot, conn)

    answer = conn.get_answer()
    return answer


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run("main:app", host=host, port=port, reload=False)
