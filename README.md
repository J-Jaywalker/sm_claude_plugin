# sm_claude_plugin

Enables voice-only mode for Claude Code using Speechmatics Realtime ASR.

Say **"Alright Claude"** to start dictating. When you stop speaking, the transcript is submitted as a Claude Code prompt.

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

The controller prints state changes as it runs:

```
[READY] Listening. Say 'Alright Claude' to begin.

[STATE] Wake word detected -> ACCUMULATING
[FINAL] what files are in this directory
[PROMPT READY] -> what files are in this directory
[STATE] Resuming listening...
```

Press `Ctrl+C` to exit.

## Current status

**Phase 1 (STT only)** — wake word detection, transcript accumulation, and end-of-utterance signalling are implemented. The assembled prompt is printed but not yet forwarded to Claude Code.

**Phase 2 (Claude integration)** — PTY wrapper to submit prompts to a live `claude` session and handle yes/no prompts by voice. Not yet implemented.
