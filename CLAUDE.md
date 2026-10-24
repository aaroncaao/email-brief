# STORY QUEST — CLAUDE.md

> Speak the Story Into Existence. A voice-powered interactive storytelling game for children ages 6–9 that builds speaking, verbal reasoning, and narrative skills. Built at Cal Hacks 13.0 on Cartesia (Sonic TTS + Ink STT).

Read this file fully before making changes. When in doubt, favor **a working, reliable demo** over feature completeness. This is a hackathon: speed and demo reliability win.

---

## 1. Project Goal

Children talk to AI characters (Fox and Owl) with their voice. Instead of picking from predefined options, the child's spoken ideas drive the story. Characters ask adaptive follow-up questions that nudge the child to expand sentences and explain their reasoning, then the child retells the adventure at the end.

**The demo moment (everything serves this):**
A child says "Boat!" → Owl asks "What should we build it with?" → child says "Wood, because it floats!" → Fox says "Perfect! Let's build a wooden boat!" → the story visibly changes.

If a task doesn't make that moment better, faster, or more reliable, deprioritize it.

## 2. MVP Scope (do NOT exceed)

Build ONE complete adventure with:
1. Two AI characters (Fox, Owl) with distinct Cartesia Sonic voices
2. Real-time voice conversation (Ink in, Sonic out)
3. Adaptive follow-up questions (coach behavior)
4. Story progression driven by the child's spoken ideas
5. Final story-retelling activity with short feedback

**Out of scope:** accounts/auth, databases, multiple adventures, parent dashboards, analytics, payments, mobile apps, live image generation, real child-safety certification. Do not build these. Educational effectiveness is "to be validated through future testing" — never claim proven outcomes in UI copy or the pitch.

## 3. Tech Stack

