# Story Quest

A minimal voice hello loop: microphone -> Cartesia Ink -> "I heard you say: ..."
-> Cartesia Sonic -> speaker. One Fox voice, with no LLM or frontend build step.

## Run locally (PowerShell)

Install Python 3.11+ and uv. From the repository root, create `.env` only if it
does not already exist:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Set `CARTESIA_API_KEY` in `.env`. `FOX_VOICE_ID` defaults to
`6ccbfb76-1fc6-48f7-b71d-91ac6298247b` when empty or unset. Owl and Anthropic
settings are reserved for later work. Optional `INK_TURN_END_THRESHOLD` and
`INK_TURN_END_TIMEOUT_MS` override Ink's defaults; leave them commented to omit
them entirely. Environment variables take precedence over `.env`.

```powershell
cd backend
```

```powershell
uv sync
```

```powershell
uv run uvicorn main:app --reload
```

Open <http://localhost:8000> in Chrome, click **Start**, and allow the microphone.
Say "Boat!", pause, and listen. A headset helps avoid acoustic echo. Microphone
access requires localhost or HTTPS. Keep one development session open at a time.

The `.env` and static page paths resolve relative to the Python source, not the
shell's working directory. The server and page can boot without a key; `/ws`
reports `CARTESIA_API_KEY missing` until configured.

## Smoke test

With the server running, open another PowerShell at the repository root:

```powershell
uv run --project backend python scripts/smoke_test.py
```

This makes real Cartesia requests: Sonic synthesizes "Hello world." as 16 kHz
PCM, then the script sends it in realtime, followed by silence until Ink ends
the turn (12-second deadline). It requires a transcript containing "hello",
reply audio, and `speaking_end`, then sends `playback_done`. It repeats on the
same connection for **two turns** and prints latency for each. Failure exits
nonzero. No microphone is needed and no audio/transcripts are saved.

## WebSocket contract

Connect to `ws://localhost:8000/ws` (use `wss` under HTTPS).

Client -> server:

| Frame | Contents |
| --- | --- |
| Binary | Mono signed 16-bit little-endian PCM at 16000 Hz. Send continuously, including silence; the test page sends exactly 1600 samples / 3200 bytes every 100 ms. |
| Text | `{"type":"playback_done"}` after the last reply source has finished playing, following `speaking_end`. Send immediately if no audio arrived. |

Server -> client, in reply order:

| Frame | Contents |
| --- | --- |
| Text | `{"type":"transcript","text":"...","final":false}` for each ungated Ink `turn.update`. Text is cumulative; replace the displayed transcript. |
| Text | `{"type":"turn_end","text":"..."}` for a nonempty final transcript. |
| Text | `{"type":"speaking_start","speaker":"fox","sample_rate":24000}` |
| Binary | Mono PCM s16le at **24000 Hz**, scheduled back-to-back for playback. |
| Text | `{"type":"speaking_end"}` means synthesis finished; queued playback may still be running. |
| Text | `{"type":"error","message":"..."}` may appear on a service/configuration error. Display the message; on a Sonic reply error, `speaking_end` also terminates the reply. |

**Half-duplex gate:** the server sets the gate before sending `turn_end`. While
gated, it forwards same-length zero frames to Ink and ignores additional final
turns. Ink events keep draining while Sonic runs in a separate task. The gate
releases on `playback_done` after `speaking_end`, or after **reply audio duration
plus 3 seconds measured from `speaking_end`**, whichever comes first. A TTS error
also releases it. The server logs the release reason. There is no barge-in.

One Sonic socket is reused for each browser session, with a new context per
reply. Ink is reopened at most once per session if it drops; SDK automatic
reconnect is disabled to avoid its queued-PCM UTF-8 decoding. If reopening fails
or Ink drops again, an error is sent and the browser connection remains open;
reload and click Start to retry. Disconnecting cancels session tasks and closes
the service sockets. Stalled TTS has a 10-second receive timeout and a 30-second
overall reply limit, reported as an error so the gate cannot stay stuck.

Server latency is logged as:

```text
[latency] turn.end -> first sonic chunk: N ms
```

The single-file test page keeps mic capture, playback, and transport/UI functions
separate so the UI teammate can port the audio code. Mic resampling uses the
actual AudioWorklet sample rate with a fractional box filter across blocks.
