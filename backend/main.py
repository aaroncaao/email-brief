"""Local voice hello loop: uv run uvicorn main:app --reload."""

from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse

from pipeline import run_session

app = FastAPI(title="Story Quest")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def voice(ws: WebSocket) -> None:
    await ws.accept()
    await run_session(ws)
