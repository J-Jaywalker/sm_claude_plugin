# C.R.A.B — Claude Realtime Audio Bot

```
  ___    ____      __      ____
 / __)  (  _ \    /__\    (  _ \
( (__    )   /   /(__)\    ) _ <
 \___)()(_)\_)()(__)(__)()(____/

--{ via Speechmatics }--
```

A voice-controlled interface for [Claude Code](https://claude.ai/code), powered by [Speechmatics](https://speechmatics.com) Realtime ASR. Say **"CRAB-BOT"** to wake it, speak your command, pause — and Claude responds.

## Features

- **Voice-to-Claude** — streams microphone audio to Speechmatics RT, assembles prompts on end-of-utterance, and submits them to `claude -p`
- **Full TUI** — Textual-based interface with live visualiser, scrollable conversation history, and a typed-command fallback
- **Speaker enrollment** — enrols your voice on first run; ignores unrecognised speakers when enrolled
- **Text-to-speech** — Claude's responses are read back using macOS `say`
- **Local wake word** — optionally use an OpenWakeWord ONNX model to avoid streaming silence to Speechmatics
- **Settings panel** — configure endpoint, audio device, TTS, wake word, and enrolled speakers at runtime

## Requirements

- Python 3.11+
- macOS (TTS uses `say`; Linux/Windows users can disable TTS in settings)
- A [Speechmatics API key](https://portal.speechmatics.com)
- Claude Code installed and authenticated (`claude` on your PATH)
- `pyaudio` system dependency — on macOS: `brew install portaudio`

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

On first launch, CRAB-BOT will run a 30-second speaker enrollment session to capture your voice for speaker identification. Speak naturally into your microphone. Enrollment data is saved to `speakers.txt` and reused on subsequent runs.

Set `DEBUG=1` to show raw ASR events in the UI.

## How to use

1. Wait for the visualiser to show **Idle**
2. Say **"CRAB-BOT"** — the visualiser switches to **Listening** (you'll hear a ping)
3. Speak your command naturally
4. Pause — end-of-utterance is detected automatically (you'll hear a pop)
5. Wait for Claude to respond; the full exchange appears in the conversation panel
6. Say **"CRAB-BOT"** again for your next command

You can also type commands directly into the input bar at the bottom.

## Settings

Press the **Settings** button (top-right) to open the settings panel. Changes take effect immediately after saving; the ASR session is restarted automatically.

| Setting | Description |
|---|---|
| Transcription Endpoint | Custom Speechmatics RT URL (leave blank for cloud default) |
| Audio Input Device | Select microphone; a live level meter confirms audio is being captured |
| Enrolled Speakers | Add or remove speaker enrollments |
| Text-to-Speech | Enable/disable and choose provider |
| Local Wake Word | Use an ONNX model for offline wake detection (see below) |

ESC saves and closes settings. The Cancel button discards changes.

## Local wake word

By default, CRAB-BOT streams audio continuously to Speechmatics and uses the transcript to detect the wake phrase. The **local wake word** option runs an offline ONNX model instead, only connecting to Speechmatics once the wake word fires — saving API cost.

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

Use TTS to synthesise ~500+ variations of "crab-bot" across different voices and speeds:

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

Download a sample of [Mozilla Common Voice](https://commonvoice.mozilla.org/en/datasets) English clips and background noise (e.g. FSD50K) as negatives. Normalise all audio to 16kHz mono:

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
| `assets/crab_art.txt` | ASCII art for the visualiser (idle, listening, thinking) and the title panel |
| `assets/system_prompt.md` | System prompt prepended to every Claude session |

## Project structure

```
crab/
  asr/
    controller.py     # wake word state machine, prompt assembly
    devices.py        # lists available audio input devices
    enrollment.py     # 30-second speaker enrollment flow
    pumps.py          # async audio pump to Speechmatics
    wake_word.py      # OpenWakeWord detector
  claude/
    driver.py         # spawns `claude -p`, streams JSON output
    stream.py         # parses claude stream-json events
  tts/
    macos.py          # macOS `say` TTS provider
  ui/
    app.py            # Textual TUI app
    modals.py         # settings and enrollment modal screens
    rendering.py      # rich chat bubble renderer
    widgets.py        # settings panel widget
  config.py           # constants, asset loaders, regex patterns
  speaker_store.py    # load/save speakers.txt
voice_controller.py   # entry point
```
