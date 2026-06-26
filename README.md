# C.R.A.B — Claude Realtime Audio Bot

```
  ___    ____      __      ____
 / __)  (  _ \    /__\    (  _ \
( (__    )   /   /(__)\    ) _ <
 \___)()(_)\_)()(__)(__)()(____/

--{ via Speechmatics }--
```

A hands-free voice interface for [Claude Code](https://claude.ai/code), powered by [Speechmatics](https://speechmatics.com) Realtime ASR. Say **"CRAB-BOT"** to wake it, speak your command, pause — Claude responds in your headphones and a Textual chat UI.

When Claude wants to edit a file, run a command, or take any action that needs permission, it speaks the question aloud — you answer **"yes"** or **"no"** by voice. When a decision genuinely needs a multi-choice menu, a small click-to-select modal appears. Read-only tools (Read / Glob / Grep) flow through without a prompt.

## Architecture at a glance

```
voice_controller.py  ◄── single user-facing surface
├── Textual TUI (crab visualiser, chat bubbles, settings, menu modal)
├── Speechmatics RT ASR (mic, wake word, end-of-utterance)
├── TTS (macOS `say`) + voice permission listener
└── Unix socket  /tmp/crab-bot.sock
       │
       ▼
crab.channel.server   ◄── spawned by Claude as its MCP stdio child
hand-rolled JSON-RPC; declares experimental.claude/channel + .../permission
       │
       ▼ stdio (MCP)
Claude Code (hidden PTY)  ◄── launched with
                              --dangerously-load-development-channels server:crab
```

There is **no terminal interaction with Claude** — every prompt arrives over the channel, every response comes back through the channel's `reply` tool, and every permission request is relayed to your voice.

## Features

- **Voice-to-Claude** — Speechmatics RT streams, wake word + end-of-utterance, prompts pushed to Claude over a custom MCP channel
- **Long-running session** — one interactive Claude Code instance for the whole run (no per-turn subprocess)
- **Voice permission relay** — Edit / Write / Bash and other approval-required tools are read out aloud and gated by voice yes/no
- **Click-to-select menu fallback** — Claude calls `ask_menu` when a choice can't be reduced to yes/no; a Textual modal pops up
- **Speaker enrollment** — first-run voice capture; transcripts from unrecognised speakers are ignored
- **TTS summary** — Claude's `<tts>` block is spoken by macOS `say` at end of turn
- **Local wake word** — optional offline ONNX model avoids streaming silence to the cloud
- **`Ctrl+T`** — toggles Textual's mouse capture so you can drag-to-select text natively

## Requirements

- Python 3.11+
- macOS (uses `say` for TTS; Linux/Windows can disable TTS in settings)
- **Claude Code ≥ 2.1.80** with channels available (`claude --version`)
- A [Speechmatics API key](https://portal.speechmatics.com)
- portaudio (for `pyaudio`): `brew install portaudio`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
export SPEECHMATICS_API_KEY=<your-key>
python voice_controller.py
```

On first launch, CRAB-BOT runs a 30-second speaker enrollment so it can ignore transcripts from other voices later. The result is saved to `speakers.txt`.

Set `DEBUG=1` to write a diagnostic log to `/tmp/crab-channel-debug.log` (events from the voice controller, channel driver, and MCP server, tagged with their source).

## How to use

1. Wait for the visualiser to show **Idle** (red crab).
2. Say **"CRAB-BOT"** — visualiser flips to **Listening** (green crab) and you hear a Ping.
3. Speak your command naturally — *"edit auth.py to use bcrypt"*, *"run the tests"*, *"what's in the README"*.
4. Pause — end-of-utterance fires (Pop sound). The visualiser shows **Thinking**.
5. If Claude wants to do something that needs permission, you hear **"Allow Write?"** (or Edit / Bash / etc.). Visualiser flips back to **Listening** and you hear another Ping — say **"yes please"** or **"no"**.
6. Claude finishes the work and the answer appears in the chat. The summary (the `<tts>` block) plays through your speakers.

You can also type into the input bar at the bottom if voice isn't an option.

## Settings

Press the **Settings** button (top-right) to open the panel. Changes take effect immediately — the ASR session restarts automatically.

| Setting | Description |
|---|---|
| Transcription Endpoint | Custom Speechmatics RT URL (blank = cloud default) |
| Audio Input Device | Microphone selector with live level meter |
| Enrolled Speakers | Add or remove speaker enrollments |
| Text-to-Speech | Enable / disable and pick provider |
| Local Wake Word | Use an ONNX model for offline wake detection |

ESC saves and closes settings. The Cancel button discards changes.

## Local wake word

By default CRAB-BOT streams audio continuously to Speechmatics and uses the cloud transcript to detect the wake phrase. The **local wake word** option runs an offline ONNX model first — Speechmatics is only opened once the wake word fires, saving API cost.

Built-in models (no training required):

| Model name | Wake phrase |
|---|---|
| `hey_jarvis_v0.1` | "Hey Jarvis" |
| `alexa_v0.1` | "Alexa" |
| `hey_mycroft_v0.1` | "Hey Mycroft" |
| `hey_rhasspy_v0.1` | "Hey Rhasspy" |

To use a built-in model, enter its name in the **Model** field in settings. To use a custom model, enter the full path to a `.onnx` file.

### Training a custom "crab-bot" model

**1. Set up a training environment**

```bash
python -m venv oww-train && source oww-train/bin/activate
pip install torch torchaudio openwakeword edge-tts
git clone https://github.com/dscripka/openWakeWord
```

**2. Generate positive samples**

Use TTS to synthesise ~500+ variations of "crab-bot" across voices and speeds:

```python
import asyncio, edge_tts, os

PHRASES = ["crab bot", "crab-bot", "crabbot", "hey crab bot"]
VOICES  = [
    "en-US-GuyNeural", "en-US-JennyNeural", "en-GB-RyanNeural",
    "en-GB-SoniaNeural", "en-AU-WilliamNeural", "en-IN-NeerjaNeural",
]

async def generate():
    os.makedirs("positive_samples", exist_ok=True)
    i = 0
    for phrase in PHRASES:
        for voice in VOICES:
            for rate in ["-10%", "+0%", "+10%"]:
                out = f"positive_samples/{i:04d}.wav"
                await edge_tts.Communicate(phrase, voice, rate=rate).save(out)
                i += 1

asyncio.run(generate())
```

**3. Gather negative samples**

Download a sample of [Mozilla Common Voice](https://commonvoice.mozilla.org/en/datasets) English clips and background noise (e.g. FSD50K) as negatives. Normalise everything to 16 kHz mono:

```bash
for f in positive_samples/*.wav negative_samples/*.wav; do
  ffmpeg -i "$f" -ar 16000 -ac 1 -y "${f%.wav}_16k.wav"
done
```

**4. Train**

Open `openWakeWord/notebooks/training_models.ipynb` in Jupyter, point it at your sample folders, set the model name to `crab_bot_v0.1`, and run. Training takes ~15 minutes on CPU.

**5. Use the model**

Copy the exported `crab_bot_v0.1.onnx` into the `assets/` folder, then set the **Model** field in settings to `assets/crab_bot_v0.1.onnx`. Start with a threshold of `0.5` and tune from there.

## Customisation

| File | Purpose |
|---|---|
| `CLAUDE.md` | CRAB-BOT persona + voice-first response rules + `<tts>` / `ask_menu` conventions. Loaded by Claude on every session start. |
| `assets/crab_art.txt` | ASCII art for the visualiser (idle / listening / thinking) and the title panel. |

## Project structure

```
crab/
  asr/
    controller.py     # wake-word state machine, prompt assembly, permission-listen mode
    devices.py
    enrollment.py     # 30-second speaker enrollment flow
    pumps.py
    wake_word.py      # OpenWakeWord ONNX detector (optional)
  channel/
    bridge.py         # Unix-socket protocol (parent ↔ MCP child)
    driver.py         # hidden-PTY Claude + socket pumps + permission relay
    mcp.json          # registers crab.channel.server as the channel for Claude
    server.py         # hand-rolled JSON-RPC MCP server; reply + ask_menu tools
    yes_no.py         # voice yes/no parser
  tts/
    macos.py          # macOS `say` provider
  ui/
    app.py            # Textual CrabApp
    modals.py         # settings, enrollment, ask_menu modals
    protocol.py       # _UI Protocol
    rendering.py      # rich chat-bubble renderer
    widgets.py        # SettingsPanel widget
  config.py
  speaker_store.py
CLAUDE.md             # project-level Claude persona / format / channel-trust rules
voice_controller.py   # entry point
```
