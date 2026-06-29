# Project Dependencies

## Python packages (`requirements.txt`)

| Package | Version Pin | Category | Purpose |
|---|---|---|---|
| `speechmatics-rt` | latest | Core / ASR | Speechmatics real-time speech recognition SDK. Streams audio to the Speechmatics API and returns live transcription results. The heart of the voice input pipeline. |
| `pyaudio` | latest | Audio I/O | Python bindings for PortAudio. Captures raw microphone input and feeds the audio stream into the ASR and wake-word pipelines. |
| `rich` | latest | Terminal UI | Advanced terminal formatting — colours, panels, tables, markdown rendering, progress bars. Used for styled console output throughout the UI. |
| `textual` | latest | Terminal UI | Full TUI (Text User Interface) framework built on top of Rich. Provides the widget system, layout engine, and event loop that powers the interactive terminal chat interface. |
| `typing_extensions` | latest | Utility (transitive) | Backport of newer Python `typing` features. Currently pulled in by `textual`/`rich`; no direct imports in this codebase, but listed so a slim install of those deps still resolves. |
| `openwakeword` | latest | Wake Word | Open-source wake-word / keyword detection. Listens passively to the microphone and triggers the active recording session when the configured wake phrase is detected. Pulls in `numpy` transitively (used directly by `crab/asr/wake_word.py`). |

The `crab.channel.server` MCP server is a **hand-rolled JSON-RPC stdio loop**
— it intentionally uses only the standard library so there is no Python-side
dependency on the official `mcp` SDK.

## External runtime requirements

| Requirement | Minimum | Why |
|---|---|---|
| Claude Code CLI (`claude`) | **2.1.80** | The channels research-preview feature; we invoke it via `--dangerously-load-development-channels server:crab`. Earlier versions reject the flag. |
| macOS `say` command | bundled | Text-to-speech for Claude's `<tts>` summaries and the voice permission prompt. Configurable in settings. |
| portaudio (system library) | latest | Required by `pyaudio` for microphone capture. On macOS: `brew install portaudio`. |
| Unix domain sockets | n/a | The bridge between `voice_controller.py` (parent) and `crab.channel.server` (Claude's MCP child) uses `/tmp/crab-bot.sock`. macOS and Linux only. |
