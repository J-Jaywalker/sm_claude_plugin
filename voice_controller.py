"""Voice controller: wake word + accumulate + end-of-utterance + Claude integration.

Streams microphone audio to Speechmatics RT ASR. On first run, enrolls the
speaker by capturing 30 seconds of audio and saving identifiers to speakers.txt.
On subsequent runs, loads enrolled speakers and ignores transcripts from
unrecognised voices. Detects wake phrase "CRAB-BOT" in finals, accumulates
until EndOfUtterance, then submits the prompt to Claude Code via `claude -p`.

Usage:
    SPEECHMATICS_API_KEY=... python voice_controller.py
    DEBUG=1 SPEECHMATICS_API_KEY=... python voice_controller.py
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from collections import deque
from enum import Enum
from typing import Any, Protocol

from rich import box as rich_box
from rich.align import Align
from rich.console import Console as _RichConsole
from rich.console import Group
from rich.constrain import Constrain
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.panel import Panel
from rich.text import Text

from textual.app import App, ComposeResult
from textual.message import Message
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Static, Switch
from textual import work

from speechmatics.rt import (
    AsyncClient,
    AudioEncoding,
    AudioFormat,
    AuthenticationError,
    ClientMessageType,
    ConversationConfig,
    Microphone,
    OperatingPoint,
    ServerMessageType,
    SpeakerDiarizationConfig,
    SpeakerIdentifier,
    TranscriptionConfig,
)


_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_TTS_TAG_RE = re.compile(r"<tts>(.*?)</tts>", re.DOTALL | re.IGNORECASE)
_WAKE_WORD_PATTERN = re.compile(r"\bcrab[\s\-]+bot\b")
_DEBUG = bool(os.environ.get("DEBUG"))
_IDLE_BUFFER_MAX = 120

_SPEAKERS_FILE = os.path.join(os.path.dirname(__file__), "speakers.txt")
_ENROLLMENT_SECONDS = 30

_RT_URL = "ws://127.0.0.1:9002/v2" if os.environ.get("SM_LOCAL_CLAUDE_TRANSCRIPTION") else None

_SYSTEM_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "assets", "system_prompt.md")

def _load_system_prompt() -> str:
    try:
        with open(_SYSTEM_PROMPT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""

_DOT_INTERVAL = 0.2

_CRAB_ART_FILE = os.path.join(os.path.dirname(__file__), "assets", "crab_art.txt")


def _load_crab_art() -> dict[str, Any]:
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    try:
        with open(_CRAB_ART_FILE, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.startswith("[") and line.endswith("]"):
                    if current is not None:
                        sections[current] = "\n".join(lines).strip("\n")
                    current = line[1:-1]
                    lines = []
                elif current is not None:
                    lines.append(line)
        if current is not None:
            sections[current] = "\n".join(lines).strip("\n")
    except FileNotFoundError:
        pass
    return {
        "idle":      sections.get("idle",       "彡(-.-)ミ\n  ^   ^"),
        "listening": sections.get("listening",  "彡(ᵔᵕᵔ)ミ\n  ^   ^"),
        "thinking":  [
            sections.get("thinking_0", "彡('o')ミ"),
            sections.get("thinking_1", "彡('o')ミ"),
        ],
    }


_CRAB_ART = _load_crab_art()


_LOGGER = logging.getLogger(__name__)


def _extract_tts(text: str) -> tuple[str, str]:
    """Return ``(display_text, tts_text)`` parsed from assistant output.

    Strips any ``<tts>...</tts>`` block from ``text`` to form the
    display text, and returns the inner contents as the spoken TTS
    text. If no ``<tts>`` block is present, ``tts_text`` is empty and
    ``display_text`` equals the input.

    Args:
        text: Raw assistant output, possibly containing a ``<tts>``
            block at the end.

    Returns:
        A tuple ``(display_text, tts_text)``. ``display_text`` has the
        ``<tts>`` block (and surrounding trailing whitespace) removed.
    """
    match = _TTS_TAG_RE.search(text)
    if not match:
        return text, ""
    tts_text = match.group(1).strip()
    display_text = _TTS_TAG_RE.sub("", text).rstrip()
    return display_text, tts_text


# ---------------------------------------------------------------------------
# Chat bubble renderable (60% max width, computed at render time)
# ---------------------------------------------------------------------------

def _contains_markdown(renderable: Any) -> bool:
    """Return True if the renderable is or contains a Markdown instance.

    Markdown has no __rich_measure__, so Measurement.get falls back to
    maximum == options.max_width — useless for sizing. We detect this and
    fall back to render-based measurement.
    """
    if isinstance(renderable, Markdown):
        return True
    if isinstance(renderable, Group):
        return any(_contains_markdown(child) for child in renderable.renderables)
    return False


def _measure_by_render(max_width: int, renderable: Any) -> int:
    """Return the longest rendered line width by rendering off-screen."""
    probe = _RichConsole(
        width=max_width,
        file=io.StringIO(),
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
        record=False,
    )
    lines = probe.render_lines(renderable, probe.options.update_width(max_width), pad=False)
    return max((sum(seg.cell_length for seg in line) for line in lines), default=0)


class _Bubble:
    """Panel that grows to fit its content, capped at 60% of console width.

    Uses render-based measurement for Markdown content (which lacks
    __rich_measure__), and Measurement.get for plain Text and similar.
    """

    def __init__(
        self,
        content: Any,
        *,
        align: str = "left",
        **panel_kw: Any,
    ) -> None:
        self._content = content
        self._align = align
        self._panel_kw = panel_kw

    def _measure_title(self, console: Any, options: Any) -> int:
        title = self._panel_kw.get("title")
        if not title:
            return 0
        title_text = console.render_str(title, markup=True) if isinstance(title, str) else title
        return Measurement.get(console, options, title_text).maximum

    def __rich_console__(self, console: Any, options: Any) -> Any:
        cap = max(20, int(options.max_width * 0.6))
        probe_options = options.update_width(cap)

        if _contains_markdown(self._content):
            natural = _measure_by_render(cap, self._content)
        else:
            natural = Measurement.get(console, probe_options, self._content).maximum

        title_width = self._measure_title(console, probe_options)
        content_width = max(natural, title_width)
        width = max(20, min(content_width + 4, cap))  # +4: 2 border + 2 padding

        panel = Panel(self._content, expand=False, **self._panel_kw)
        constrained = Constrain(panel, width=width)
        if self._align == "right":
            yield from Align.right(constrained).__rich_console__(console, options)
        else:
            yield from constrained.__rich_console__(console, options)


# ---------------------------------------------------------------------------
# UI Protocol
# ---------------------------------------------------------------------------

class _UI(Protocol):
    def set_status(self, status: str) -> None: ...
    def set_partial(self, text: str) -> None: ...
    def add_user_message(self, text: str) -> None: ...
    def add_assistant_text(self, text: str) -> None: ...
    def add_tool_use(self, label: str) -> None: ...
    def add_error_message(self, text: str) -> None: ...


# ---------------------------------------------------------------------------
# Settings panel widget
# ---------------------------------------------------------------------------

class SettingsPanel(Static):
    """Square panel in the top-right corner. Green normally, orange on hover."""

    can_focus = False

    class OpenSettings(Message):
        pass

    def on_mount(self) -> None:
        self.update("SETTINGS")

    def on_click(self) -> None:
        self.post_message(self.OpenSettings())


# ---------------------------------------------------------------------------
# Enrollment modal
# ---------------------------------------------------------------------------

async def _enroll_speaker_tui(
    api_key: str,
    audio_format: AudioFormat,
    name: str,
    on_status: Any,
    rt_url: str | None = None,
) -> dict[str, list[str]]:
    """Run a 30-second enrollment session, updating *on_status* with progress.

    *on_status* is a callable(str) that accepts a status string (plain text).
    Returns the enrolled speakers dict and saves it to disk.
    """
    enrollment_config = TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        diarization="speaker",
        max_delay=1.0,
        additional_vocab=[
            {"content": "CRAB-BOT", "sounds_like": ["crab bot", "grab bot", "crab bought"]},
        ],
    )

    loop = asyncio.get_event_loop()
    speakers_future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()

    mic = Microphone(sample_rate=audio_format.sample_rate, chunk_size=audio_format.chunk_size)
    if not mic.start():
        raise RuntimeError("Microphone not available")

    try:
        async with AsyncClient(api_key=api_key, **({"url": rt_url} if rt_url else {})) as client:

            @client.on(ServerMessageType.SPEAKERS_RESULT)
            def _on_speakers(msg: dict[str, Any]) -> None:
                if not speakers_future.done():
                    speakers_future.set_result(msg.get("speakers", []))

            await client.start_session(
                transcription_config=enrollment_config,
                audio_format=audio_format,
            )

            stop_pump = asyncio.Event()
            pump_task = asyncio.create_task(
                _audio_pump_raw(client, mic, audio_format.chunk_size, stop_pump)
            )

            for remaining in range(_ENROLLMENT_SECONDS, 0, -1):
                on_status(f"Recording as '{name}'... {remaining}s remaining — speak naturally")
                await asyncio.sleep(1)

            stop_pump.set()
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass

            on_status("Processing speaker identifiers...")
            await client.send_message({"message": ClientMessageType.GET_SPEAKERS})

            try:
                raw_speakers = await asyncio.wait_for(speakers_future, timeout=10.0)
            except asyncio.TimeoutError:
                raw_speakers = []
    finally:
        mic.stop()

    if not raw_speakers:
        raise RuntimeError("Enrollment failed: no speaker identifiers detected")

    best = max(raw_speakers, key=lambda s: len(s.get("speaker_identifiers", [])))
    enrolled = {name: best["speaker_identifiers"]}
    _save_speakers(enrolled)
    return enrolled


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

    def __init__(self, api_key: str, audio_format: AudioFormat, rt_url: str | None) -> None:
        super().__init__()
        self._api_key = api_key
        self._audio_format = audio_format
        self._rt_url = rt_url
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
            )
            status.update(f"[green]'{name}' enrolled successfully![/green]")
            await asyncio.sleep(1.5)
            self.dismiss(enrolled)
        except Exception as exc:
            status.update(f"[red]Error: {exc}[/red]")
            self._recording = False
            self.query_one("#enroll-start", Button).disabled = False


_TTS_PROVIDER_MACOS = "macos"
_TTS_PROVIDER_PYTHON = "python"


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
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._audio_format = audio_format
        self._rt_url = rt_url
        self._tts_enabled = tts_enabled
        self._tts_provider = tts_provider
        self._speakers: dict[str, list[str]] = _load_speakers()

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-container"):
            yield Label("CRAB-BOT Settings", id="settings-title")

            yield Label("Transcription Endpoint", classes="section-label")
            yield Label("Leave empty to use the Speechmatics cloud default", classes="hint")
            yield Input(
                value=self._rt_url or "",
                placeholder="wss://eu2.rt.speechmatics.com/v2",
                id="endpoint-input",
            )

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
            self.app.push_screen(
                EnrollModal(self._api_key, self._audio_format, rt_url),
                self._on_enrolled,
            )
        elif btn_id == "save-btn":
            self._save_and_close()
        elif btn_id == "cancel-btn":
            self.dismiss(None)

    def on_key(self, event: Any) -> None:
        if event.key == "escape":
            self.dismiss(None)

    def _on_enrolled(self, result: dict[str, list[str]] | None) -> None:
        if result:
            self._speakers.update(result)
            self._rebuild_speakers()
            name = next(iter(result))
            self.app.notify(f"'{name}' enrolled", severity="information")

    def _save_and_close(self) -> None:
        self._rt_url = self.query_one("#endpoint-input", Input).value.strip() or None
        self._tts_enabled = self.query_one("#tts-switch", Switch).value
        pressed = self.query_one("#tts-provider", RadioSet).pressed_button
        if pressed is not None:
            self._tts_provider = (
                _TTS_PROVIDER_PYTHON if pressed.id == "rb-python" else _TTS_PROVIDER_MACOS
            )
        enrolled_labels = {label for ids in self._speakers.values() for label in ids}
        self.app.post_message(
            CrabApp.SettingsChanged(
                rt_url=self._rt_url,
                tts_enabled=self._tts_enabled,
                tts_provider=self._tts_provider,
                enrolled_labels=enrolled_labels,
            )
        )
        self.dismiss(None)


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

class CrabApp(App[None]):
    """Full-screen Textual TUI: visualiser, scrollable conversation, prompt strip."""

    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [("ctrl+c", "quit", "Quit"), ("ctrl+q", "quit", "Quit")]

    class SettingsChanged(Message):
        """Posted by SettingsModal when the user saves new settings."""
        def __init__(
            self,
            rt_url: str | None,
            tts_enabled: bool,
            tts_provider: str,
            enrolled_labels: set[str],
        ) -> None:
            super().__init__()
            self.rt_url = rt_url
            self.tts_enabled = tts_enabled
            self.tts_provider = tts_provider
            self.enrolled_labels = enrolled_labels

    CSS = """
    Horizontal#top {
        height: 9;
    }
    Static#visualiser {
        width: 20;
        height: 100%;
    }
    Static#instructions {
        width: 1fr;
        height: 100%;
    }
    SettingsPanel {
        width: 22;
        height: 100%;
        background: #29A383;
        color: white;
        border: round #29A383;
        content-align: center middle;
    }
    SettingsPanel:hover {
        background: #F76B15;
        color: black;
        border: round #F76B15;
    }
    VerticalScroll#conversation {
        height: 1fr;
    }
    Static#history {
        height: auto;
    }
    Input#cmd-input {
        height: 3;
        border: round cyan;
    }
    Input#cmd-input:focus {
        border: round cyan;
    }
    """

    def __init__(
        self,
        api_key: str,
        audio_format: AudioFormat,
        transcription_config: TranscriptionConfig,
        speaker_name: str,
        enrolled_labels: set[str],
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._audio_format = audio_format
        self._transcription_config = transcription_config
        self._speaker_name = speaker_name
        self._enrolled_labels = enrolled_labels

        self._status = "idle"
        self._partial = ""
        self._last_prompt = ""
        self._history: deque[dict[str, Any]] = deque(maxlen=30)
        self._frame = 0
        self._stop_event: asyncio.Event | None = None
        self._sm_task: asyncio.Task[None] | None = None
        self._tts_proc: asyncio.subprocess.Process | None = None
        self._controller: VoiceController | None = None
        self._exiting = False
        self._restarting_sm = False
        self._rt_url: str | None = _RT_URL
        self._tts_enabled: bool = True
        self._tts_provider: str = _TTS_PROVIDER_MACOS

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(id="visualiser")
            yield Static(id="instructions")
            yield SettingsPanel()
        with VerticalScroll(id="conversation"):
            yield Static(id="history")
        yield Input(placeholder="Type a command or speak...", id="cmd-input")

    def on_mount(self) -> None:
        self._stop_event = asyncio.Event()
        self._render_visualiser()
        self._render_instructions()
        self._render_history()
        self.set_interval(_DOT_INTERVAL, self._tick)
        self._sm_task = asyncio.create_task(self._run_speechmatics(), name="speechmatics")

    def on_unmount(self) -> None:
        # Signal the speechmatics task to stop. Do NOT await here — Textual's
        # shutdown is mid-flight and awaiting causes a race with the event loop
        # teardown. main() waits for the task after run_async() returns instead.
        self._exiting = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._tts_proc is not None:
            try:
                self._tts_proc.kill()
            except Exception:
                pass

    # -- Animation tick -----------------------------------------------------

    def _tick(self) -> None:
        self._frame += 1
        self._render_visualiser()

    # -- Renderers ----------------------------------------------------------

    def _render_visualiser(self) -> None:
        if self._status == "idle":
            art = _CRAB_ART["idle"]
            label = "Idle"
            color = "bright_red"
        elif self._status == "listening":
            art = _CRAB_ART["listening"]
            label = "Listening..."
            color = "bright_green"
        else:
            art = _CRAB_ART["thinking"][(self._frame // 5) % 2]
            label = "Thinking..."
            color = "bright_yellow"
        t = Text(art, style=color, justify="center")
        panel = Panel(
            Align.center(t, vertical="middle"),
            title="[bold]CRAB VISUALISER[/bold]",
            subtitle=f"[{color}]{label}[/{color}]",
            border_style=color,
        )
        self.query_one("#visualiser", Static).update(panel)

    def _render_instructions(self) -> None:
        t = Text(justify="left")
        t.append("How to use\n\n", style="bold")
        t.append("1. ", style="dim"); t.append('Say "CRAB-BOT"\n')
        t.append("2. ", style="dim"); t.append("Speak your command naturally\n")
        t.append("3. ", style="dim"); t.append("Pause — end of speech is detected automatically\n")
        t.append("4. ", style="dim"); t.append("Wait for Claude to respond\n")
        t.append("5. ", style="dim"); t.append('Say "CRAB-BOT" again for your next command')
        panel = Panel(
            Align.center(t, vertical="middle"),
            title="[bold]C.R.A.B — Claude Realtime Audio Bot[/bold]",
            border_style="dim",
        )
        self.query_one("#instructions", Static).update(panel)

    def _render_history(self) -> None:
        if not self._history:
            content: Any = Align.center(Text("No conversation yet.", style="dim"))
        else:
            items: list[Any] = []
            for msg in self._history:
                role = msg["role"]
                if role == "user":
                    items.append(_Bubble(
                        Text(msg["text"]),
                        align="left",
                        title=f"[bold cyan]{self._speaker_name}[/bold cyan]",
                        box=rich_box.ROUNDED,
                        border_style="cyan",
                    ))
                elif role == "assistant":
                    parts: list[Any] = []
                    text_chunks: list[str] = []
                    for kind, seg in msg["segments"]:
                        if kind == "text":
                            text_chunks.append(seg)
                        else:
                            if text_chunks:
                                combined = "".join(text_chunks)
                                display_text, _ = _extract_tts(combined)
                                if display_text:
                                    parts.append(Markdown(display_text))
                                text_chunks = []
                            parts.append(Text(seg, style="dim"))
                    if text_chunks:
                        combined = "".join(text_chunks)
                        display_text, _ = _extract_tts(combined)
                        if display_text:
                            parts.append(Markdown(display_text))
                    items.append(_Bubble(
                        Group(*parts) if parts else Text(""),
                        align="right",
                        title="[dim]CRAB[/dim]",
                        box=rich_box.ROUNDED,
                        border_style="dim",
                    ))
                elif role == "done":
                    items.append(Align.right(Text(msg["text"], style="dim")))
                elif role == "error":
                    items.append(Text(msg["text"], style="bright_red"))
                items.append(Text(""))
            content = Group(*items)
        self.query_one("#history", Static).update(content)
        self.query_one("#conversation", VerticalScroll).scroll_end(animate=False)

    # -- Input / settings event handlers ------------------------------------

    def on_settings_panel_open_settings(self, event: SettingsPanel.OpenSettings) -> None:
        self._settings_flow()

    @work(exit_on_error=False)
    async def _settings_flow(self) -> None:
        """Pause ASR, show the settings modal, then resume ASR.

        Setting ``self._restarting_sm`` tells ``_run_speechmatics`` that the
        stop is intentional and it must NOT call ``self.exit()`` when it
        returns — otherwise clicking Settings would shut the app down.
        """
        self._restarting_sm = True
        try:
            if self._stop_event is not None:
                self._stop_event.set()
            if self._sm_task is not None and not self._sm_task.done():
                try:
                    await asyncio.wait_for(self._sm_task, timeout=5.0)
                except asyncio.TimeoutError:
                    self._sm_task.cancel()
                    try:
                        await self._sm_task
                    except (asyncio.CancelledError, Exception):
                        pass
                except (asyncio.CancelledError, Exception):
                    pass
            self._controller = None
            self.set_status("idle")

            try:
                await self.push_screen_wait(
                    SettingsModal(
                        api_key=self._api_key,
                        audio_format=self._audio_format,
                        rt_url=self._rt_url,
                        tts_enabled=self._tts_enabled,
                        tts_provider=self._tts_provider,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.add_error_message(f"[ERROR] Settings: {exc}")
        finally:
            self._restarting_sm = False

        # Restart the main ASR session regardless of whether settings were saved.
        if not self._exiting:
            self._stop_event = asyncio.Event()
            self._sm_task = asyncio.create_task(
                self._run_speechmatics(), name="speechmatics"
            )

    def on_crab_app_settings_changed(self, event: "CrabApp.SettingsChanged") -> None:
        self._rt_url = event.rt_url
        self._tts_enabled = event.tts_enabled
        self._tts_provider = event.tts_provider
        self._enrolled_labels = event.enrolled_labels
        if self._controller is not None:
            self._controller.enrolled_labels = event.enrolled_labels

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self._controller is None:
            return
        self._controller.last_prompt = text
        self._controller.state = _State.IDLE
        self.add_user_message(text)
        self.set_status("thinking")
        self._controller.prompt_ready.set()
        event.input.clear()

    # -- Public API (same interface as old UI class) ------------------------

    def set_status(self, status: str) -> None:
        self._status = status
        if status != "listening":
            self._partial = ""
            self.query_one("#cmd-input", Input).placeholder = "Type a command or speak..."
        self._render_visualiser()

    def set_partial(self, text: str) -> None:
        self._partial = text
        inp = self.query_one("#cmd-input", Input)
        if not inp.value:
            inp.placeholder = text if text else "Type a command or speak..."

    def add_user_message(self, text: str) -> None:
        self._last_prompt = text
        self._partial = ""
        self._history.append({"role": "user", "text": text})
        self._render_history()
        inp = self.query_one("#cmd-input", Input)
        inp.placeholder = "Type a command or speak..."

    def add_assistant_text(self, text: str) -> None:
        clean = _ANSI_ESCAPE.sub("", text).strip()
        if not clean:
            return
        if self._history and self._history[-1]["role"] == "assistant":
            self._history[-1]["segments"].append(("text", clean))
        else:
            self._history.append({"role": "assistant", "segments": [("text", clean)]})
        self._render_history()

    def add_tool_use(self, label: str) -> None:
        if label.startswith("[DONE]"):
            self._finalise_assistant_tts()
            self._history.append({"role": "done", "text": label})
        elif self._history and self._history[-1]["role"] == "assistant":
            self._history[-1]["segments"].append(("tool", label))
        else:
            self._history.append({"role": "assistant", "segments": [("tool", label)]})
        self._render_history()

    def _finalise_assistant_tts(self) -> None:
        """Extract the ``<tts>`` block from the most recent assistant turn and speak it."""
        for msg in reversed(self._history):
            if msg["role"] != "assistant":
                continue
            if "tts" in msg:
                return
            text_chunks = [
                seg for kind, seg in msg["segments"] if kind == "text"
            ]
            if not text_chunks:
                msg["tts"] = ""
                return
            combined = "".join(text_chunks)
            _, tts_text = _extract_tts(combined)
            msg["tts"] = tts_text
            if tts_text:
                _LOGGER.info("TTS: %s", tts_text)
                asyncio.create_task(self._speak(tts_text), name="tts")
            return

    async def _speak(self, text: str) -> None:
        """Speak *text* using the configured TTS provider, if TTS is enabled."""
        if not self._tts_enabled:
            return

        if self._tts_proc is not None:
            try:
                self._tts_proc.kill()
                await self._tts_proc.wait()
            except Exception:
                pass
            self._tts_proc = None

        if self._tts_provider == _TTS_PROVIDER_MACOS:
            try:
                self._tts_proc = await asyncio.create_subprocess_exec(
                    "say", text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await self._tts_proc.wait()
            except Exception as exc:
                _LOGGER.warning("TTS failed: %s", exc)
            finally:
                self._tts_proc = None

    def add_error_message(self, text: str) -> None:
        self._history.append({"role": "error", "text": text})
        self._render_history()

    # -- Speechmatics integration -------------------------------------------

    async def _run_speechmatics(self) -> None:
        assert self._stop_event is not None
        stop_event = self._stop_event
        controller = VoiceController(enrolled_labels=self._enrolled_labels, ui=self)
        self._controller = controller

        mic = Microphone(
            sample_rate=self._audio_format.sample_rate,
            chunk_size=self._audio_format.chunk_size,
        )
        if not mic.start():
            self.add_error_message("[ERROR] Microphone not available — install with `pip install pyaudio`.")
            return

        try:
            async with AsyncClient(api_key=self._api_key, **({'url': self._rt_url} if self._rt_url else {})) as client:

                @client.on(ServerMessageType.RECOGNITION_STARTED)
                def _on_started(message: dict[str, Any]) -> None:
                    if _DEBUG:
                        self.add_tool_use("[DBG MSG] RECOGNITION_STARTED")
                    self.set_status("idle")

                @client.on(ServerMessageType.ADD_TRANSCRIPT)
                def _on_final(message: dict[str, Any]) -> None:
                    controller.handle_final(message)

                @client.on(ServerMessageType.END_OF_UTTERANCE)
                def _on_eou(message: dict[str, Any]) -> None:
                    controller.handle_end_of_utterance(message)

                @client.on(ServerMessageType.ERROR)
                def _on_server_error(message: dict[str, Any]) -> None:
                    if _DEBUG:
                        self.add_tool_use("[DBG MSG] ERROR")
                    reason = message.get("reason", "unknown")
                    self.add_error_message(f"[ERROR] {reason}")
                    stop_event.set()

                await client.start_session(
                    transcription_config=self._transcription_config,
                    audio_format=self._audio_format,
                )

                if _DEBUG:
                    self.add_tool_use("[READY] Debug mode ON.")

                pump_task = asyncio.create_task(
                    _audio_pump(
                        client=client,
                        mic=mic,
                        controller=controller,
                        chunk_size=self._audio_format.chunk_size,
                        stop_event=stop_event,
                    ),
                    name="audio-pump",
                )
                driver_task = asyncio.create_task(
                    claude_driver(
                        controller=controller,
                        ui=self,
                        stop_event=stop_event,
                    ),
                    name="claude-driver",
                )
                stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

                try:
                    await asyncio.wait(
                        {pump_task, driver_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    stop_event.set()
                    pump_task.cancel()
                    driver_task.cancel()
                    stop_task.cancel()
                    for task in (pump_task, driver_task, stop_task):
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    try:
                        await asyncio.wait_for(client.stop_session(), timeout=3.0)
                    except Exception:
                        pass

        except AuthenticationError as exc:
            self.add_error_message(f"[ERROR] Authentication failed: {exc}")
            await asyncio.sleep(2)
        finally:
            mic.stop()

        if not self._exiting and not self._restarting_sm:
            self.exit()


# ---------------------------------------------------------------------------
# Speaker store
# ---------------------------------------------------------------------------

def _load_speakers() -> dict[str, list[str]]:
    """Load enrolled speakers from file. Returns {name: [identifier, ...]}."""
    speakers: dict[str, list[str]] = {}
    try:
        with open(_SPEAKERS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or ":" not in line:
                    continue
                name, ids_str = line.split(":", 1)
                ids = [i for i in ids_str.split(",") if i]
                if name and ids:
                    speakers[name] = ids
    except FileNotFoundError:
        pass
    return speakers


def _save_speakers(speakers: dict[str, list[str]]) -> None:
    with open(_SPEAKERS_FILE, "w") as f:
        for name, ids in speakers.items():
            f.write(f"{name}:{','.join(ids)}\n")


def _dominant_speaker(results: list[dict[str, Any]]) -> str | None:
    """Return the most-frequent speaker label across all word alternatives."""
    counts: dict[str, int] = {}
    for result in results:
        for alt in result.get("alternatives", []):
            spk = alt.get("speaker")
            if spk:
                counts[spk] = counts.get(spk, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


# ---------------------------------------------------------------------------
# Normalisation / wake word
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase and collapse punctuation/whitespace for wake word matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class _State(str, Enum):
    IDLE = "IDLE"
    ACCUMULATING = "ACCUMULATING"


class VoiceController:
    """Coordinates microphone capture, ASR events, and prompt assembly."""

    def __init__(self, enrolled_labels: set[str], ui: _UI) -> None:
        self._ui = ui
        self.enrolled_labels = enrolled_labels
        self.state: _State = _State.IDLE
        self.buffer: list[str] = []
        self._idle_buffer: str = ""
        self.listening: asyncio.Event = asyncio.Event()
        self.listening.set()
        self.prompt_ready: asyncio.Event = asyncio.Event()
        self.last_prompt: str = ""

    def handle_final(self, message: dict[str, Any]) -> None:
        """Handle an ADD_TRANSCRIPT (final) server message."""
        transcript = message.get("metadata", {}).get("transcript", "")
        if not transcript:
            return

        if self.enrolled_labels:
            speaker = _dominant_speaker(message.get("results", []))
            if speaker is not None and speaker not in self.enrolled_labels:
                if _DEBUG:
                    self._ui.add_tool_use(f"[DBG] ignored transcript from {speaker!r}")
                return

        if self.state is _State.IDLE:
            if _DEBUG:
                self._ui.add_tool_use(f"[DBG] {transcript!r}")
            self._idle_buffer = (self._idle_buffer + " " + transcript)[-_IDLE_BUFFER_MAX:]
            if _WAKE_WORD_PATTERN.search(_normalize(self._idle_buffer)):
                self._idle_buffer = ""
                self.state = _State.ACCUMULATING
                self._ui.set_status("listening")
            return

        # ACCUMULATING: strip wake word tail if present in the first final.
        match = _WAKE_WORD_PATTERN.search(_normalize(transcript))
        if match:
            transcript = transcript[match.end():]

        transcript = transcript.strip()
        if transcript and re.search(r"[a-zA-Z]", transcript):
            self.buffer.append(transcript)
            self._ui.set_partial(" ".join(self.buffer))

    def handle_end_of_utterance(self, message: dict[str, Any]) -> None:
        """Handle an END_OF_UTTERANCE server message."""
        del message
        if _DEBUG:
            self._ui.add_tool_use("[DBG MSG] END_OF_UTTERANCE")

        if self.state is _State.IDLE:
            self._idle_buffer = ""
            return

        prompt = " ".join(self.buffer).strip()
        self.buffer.clear()
        self._idle_buffer = ""

        if not prompt:
            # Wake word heard but no command yet — stay ACCUMULATING.
            return

        if not re.search(r"[a-zA-Z]{2,}", prompt):
            # Only punctuation/noise — reset silently so user can try again.
            self.state = _State.IDLE
            self._ui.set_status("idle")
            return

        self.state = _State.IDLE
        self.last_prompt = prompt
        self._ui.add_user_message(prompt)
        self._ui.set_status("thinking")
        self.prompt_ready.set()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _require_api_key() -> str:
    api_key = os.environ.get("SPEECHMATICS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "SPEECHMATICS_API_KEY is not set. Export it before running, "
            "e.g. `export SPEECHMATICS_API_KEY=...`."
        )
    return api_key


def _build_transcription_config(
    speakers: list[SpeakerIdentifier] | None = None,
) -> TranscriptionConfig:
    diarization_config = SpeakerDiarizationConfig(speakers=speakers) if speakers else None
    return TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        diarization="speaker" if speakers else None,
        max_delay=1.0,
        conversation_config=ConversationConfig(
            end_of_utterance_silence_trigger=1.5,
        ),
        speaker_diarization_config=diarization_config,
        additional_vocab=[
            {"content": "CRAB-BOT", "sounds_like": ["crab bot", "grab bot", "crab bought"]},
        ],
    )


# ---------------------------------------------------------------------------
# Audio pumps
# ---------------------------------------------------------------------------

async def _audio_pump(
    client: AsyncClient,
    mic: Microphone,
    controller: VoiceController,
    chunk_size: int,
    stop_event: asyncio.Event,
) -> None:
    """Forward mic frames to the server only while controller.listening is set."""
    while not stop_event.is_set():
        try:
            frame = await mic.read(chunk_size=chunk_size)
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            stop_event.set()
            return

        if controller.listening.is_set():
            try:
                await client.send_audio(frame)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                stop_event.set()
                return


async def _audio_pump_raw(
    client: AsyncClient,
    mic: Microphone,
    chunk_size: int,
    stop_event: asyncio.Event,
) -> None:
    """Unconditional audio pump used during enrollment."""
    while not stop_event.is_set():
        try:
            frame = await mic.read(chunk_size=chunk_size)
            await client.send_audio(frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return


# ---------------------------------------------------------------------------
# Speaker enrollment
# ---------------------------------------------------------------------------

async def _enroll_speaker(api_key: str, audio_format: AudioFormat) -> dict[str, list[str]]:
    """Capture 30 s of speech, request speaker identifiers, save to disk."""
    loop = asyncio.get_event_loop()
    name = await loop.run_in_executor(
        None, lambda: input("No enrolled speakers found. Enter your name: ").strip()
    )
    if not name:
        name = "owner"

    print(f"\n[ENROLL] Recording {_ENROLLMENT_SECONDS}s as '{name}'. Please speak naturally...\n")

    enrollment_config = TranscriptionConfig(
        language="en",
        operating_point=OperatingPoint.ENHANCED,
        diarization="speaker",
        max_delay=1.0,
        additional_vocab=[
            {"content": "CRAB-BOT", "sounds_like": ["crab bot", "grab bot", "crab bought"]},
        ],
    )

    speakers_future: asyncio.Future[list[dict[str, Any]]] = loop.create_future()

    mic = Microphone(
        sample_rate=audio_format.sample_rate,
        chunk_size=audio_format.chunk_size,
    )
    if not mic.start():
        raise RuntimeError("Microphone not available for enrollment")

    try:
        async with AsyncClient(api_key=api_key, **({'url': _RT_URL} if _RT_URL else {})) as client:

            @client.on(ServerMessageType.RECOGNITION_STARTED)
            def _on_started(msg: dict[str, Any]) -> None:
                print("[ENROLL] Connected — recording now...")

            @client.on(ServerMessageType.SPEAKERS_RESULT)
            def _on_speakers(msg: dict[str, Any]) -> None:
                if not speakers_future.done():
                    speakers_future.set_result(msg.get("speakers", []))

            await client.start_session(
                transcription_config=enrollment_config,
                audio_format=audio_format,
            )

            stop_pump = asyncio.Event()
            pump_task = asyncio.create_task(
                _audio_pump_raw(client, mic, audio_format.chunk_size, stop_pump)
            )

            await asyncio.sleep(_ENROLLMENT_SECONDS)
            stop_pump.set()
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass

            print("[ENROLL] Recording complete. Requesting speaker identifiers...")
            await client.send_message({"message": ClientMessageType.GET_SPEAKERS})

            try:
                raw_speakers = await asyncio.wait_for(speakers_future, timeout=10.0)
            except asyncio.TimeoutError:
                raw_speakers = []

    finally:
        mic.stop()

    if not raw_speakers:
        raise RuntimeError("Enrollment failed: no speakers detected in the recording")

    best = max(raw_speakers, key=lambda s: len(s.get("speaker_identifiers", [])))
    enrolled = {name: best["speaker_identifiers"]}
    _save_speakers(enrolled)
    print(f"[ENROLL] '{name}' enrolled ({len(best['speaker_identifiers'])} identifier(s) saved).\n")
    return enrolled


# ---------------------------------------------------------------------------
# Claude output rendering + driver
# ---------------------------------------------------------------------------

def _handle_stream_event(event: dict[str, Any], ui: _UI) -> None:
    """Dispatch a claude --output-format stream-json event to the UI."""
    event_type = event.get("type")

    if event_type == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text":
                ui.add_assistant_text(block["text"])
            elif block.get("type") == "tool_use":
                name = block.get("name", "")
                inp = block.get("input", {})
                if name in ("Edit", "Write"):
                    ui.add_tool_use(f"[EDIT] {os.path.relpath(inp.get('file_path', '?'))}")
                elif name == "Bash":
                    ui.add_tool_use(f"[BASH] {inp.get('command', '?')[:120]}")
                elif name:
                    ui.add_tool_use(f"[TOOL] {name}")

    elif event_type == "user":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                output = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
            else:
                output = str(content)
            output = output.strip()
            if output:
                truncated = output[:200] + ("…" if len(output) > 200 else "")
                ui.add_tool_use(f"  └─ {truncated}")

    elif event_type == "result":
        if event.get("subtype") == "error":
            ui.add_error_message(f"[CLAUDE ERROR] {event.get('error', '')}")
        elif event.get("subtype") == "success":
            usage = event.get("usage", {})
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            duration_s = (event.get("duration_ms") or 0) / 1000
            parts = [f"{in_tok:,} in", f"{out_tok:,} out"]
            if cache_read:
                parts.append(f"{cache_read:,} cached")
            parts.append(f"Completed in {duration_s:.1f}s")
            ui.add_tool_use("[DONE] " + " · ".join(parts))


async def claude_driver(
    controller: VoiceController,
    ui: _UI,
    stop_event: asyncio.Event,
) -> None:
    """Run `claude -p` for each assembled prompt, streaming output to the UI.

    Clears controller.listening while Claude runs so the audio pump drops
    frames, then restores it when done.
    """
    child_env = {k: v for k, v in os.environ.items() if k != "DEBUG"}
    cwd = os.getcwd()
    base_prompt = _load_system_prompt()
    system_prompt = f"{base_prompt}\n\nWorking directory: {cwd}".strip()
    first_prompt = True

    while not stop_event.is_set():
        await controller.prompt_ready.wait()
        controller.prompt_ready.clear()
        prompt_text = controller.last_prompt
        if not prompt_text:
            continue

        controller.listening.clear()

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--verbose",
            "--output-format", "stream-json",
        ]
        if system_prompt:
            cmd += ["--system-prompt", system_prompt]
        if not first_prompt:
            cmd.append("--continue")
        cmd += ["-p", prompt_text]
        first_prompt = False

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=child_env,
                cwd=os.getcwd(),
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    clean = _ANSI_ESCAPE.sub("", line)
                    if clean:
                        ui.add_tool_use(clean)
                    continue
                _handle_stream_event(event, ui)
            await proc.wait()
        except asyncio.CancelledError:
            if proc is not None:
                proc.terminate()
            raise
        except Exception as exc:
            ui.add_error_message(f"[CLAUDE] Error: {exc}")

        ui.set_status("idle")
        controller.listening.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    api_key = _require_api_key()

    audio_format = AudioFormat(
        encoding=AudioEncoding.PCM_S16LE,
        sample_rate=16000,
        chunk_size=4096,
    )

    speakers = _load_speakers()
    if not speakers:
        speakers = await _enroll_speaker(api_key, audio_format)

    speaker_identifiers = [
        SpeakerIdentifier(label=name, speaker_identifiers=ids)
        for name, ids in speakers.items()
    ]
    enrolled_labels = set(speakers.keys())
    transcription_config = _build_transcription_config(speakers=speaker_identifiers)
    speaker_name = next(iter(enrolled_labels), "You")

    app = CrabApp(
        api_key=api_key,
        audio_format=audio_format,
        transcription_config=transcription_config,
        speaker_name=speaker_name,
        enrolled_labels=enrolled_labels,
    )
    await app.run_async()

    # Textual has exited but the asyncio event loop (asyncio.run) is still live.
    # Wait here for the speechmatics task to finish its own cleanup (stop_session,
    # mic.stop) before we let asyncio.run() return and Python starts joining threads.
    sm_task = app._sm_task
    if sm_task and not sm_task.done():
        try:
            await asyncio.wait_for(sm_task, timeout=4.0)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            sm_task.cancel()
            try:
                await sm_task
            except (asyncio.CancelledError, Exception):
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    print("\n[BYE] Exiting.")
    # os._exit skips Python's atexit thread-join phase (concurrent.futures executor
    # threads from PyAudio / Textual internals) which hangs on a second Ctrl+C.
    os._exit(0)
