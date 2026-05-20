"""Modal screens: speaker enrollment and full settings dialog."""

from __future__ import annotations

import asyncio
import math
import struct
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Select, Static, Switch

from speechmatics.rt import AudioFormat

from crab.asr.devices import list_input_devices
from crab.asr.enrollment import _enroll_speaker_tui
from crab.speaker_store import _load_speakers, _save_speakers
from crab.tts.macos import _TTS_PROVIDER_MACOS, _TTS_PROVIDER_PYTHON
from crab.ui.widgets import SettingsPanel  # noqa: F401  (kept for parity)

_DEVICE_DEFAULT = -1  # sentinel meaning "system default"
_LEVEL_WIDTH = 24
_LEVEL_SAMPLE_RATE = 16000
_LEVEL_CHUNK = 1024


def _render_level(level: float) -> str:
    filled = round(level * _LEVEL_WIDTH)
    color = "#29A383" if level < 0.6 else ("#FFC53D" if level < 0.85 else "#FF4444")
    bar = "█" * filled + "░" * (_LEVEL_WIDTH - filled)
    return f"Level  [{color}]{bar}[/{color}]"


# ---------------------------------------------------------------------------
# Enrollment modal
# ---------------------------------------------------------------------------

class EnrollModal(ModalScreen[dict[str, list[str]] | None]):
    """Sub-screen for the 30-second speaker recording flow."""

    CSS = """
    EnrollModal { align: center middle; }
    #enroll-container {
        width: 64; height: auto;
        background: $surface; border: round #29A383; padding: 1 2;
    }
    #enroll-title { text-align: center; color: #29A383; margin-bottom: 1; }
    #enroll-name  { margin-bottom: 1; }
    #enroll-start { width: 100%; margin-bottom: 1; }
    #enroll-status { text-align: center; }
    """

    def __init__(
        self,
        api_key: str,
        audio_format: AudioFormat,
        rt_url: str | None,
        device_index: int | None = None,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._audio_format = audio_format
        self._rt_url = rt_url
        self._device_index = device_index
        self._recording = False

    def compose(self) -> ComposeResult:
        with Vertical(id="enroll-container"):
            yield Label("Register New Speaker", id="enroll-title")
            yield Input(placeholder="Enter your name...", id="enroll-name")
            yield Button("Start 30s Recording", id="enroll-start", variant="success")
            yield Label("", id="enroll-status")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "enroll-start" and not self._recording:
            name = self.query_one("#enroll-name", Input).value.strip() or "owner"
            self._recording = True
            self.query_one("#enroll-start", Button).disabled = True
            asyncio.create_task(self._run(name))

    def on_key(self, event: Any) -> None:
        if event.key == "escape" and not self._recording:
            self.dismiss(None)

    async def _run(self, name: str) -> None:
        status = self.query_one("#enroll-status", Label)
        try:
            enrolled = await _enroll_speaker_tui(
                self._api_key, self._audio_format, name,
                lambda msg: status.update(msg),
                rt_url=self._rt_url,
                device_index=self._device_index,
            )
            status.update(f"[green]'{name}' enrolled successfully![/green]")
            await asyncio.sleep(1.5)
            self.dismiss(enrolled)
        except Exception as exc:
            status.update(f"[red]Error: {exc}[/red]")
            self._recording = False
            self.query_one("#enroll-start", Button).disabled = False


class SettingsModal(ModalScreen[None]):
    """Full settings screen: endpoint, speaker enrollment, TTS options."""

    CSS = """
    SettingsModal { align: center middle; }
    #settings-container {
        width: 72; height: auto; max-height: 90vh;
        background: $surface; border: round #29A383; padding: 1 2;
    }
    #settings-title {
        text-align: center; color: #29A383;
        text-style: bold; margin-bottom: 1;
    }
    .section-label { text-style: bold; color: $text; margin-top: 1; }
    .hint { color: $text-muted; margin-bottom: 1; }
    #endpoint-input { margin-bottom: 0; }
    #device-select { margin-top: 1; margin-bottom: 0; }
    #level-bar { height: 1; margin-top: 1; margin-bottom: 1; }
    #speakers-list { margin-top: 1; height: auto; }
    .speaker-row { height: 3; margin-bottom: 0; }
    .speaker-name { width: 1fr; height: 3; content-align: left middle; padding-left: 1; }
    .delete-btn { width: 10; height: 3; }
    #enroll-btn { width: 100%; margin-top: 1; }
    #tts-row { height: 3; margin-top: 1; }
    #tts-switch { margin-right: 2; }
    RadioSet { margin-top: 1; margin-bottom: 0; }
    #btn-row { margin-top: 2; height: 3; }
    #save-btn   { width: 1fr; margin-right: 1; }
    #cancel-btn { width: 1fr; }
    """

    def __init__(
        self,
        api_key: str,
        audio_format: AudioFormat,
        rt_url: str | None,
        tts_enabled: bool,
        tts_provider: str,
        device_index: int | None = None,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._audio_format = audio_format
        self._rt_url = rt_url
        self._tts_enabled = tts_enabled
        self._tts_provider = tts_provider
        self._device_index = device_index
        self._speakers: dict[str, list[str]] = _load_speakers()
        self._level_task: asyncio.Task[None] | None = None
        self._level_stream: Any = None
        self._level_pa: Any = None

    def compose(self) -> ComposeResult:
        devices = list_input_devices()
        options: list[tuple[str, int]] = [("System default", _DEVICE_DEFAULT)]
        options += [(name, idx) for idx, name in devices]
        current = _DEVICE_DEFAULT if self._device_index is None else self._device_index

        with Vertical(id="settings-container"):
            yield Label("CRAB-BOT Settings", id="settings-title")

            yield Label("Transcription Endpoint", classes="section-label")
            yield Label("Leave empty to use the Speechmatics cloud default", classes="hint")
            yield Input(
                value=self._rt_url or "",
                placeholder="wss://eu2.rt.speechmatics.com/v2",
                id="endpoint-input",
            )

            yield Label("Audio Input Device", classes="section-label")
            yield Select(options=options, value=current, id="device-select")
            yield Static(_render_level(0.0), id="level-bar")

            yield Label("Enrolled Speakers", classes="section-label")
            yield Vertical(id="speakers-list")
            yield Button("Register New Speaker", id="enroll-btn", variant="primary")

            yield Label("Text-to-Speech", classes="section-label")
            with Horizontal(id="tts-row"):
                yield Switch(value=self._tts_enabled, id="tts-switch")
                yield Label(" Enabled", id="tts-switch-label")
            with RadioSet(id="tts-provider"):
                yield RadioButton(
                    "macOS built-in (say)",
                    value=(self._tts_provider == _TTS_PROVIDER_MACOS),
                    id="rb-macos",
                )
                yield RadioButton(
                    "Python offline — planned",
                    value=(self._tts_provider == _TTS_PROVIDER_PYTHON),
                    disabled=True,
                    id="rb-python",
                )

            with Horizontal(id="btn-row"):
                yield Button("Save", id="save-btn", variant="success")
                yield Button("Cancel", id="cancel-btn")

    def on_mount(self) -> None:
        self._rebuild_speakers()
        self._restart_level_task()

    def on_unmount(self) -> None:
        self._cancel_level_task()

    def _cancel_level_task(self) -> None:
        if self._level_task and not self._level_task.done():
            self._level_task.cancel()
        self._level_task = None
        # Close synchronously so the device is free before SM restarts.
        if self._level_stream is not None:
            try:
                self._level_stream.stop_stream()
                self._level_stream.close()
            except Exception:
                pass
            self._level_stream = None
        if self._level_pa is not None:
            try:
                self._level_pa.terminate()
            except Exception:
                pass
            self._level_pa = None

    def _restart_level_task(self) -> None:
        self._cancel_level_task()
        raw = self.query_one("#device-select", Select).value
        device_index = None if raw == _DEVICE_DEFAULT or raw == Select.BLANK else int(raw)
        self._level_task = asyncio.create_task(self._level_loop(device_index))

    async def _level_loop(self, device_index: int | None) -> None:
        try:
            import pyaudio
        except ImportError:
            return

        pa = pyaudio.PyAudio()
        current_level: list[float] = [0.0]

        def _callback(
            in_data: bytes, frame_count: int, time_info: Any, status: int
        ) -> tuple[None, int]:
            shorts = struct.unpack(f"{len(in_data) // 2}h", in_data)
            rms = math.sqrt(sum(s * s for s in shorts) / len(shorts)) if shorts else 0.0
            current_level[0] = min(1.0, rms / 4000.0)
            return (None, pyaudio.paContinue)

        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=_LEVEL_SAMPLE_RATE,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=_LEVEL_CHUNK,
                stream_callback=_callback,
            )
            stream.start_stream()
            self._level_stream = stream
            self._level_pa = pa
        except Exception as exc:
            try:
                self.query_one("#level-bar", Static).update(f"[red]Can't open device: {exc}[/red]")
            except Exception:
                pass
            pa.terminate()
            return

        try:
            while True:
                await asyncio.sleep(0.1)
                try:
                    self.query_one("#level-bar", Static).update(
                        _render_level(current_level[0])
                    )
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            # _cancel_level_task may have already closed these synchronously; guard
            # against double-close.
            if self._level_stream is not None:
                try:
                    self._level_stream.stop_stream()
                    self._level_stream.close()
                except Exception:
                    pass
                self._level_stream = None
            if self._level_pa is not None:
                try:
                    self._level_pa.terminate()
                except Exception:
                    pass
                self._level_pa = None

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "device-select":
            self._restart_level_task()

    def _rebuild_speakers(self) -> None:
        container = self.query_one("#speakers-list", Vertical)
        for child in list(container.children):
            child.remove()
        if not self._speakers:
            container.mount(Label("  No speakers enrolled", classes="hint"))
            return
        for i, (name, ids) in enumerate(self._speakers.items()):
            id_count = len(ids)
            row = Horizontal(classes="speaker-row")
            container.mount(row)
            row.mount(Label(
                f"  {name}  [dim]({id_count} identifier{'s' if id_count != 1 else ''})[/dim]",
                classes="speaker-name",
            ))
            row.mount(Button("Remove", id=f"del-spk-{i}", classes="delete-btn", variant="error"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id.startswith("del-spk-"):
            idx = int(btn_id.split("-")[-1])
            name = list(self._speakers.keys())[idx]
            del self._speakers[name]
            _save_speakers(self._speakers)
            self._rebuild_speakers()
        elif btn_id == "enroll-btn":
            rt_url = self.query_one("#endpoint-input", Input).value.strip() or None
            raw = self.query_one("#device-select", Select).value
            device_index = None if raw == _DEVICE_DEFAULT or raw == Select.BLANK else int(raw)
            self.app.push_screen(
                EnrollModal(self._api_key, self._audio_format, rt_url, device_index),
                self._on_enrolled,
            )
        elif btn_id == "save-btn":
            self._save_and_close()
        elif btn_id == "cancel-btn":
            self.dismiss(None)

    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            self._save_and_close()

    def _on_enrolled(self, result: dict[str, list[str]] | None) -> None:
        if result:
            self._speakers.update(result)
            self._rebuild_speakers()
            name = next(iter(result))
            self.app.notify(f"'{name}' enrolled", severity="information")

    def _save_and_close(self) -> None:
        self._cancel_level_task()  # free the audio device before SM restarts
        self._rt_url = self.query_one("#endpoint-input", Input).value.strip() or None
        self._tts_enabled = self.query_one("#tts-switch", Switch).value
        pressed = self.query_one("#tts-provider", RadioSet).pressed_button
        if pressed is not None:
            self._tts_provider = (
                _TTS_PROVIDER_PYTHON if pressed.id == "rb-python" else _TTS_PROVIDER_MACOS
            )

        raw = self.query_one("#device-select", Select).value
        device_index = None if raw == _DEVICE_DEFAULT or raw == Select.BLANK else int(raw)

        enrolled_labels = set(self._speakers.keys())
        # Late import to avoid circular dependency: crab.ui.app imports this module.
        from crab.ui.app import CrabApp
        self.app.post_message(
            CrabApp.SettingsChanged(
                rt_url=self._rt_url,
                tts_enabled=self._tts_enabled,
                tts_provider=self._tts_provider,
                enrolled_labels=enrolled_labels,
                device_index=device_index,
            )
        )
        self.dismiss(None)