| Layer | Choice |
|---|---|
| STT | Cartesia **Ink** (streaming WebSocket) |
| TTS | Cartesia **Sonic** (streaming WebSocket), one voice ID per character |
| Backend | **Python 3.11+ / FastAPI**, WebSocket endpoint |
| Pipeline | Hand-rolled async loop, or **Pipecat** if adopted (decide once, don't mix) |
| LLM | Claude via Anthropic SDK, **streaming**, Haiku-class model for latency |
| VAD / turn-taking | Silero VAD or Ink endpointing + ~800–1000 ms silence timeout |
| State | In-memory Python dict per session (no DB) |
| Frontend | React + Vite (or Next.js), Web Audio API / AudioWorklet for mic PCM |
| Deploy | Local for demo; ngrok if a public URL is needed |

Keep API keys in `.env` (never commit): `CARTESIA_API_KEY`, `ANTHROPIC_API_KEY`, `FOX_VOICE_ID`, `OWL_VOICE_ID`.

## 4. Architecture

```
Browser (mic PCM) ──WS──▶ FastAPI ──▶ Ink (STT, partial + final transcripts)
                                   │
                                   ▼
                         Turn detector (VAD / endpointing)
                                   │ final child utterance
                                   ▼
                      Story Director + Claude (streaming)
                                   │ sentence-by-sentence text
                                   ▼
                         Sonic TTS (per-character voice)
                                   │ audio chunks
Browser (speaker) ◀──WS────────────┘  + UI events (speaker, scene, state)
```

Principles:
- **Stream everything.** Never wait for a full LLM reply before TTS. Flush to Sonic at sentence boundaries.
- **Target latency:** child stops talking → first audio out in **< 1.5 s**. Measure it, log it.
- **Barge-in:** if the child starts speaking while a character talks, stop playback and cancel the in-flight TTS/LLM.
- **One source of truth for session state** (see §6). The frontend is a thin renderer driven by server events.

## 5. Suggested Repo Layout

```
story-quest/
├── CLAUDE.md
├── .env.example
├── backend/
│   ├── main.py              # FastAPI app, WS endpoint
│   ├── pipeline.py          # mic → Ink → LLM → Sonic orchestration
│   ├── cartesia_client.py   # Ink + Sonic WS wrappers
│   ├── director.py          # story state machine + prompt assembly
│   ├── prompts/
│   │   ├── fox.md
│   │   ├── owl.md
│   │   └── director.md
│   ├── story/
│   │   └── river_bridge.json  # the one adventure: scenes, beats, fallbacks
│   ├── state.py             # SessionState dataclass
│   └── tests/
├── frontend/
│   └── src/
│       ├── App.tsx
│       ├── audio/           # mic capture, playback queue
│       ├── components/      # MicButton, CharacterAvatar, SceneView, Retell
│       └── assets/scenes/   # pre-made static scene images
└── scripts/
    └── latency_test.py
```

## 6. Session State

```python
@dataclass
class SessionState:
    scene_id: str                      # current scene in the adventure
    beat_index: int                    # progress through scene beats
    child_ideas: dict[str, str]        # e.g. {"crossing": "boat", "material": "wood because it floats"}
    history: list[Turn]                # compact transcript (speaker, text)
    follow_ups_in_scene: int           # cap to avoid interrogation loops
    phase: Literal["intro","adventure","retell","done"]
```

Rules:
- Store the child's **ideas and reasons**, not just raw text. Characters must later reference them ("Remember your wooden boat?").
- Inject `child_ideas` + current scene + last ~6 turns into every LLM call. Keep prompts small for latency.
- Cap follow-ups per scene (max 2) then advance the story. A kid should never feel stuck.

## 7. The Adventure (hardcoded for MVP)

One short story (~5 scenes, 4–6 minutes): Fox and Owl reach a broken bridge over a river → child's idea gets them across → a second problem (e.g., a lost lantern / dark cave) → child solves it → happy ending → retell.

Each scene in `river_bridge.json` defines: `setup_line`, `speaker`, `goal_question`, `acceptable_idea_types`, `fallback_hint`, `next_scene`. The LLM improvises **within** the scene; the JSON keeps the story bounded and demo-safe. The "bridge/boat" scene must be the most polished path.

## 8. LLM Output Contract

Each turn, Claude returns **strict JSON** (parse defensively, fall back to a safe canned line on failure):

```json
{
  "speaker": "fox" | "owl",
  "line": "text to speak, 1–2 short sentences",
  "follow_up_type": "expand_sentence" | "ask_why" | "advance_story" | "retell_prompt" | "encourage",
  "idea_update": {"key": "value"} ,
  "advance_scene": false
}
```

- Use streaming + incremental parsing, or have the model emit `line` first so TTS can start early.
- Temperature low (0.3–0.6) for reliability. Max tokens small (~120).

## 9. Character & Prompt Guidelines

**Audience: ages 6–9.** Language rules for all character output:
- 1–2 short sentences per turn. Vocabulary at a 1st–3rd grade level. No sarcasm, no scary content, no violence.
- Always end with a clear, answerable question or invitation, except in celebration beats.
- Warm, curious, encouraging. Praise the *thinking* ("I love that you told us why!"), not just the answer.

**Fox** — energetic, playful, quick, optimistic. Drives action. Bright, faster voice.
**Owl** — calm, wise, gentle. Asks the "why/how" scaffolding questions. Slower, warmer voice.

**Adaptive Speaking Coach behavior** (language scaffolding, dialogic-reading inspired):
- Child gives 1–2 words ("Boat!") → affirm, then **expand**: "Great idea! What should we build it with?"
- Child gives an idea with no reason → ask "Why do you think that will work?"
- Child gives a reason → **model a fuller sentence** back: "So the boat floats *because* wood is lighter than water. Nice thinking!"
- Child is silent/unsure → offer a gentle hint or two simple choices, never pressure.
- Never correct grammar bluntly. Recast naturally instead.

**Story Memory:** callbacks to earlier ideas are a headline feature. Prefer including at least one callback per later scene.

## 10. Voice / Audio Conventions

- Sonic: one voice ID per character; tune `speed`/emotion per character. Keep voice IDs in env/config, never hardcoded in logic.
- Audio format: PCM 16-bit, consistent sample rate end-to-end (confirm Ink/Sonic supported rates and stay consistent; mismatches cause garbled audio).
- Playback: queue audio chunks, support flush on barge-in.
- Kids pause mid-sentence. Prefer **longer silence thresholds** over cutting them off. Never interrupt a child who is still thinking.
- Pronunciation/expressive speaking activities are **optional/stretch**. Only attempt after the core loop is solid.

## 12. Retelling Activity

After the final scene, the child retells the story. Backend sends the retell transcript + stored `child_ideas` and scene summaries to Claude, which returns:
- One specific, positive feedback line (e.g., "You told it in order and used 'because' twice!")
- Optionally one gentle "next time try" suggestion
Keep it short, spoken aloud by Owl, and shown on screen.

## 13. Safety & Privacy (hackathon-appropriate)

- Child-directed product: keep all character output age-appropriate; add a system-prompt guard and a simple output filter.
- Do **not** persist audio or transcripts beyond the session. No analytics, no third-party trackers.
- Don't ask the child for personal info (name, school, address). Characters must deflect if the child volunteers it.
- Never claim clinical or proven educational effectiveness.

## 14. Coding Conventions

- Python: type hints everywhere, `async`/`await` for all I/O, `ruff` + `black`. Small functions, clear names.
- TypeScript: strict mode, functional components, no `any` unless justified.
- Log latency at each hop (STT final → LLM first token → first TTS audio). Print to console in a readable format.
- Fail soft: on any API error, speak a safe canned line ("Hmm, let me think about that…") and retry once. Never crash the session.
- Prefer small, working increments. Keep the app runnable at every commit.
- No premature abstractions. No new dependencies without a clear reason.

## 15. Build Order (follow this sequence)

1. **Hello loop:** mic → Ink → Claude → Sonic → speaker, single character. Get latency < 1.5 s.
2. Add second character + speaker routing + distinct voices.
3. Add session state + story JSON + scene advancement.
4. Implement follow-up/coach logic (expand, ask why, recast).
5. Polish the **"Boat!" demo path** until it works every time.
6. Add barge-in handling.
7. Add retelling activity + feedback.
8. UI polish, scene art, transcript display.
9. Demo rehearsal, fallback plan, pre-recorded backup video.

## 16. Demo-Day Checklist

- [ ] Wired headset or good directional mic; test in a loud room
- [ ] Rehearsed "Boat!" path works 10/10 times
- [ ] Backup: pre-recorded screen capture of a successful run
- [ ] `.env` keys verified, rate limits/credits checked
- [ ] Latency numbers ready to cite
- [ ] Pitch lines ready: *Voice is the core of the experience, not an add-on*; *We don't just tell children stories. We help them become storytellers.*

## 17. How Claude Should Work in This Repo

- Ask before large refactors or adding dependencies; otherwise act and keep moving.
- When changing prompts, test against the "Boat!" scenario and at least two off-script child inputs (silence, a nonsense answer, an unexpected idea like "a dragon!").
- When something is slow, measure first, then optimize the bottleneck (usually LLM first-token or endpointing delay).
- Keep answers and diffs concise. Explain what changed and how to run it.
- If a requested feature is outside the MVP scope in §2, flag it and suggest a stretch-goal note instead of building it.