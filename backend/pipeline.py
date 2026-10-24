"""Half-duplex mic -> Ink -> echo reply -> Sonic, with owned task lifetimes."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from cartesia import AsyncCartesia
from cartesia.lib._tts import AsyncTTSResourceConnection, AsyncWebSocketContext
from cartesia.resources.stt.auto_finalize import AsyncAutoFinalizeResourceConnection
from dotenv import load_dotenv
from fastapi import WebSocket, WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOICE = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"
LOG = logging.getLogger("uvicorn.error")
TTS_TIMEOUT = 30.0


def ink_options() -> dict[str, float]:
    """Leave endpointing defaults to Ink unless an override is provided."""
    options: dict[str, float] = {}
    for name in ("turn_end_threshold", "turn_end_timeout_ms"):
        raw = os.getenv(f"INK_{name.upper()}", "").strip()
        if not raw:
            continue
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"INK_{name.upper()} must be finite and nonnegative")
        if name == "turn_end_threshold" and value > 1:
            raise ValueError("INK_TURN_END_THRESHOLD must be between 0 and 1")
        options[name] = value
    return options


@dataclass
class Turn:
    started: float = field(default_factory=perf_counter)
    playback_done: asyncio.Event = field(default_factory=asyncio.Event)
    audio_bytes: int = 0
    finished: bool = False


class Session:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.mic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
        self.turn: Turn | None = None
        self.send_lock = asyncio.Lock()

    async def send(self, message: dict[str, object] | bytes) -> None:
        try:
            async with self.send_lock:
                if isinstance(message, bytes):
                    await self.ws.send_bytes(message)
                else:
                    await self.ws.send_json(message)
        except (RuntimeError, OSError) as exc:
            raise WebSocketDisconnect() from exc

    async def error(self, message: str) -> None:
        LOG.warning("[voice] %s", message)
        await self.send({"type": "error", "message": message})

    def release(self, turn: Turn, reason: str) -> None:
        if self.turn is turn:
            self.turn = None
            turn.playback_done.set()
            LOG.info("[gate] released: %s", reason)

    async def browser_receiver(self) -> None:
        """The only browser reader; never block control messages on Ink I/O."""
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            audio = message.get("bytes")
            if audio is not None:
                if len(audio) % 2:
                    await self.error("Mic PCM must contain complete 16-bit samples")
                    continue
                if self.mic.full():
                    self.mic.get_nowait()  # Bound latency during a reconnect.
                self.mic.put_nowait(bytes(len(audio)) if self.turn else audio)
            elif message.get("text"):
                try:
                    control = json.loads(message["text"])
                except (ValueError, TypeError):
                    await self.error("Expected a JSON control message")
                    continue
                if isinstance(control, dict) and control.get("type") == "playback_done":
                    if self.turn is not None and self.turn.finished:
                        self.release(self.turn, "playback_done")

    async def mic_to_ink(self, ink: AsyncAutoFinalizeResourceConnection) -> None:
        while True:
            audio = await self.mic.get()
            # Recheck at send time: queued pre-turn audio must also be gated.
            await ink.send_raw(bytes(len(audio)) if self.turn else audio)

    async def ink_events(
        self,
        ink: AsyncAutoFinalizeResourceConnection,
        sonic: AsyncTTSResourceConnection,
        tasks: asyncio.TaskGroup,
        voice_id: str,
    ) -> None:
        async for event in ink:
            if event.type == "error":
                raise RuntimeError(event.message)
            if event.type == "turn.update" and self.turn is None:
                await self.send({
                    "type": "transcript", "text": event.transcript, "final": False,
                })
            elif event.type == "turn.end" and self.turn is None:
                text = event.transcript.strip()
                if not text:
                    continue
                turn = Turn()
                self.turn = turn  # Set before any await or outbound event.
                await self.send({"type": "turn_end", "text": text})
                tasks.create_task(self.reply(sonic, voice_id, text, turn))
            # connected/start/eager_end/resume need no browser event.
        raise ConnectionError("Ink connection closed")

    async def ink_connection(
        self,
        ink: AsyncAutoFinalizeResourceConnection,
        sonic: AsyncTTSResourceConnection,
        tasks: asyncio.TaskGroup,
        voice_id: str,
    ) -> None:
        workers = [
            asyncio.create_task(self.mic_to_ink(ink)),
            asyncio.create_task(self.ink_events(ink, sonic, tasks, voice_id)),
        ]
        try:
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def ink_lifecycle(
        self,
        client: AsyncCartesia,
        sonic: AsyncTTSResourceConnection,
        tasks: asyncio.TaskGroup,
        voice_id: str,
    ) -> None:
        options = ink_options()
        for attempt in range(2):
            try:
                # Never opt into SDK reconnect: its PCM queue decodes UTF-8.
                async with client.stt.auto_finalize.websocket(
                    model="ink-2", encoding="pcm_s16le", sample_rate=16000,
                    max_retries=0, **options,
                ) as ink:
                    await self.ink_connection(ink, sonic, tasks, voice_id)
            except WebSocketDisconnect:
                raise
            except Exception as exc:
                if attempt == 0:
                    LOG.warning("[voice] Ink dropped; reopening once: %s", exc)
                    while not self.mic.empty():
                        self.mic.get_nowait()
                else:
                    await self.error(f"Ink unavailable after one reopen: {exc}")
        # Keep the browser session alive after reporting exhaustion. The browser
        # reader still handles disconnects; no hidden retry loop burns quota.

    async def stream_reply(self, ctx: AsyncWebSocketContext, turn: Turn) -> None:
        done = False
        async for event in ctx.receive():
            if event.type == "error":
                raise RuntimeError(str(event.error))
            if event.type == "done":
                done = True
            if event.type == "chunk" and event.audio:
                if turn.audio_bytes == 0:
                    LOG.info(
                        "[latency] turn.end -> first sonic chunk: %.0f ms",
                        (perf_counter() - turn.started) * 1000,
                    )
                turn.audio_bytes += len(event.audio)
                await self.send(event.audio)
        if not done:
            raise ConnectionError("Sonic ended without a done event")

    async def reply(
        self, sonic: AsyncTTSResourceConnection, voice_id: str, text: str, turn: Turn,
    ) -> None:
        ctx: AsyncWebSocketContext | None = None
        succeeded = False
        try:
            await self.send({
                "type": "speaking_start", "speaker": "fox", "sample_rate": 24000,
            })
            async with asyncio.timeout(TTS_TIMEOUT):
                ctx = sonic.context(
                    model_id="sonic-3.6", voice=voice_id, timeout=10,
                    output_format={
                        "container": "raw", "encoding": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                )
                await ctx.push(f"I heard you say: {text}")
                await ctx.no_more_inputs()
                await self.stream_reply(ctx, turn)
            succeeded = True
        except WebSocketDisconnect:
            raise
        except Exception as exc:
            self.release(turn, "TTS error")
            await self.error(f"Sonic reply failed: {type(exc).__name__}: {exc}")
        finally:
            if ctx is not None and not succeeded:
                try:
                    async with asyncio.timeout(1):
                        await ctx.cancel()
                except Exception:
                    pass  # The socket may already be closed.
        turn.finished = True
        await self.send({"type": "speaking_end"})
        if succeeded:
            try:
                # Conservative fallback measured from speaking_end; already
                # played chunks count too, so a slow browser is not cut off.
                await asyncio.wait_for(
                    turn.playback_done.wait(), turn.audio_bytes / 48000 + 3,
                )
            except TimeoutError:
                self.release(turn, "timeout (reply audio seconds + 3s)")

    async def cartesia_session(self, api_key: str) -> None:
        voice_id = os.getenv("FOX_VOICE_ID", "").strip() or DEFAULT_VOICE
        try:
            async with AsyncCartesia(api_key=api_key) as client:
                async with client.tts.websocket_connect() as sonic:
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(self.ink_lifecycle(client, sonic, tasks, voice_id))
                        # Own Sonic until the browser reader cancels this task.
                        await asyncio.Future()
        except* WebSocketDisconnect:
            raise
        except* Exception as exc:
            await self.error(f"Cartesia session failed: {exc}")


async def run_session(ws: WebSocket) -> None:
    session = Session(ws)
    try:
        await asyncio.to_thread(load_dotenv, ROOT / ".env")
        api_key = os.getenv("CARTESIA_API_KEY", "").strip()
        if not api_key:
            await session.error("CARTESIA_API_KEY missing")
            await ws.close(code=1011)
            return
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(session.browser_receiver())
            tasks.create_task(session.cartesia_session(api_key))
    except* WebSocketDisconnect:
        LOG.info("[voice] browser disconnected")
    except* Exception as exc:
        LOG.warning("[voice] session stopped: %s", exc)
        try:
            await session.error("Voice session stopped; please reconnect")
        except WebSocketDisconnect:
            pass
