# Project Dependencies

| Package | Version Pin | Category | Purpose |
|---|---|---|---|
| `speechmatics-rt` | latest | Core / ASR | Speechmatics real-time speech recognition SDK. Streams audio to the Speechmatics API and returns live transcription results. The heart of the voice input pipeline. |
| `pyaudio` | latest | Audio I/O | Python bindings for PortAudio. Captures raw microphone input and feeds the audio stream into the ASR and wake-word pipelines. |
| `rich` | latest | Terminal UI | Advanced terminal formatting — colours, panels, tables, markdown rendering, progress bars. Used for styled console output throughout the UI. |
| `textual` | latest | Terminal UI | Full TUI (Text User Interface) framework built on top of Rich. Provides the widget system, layout engine, and event loop that powers the interactive terminal chat interface. |
| `typing_extensions` | latest | Utility | Backport of newer Python `typing` features (e.g. `Self`, `TypeAlias`, `override`) for compatibility with Python versions that don't yet ship them in stdlib. |
| `openwakeword` | latest | Wake Word | Open-source wake-word / keyword detection. Listens passively to the microphone and triggers the active recording session when the configured wake phrase is detected. |
