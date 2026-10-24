"""Two real Sonic -> Ink -> Sonic turns; run from the repository root."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from time import perf_counter

from cartesia import AsyncCartesia
from dotenv import load_dotenv
from websockets.asyncio.client import ClientConnection, connect

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VOICE = "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"
FRAME_BYTES = 3200


async def synthesize(api_key: str) -> bytes:
    voice_id = os.getenv("FOX_VOICE_ID", "").strip() or DEFAULT_VOICE
    async with AsyncCartesia(api_key=api_key) as client:
        async with client.tts.websocket_connect() as sonic:
            ctx = sonic.context(
                model_id="sonic-3.6", voice=voice_id, timeout=10,
                output_format={
                    "container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000,
                },
            )
            await ctx.push("Hello world.")
            await ctx.no_more_inputs()
            chunks: list[bytes] = []
            async for event in ctx.receive():
                if event.type == "error":
                    raise RuntimeError(f"Sonic synthesis failed: {event.error}")
                if event.type == "chunk" and event.audio:
                    chunks.append(event.audio)
    audio = b"".join(chunks)
    if not audio or len(audio) % 2:
        raise AssertionError("Sonic did not return valid 16 kHz PCM")
    return audio


async def send_audio(
    ws: ClientConnection, audio: bytes, turn_end: asyncio.Event,
) -> None:
    loop = asyncio.get_running_loop()
    next_send = loop.time()
    offset = 0
    async with asyncio.timeout(12):
        while not turn_end.is_set():
            frame = audio[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b"\0")
            await ws.send(frame)
            offset += FRAME_BYTES
            next_send += 0.1
            delay = max(0, next_send - loop.time())
            try:
                await asyncio.wait_for(turn_end.wait(), timeout=delay)
            except TimeoutError:
                pass


async def receive_reply(
    ws: ClientConnection, turn_end: asyncio.Event, turn_number: int,
) -> None:
    ended_at: float | None = None
    got_audio = False
    async with asyncio.timeout(45):
        async for message in ws:
            if isinstance(message, bytes):
                if message and not got_audio:
                    if ended_at is None:
                        raise AssertionError("Audio arrived before turn_end")
                    print(
                        f"Turn {turn_number}: turn_end -> first audio byte: "
                        f"{(perf_counter() - ended_at) * 1000:.0f} ms",
                        flush=True,
                    )
                    got_audio = True
                continue
            event = json.loads(message)
            if event["type"] == "error":
                raise RuntimeError(event["message"])
            if event["type"] == "turn_end":
                if ended_at is not None:
                    raise AssertionError("Duplicate turn_end while gated")
                if "hello" not in event["text"].lower():
                    raise AssertionError(f"Expected 'hello', got {event['text']!r}")
                ended_at = perf_counter()
                turn_end.set()
            elif event["type"] == "speaking_end":
                if ended_at is None or not got_audio:
                    raise AssertionError("speaking_end arrived without turn_end/audio")
                await ws.send(json.dumps({"type": "playback_done"}))
                return
    raise AssertionError("Connection closed before speaking_end")


async def check_turn(ws: ClientConnection, audio: bytes, turn_number: int) -> None:
    turn_end = asyncio.Event()
    tasks = [
        asyncio.create_task(send_audio(ws, audio, turn_end)),
        asyncio.create_task(receive_reply(ws, turn_end, turn_number)),
    ]
    try:
        # A successful sender leaves the receiver running; failures cancel both.
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    await asyncio.to_thread(load_dotenv, ROOT / ".env")
    api_key = os.getenv("CARTESIA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("CARTESIA_API_KEY missing")
    async with asyncio.timeout(30):
        audio = await synthesize(api_key)
    async with connect("ws://localhost:8000/ws", max_size=None) as ws:
        for turn_number in (1, 2):
            await check_turn(ws, audio, turn_number)
    print("PASS: two turns transcribed, replied, and acknowledged")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
