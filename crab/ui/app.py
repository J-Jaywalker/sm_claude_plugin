"""The Textual application class for the CRAB voice controller."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections import deque
from typing import Any

from rich import box as rich_box
from rich.align import Align
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Input, Static

from speechmatics.rt import (
    AsyncClient,
    AudioFormat,
    AuthenticationError,
    Microphone,
    ServerMessageType,
    TranscriptionConfig,
)

from crab.asr.controller import VoiceController, _State
from crab.asr.pumps import _audio_pump
from crab.channel.driver import channel_driver
from crab.config import (
    _ANSI_ESCAPE,
    _CRAB_ART,
    _DEBUG,
    _DOT_INTERVAL,
    _NARRATE_TAG_RE,
    _RT_URL,
    _THINKING_LABELS,
)
from crab.tts.base import _extract_tts
from crab.tts.macos import _TTS_PROVIDER_MACOS
from crab.ui.modals import SettingsModal
from crab.ui.rendering import _Bubble
from crab.ui.widgets import SettingsPanel


_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TUI
# ---------------------------------------------------------------------------

class CrabApp(App[None]):
    """Full-screen Textual TUI: visualiser, scrollable conversation, prompt strip."""

    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+t", "toggle_mouse", "Mouse on (for clicks)"),
        ("ctrl+s", "open_settings", "Settings"),
    ]

    class SettingsChanged(Message):
        """Posted by SettingsModal when the user saves new settings."""
        def __init__(
            self,
            rt_url: str | None,
            tts_enabled: bool,
            tts_provider: str,
            enrolled_labels: set[str],
            device_index: int | None = None,
            local_wake_word: bool = False,
            wake_word_model: str = "",
            wake_word_threshold: float = 0.5,
        ) -> None:
            super().__init__()
            self.rt_url = rt_url
            self.tts_enabled = tts_enabled
            self.tts_provider = tts_provider
            self.enrolled_labels = enrolled_labels
            self.device_index = device_index
            self.local_wake_word = local_wake_word
            self.wake_word_model = wake_word_model
            self.wake_word_threshold = wake_word_threshold

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
        self._device_index: int | None = None
        self._local_wake_word: bool = False
        self._wake_word_model: str = ""
        self._wake_word_threshold: float = 0.5
        self._narrate_scan_buf: str = ""
        # Default OFF so drag-to-select works like a normal terminal. The
        # user can press Ctrl+T to opt in when they want to click (e.g. the
        # settings panel), and Ctrl+S opens settings via keyboard regardless.
        self._mouse_capture_enabled: bool = False
        # Optional Claude-controlled status label (set via mcp__crab__set_status).
        # Empty string means "use the default cycling crab puns".
        self._custom_label: str = ""
        # TTS playback is serialized through this queue so concurrent narrate /
        # <tts> / menu-question requests never interrupt each other. Items are
        # (text, optional Future that resolves once that clip finishes).
        self._tts_queue: asyncio.Queue[
            tuple[str, asyncio.Future[None] | None] | None
        ] = asyncio.Queue()
        self._tts_worker_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static(id="visualiser")
            yield Static(id="instructions")
            yield SettingsPanel()
        with VerticalScroll(id="conversation"):
            yield Static(id="history")
        yield Input(placeholder="Type a command or speak...", id="cmd-input")
        # Footer auto-renders the BINDINGS list at the bottom so Ctrl+T
        # (toggle mouse capture for native text-select) is always visible.
        yield Footer()

    def on_mount(self) -> None:
        self._stop_event = asyncio.Event()
        self._render_visualiser()
        self._render_instructions()
        self._render_history()
        self.set_interval(_DOT_INTERVAL, self._tick)
        self._sm_task = asyncio.create_task(self._run_speechmatics(), name="speechmatics")
        self._tts_worker_task = asyncio.create_task(self._tts_worker(), name="tts-worker")
        # Mouse capture starts disabled — Textual's driver enables it during
        # startup, so we explicitly turn it back off now. Users opt in via
        # Ctrl+T when they want to click (e.g. settings panel).
        if not self._mouse_capture_enabled and self._driver is not None:
            try:
                self._driver._disable_mouse_support()
            except Exception:  # noqa: BLE001
                pass

    def on_unmount(self) -> None:
        # Signal the speechmatics task to stop. Do NOT await here — Textual's
        # shutdown is mid-flight and awaiting causes a race with the event loop
        # teardown. main() waits for the task after run_async() returns instead.
        self._exiting = True
        if self._stop_event is not None:
            self._stop_event.set()
        # Tell the TTS worker to exit cleanly. Kill any in-flight clip so the
        # event loop teardown doesn't block on a long `say` invocation.
        try:
            self._tts_queue.put_nowait(None)
        except Exception:
            pass
        if self._tts_worker_task is not None and not self._tts_worker_task.done():
            self._tts_worker_task.cancel()
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
            label = self._custom_label or _THINKING_LABELS[
                (self._frame // 25) % len(_THINKING_LABELS)
            ]
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
        t = Text(justify="center")
        t.append(_CRAB_ART["title"], style="bold #29A383")
        t.append("\n\n--{ via Speechmatics }--", style="dim")
        panel = Panel(
            Align.center(t, vertical="middle"),
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
                                combined = _NARRATE_TAG_RE.sub(
                                    "", "".join(text_chunks)
                                ).strip()
                                display_text, _ = _extract_tts(combined)
                                if display_text:
                                    parts.append(Markdown(display_text))
                                text_chunks = []
                            parts.append(Text(seg, style="dim"))
                    if text_chunks:
                        combined = _NARRATE_TAG_RE.sub(
                            "", "".join(text_chunks)
                        ).strip()
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
        if self._status == "thinking":
            self.notify("Please wait for CRAB-BOT to finish responding.", severity="warning")
            return

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
                except asyncio.CancelledError:
                    raise
                except Exception:
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
                        device_index=self._device_index,
                        local_wake_word=self._local_wake_word,
                        wake_word_model=self._wake_word_model,
                        wake_word_threshold=self._wake_word_threshold,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.add_error_message(f"[ERROR] Settings: {exc}")
        finally:
            self._restarting_sm = False

        # Yield to the event loop so any SettingsChanged message is processed
        # before the new ASR session reads self._device_index / self._rt_url.
        await asyncio.sleep(0)

        # Restart the main ASR session regardless of whether settings were saved.
        if not self._exiting:
            self._stop_event = asyncio.Event()
            self._sm_task = asyncio.create_task(
                self._run_speechmatics(), name="speechmatics"
            )

    async def await_asr_shutdown(self, timeout: float = 4.0) -> None:
        """Wait for the ASR task to finish its own cleanup after Textual has exited."""
        task = self._sm_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except Exception:
            pass

    def on_crab_app_settings_changed(self, event: "CrabApp.SettingsChanged") -> None:
        self._rt_url = event.rt_url
        self._tts_enabled = event.tts_enabled
        self._tts_provider = event.tts_provider
        self._enrolled_labels = event.enrolled_labels
        self._device_index = event.device_index
        self._local_wake_word = event.local_wake_word
        self._wake_word_model = event.wake_word_model
        self._wake_word_threshold = event.wake_word_threshold
        if self._controller is not None:
            self._controller.enrolled_labels = event.enrolled_labels

    def action_open_settings(self) -> None:
        """Keyboard equivalent of clicking the SettingsPanel widget."""
        self.post_message(SettingsPanel.OpenSettings())

    async def show_menu(
        self,
        question: str,
        options: list[str],
        external_result: asyncio.Future[int] | None = None,
    ) -> int:
        """Push a click- or voice-to-select modal and return the chosen index.

        Returns -1 if the user dismisses the modal without choosing. If
        `external_result` is supplied, the modal closes when that future
        resolves — used by the channel driver to wire voice answers in.

        Uses the callback form of push_screen rather than push_screen_wait,
        because the channel driver runs as a regular asyncio task — not a
        Textual worker — and push_screen_wait raises a NoActiveWorker error
        in that context.
        """
        from crab.ui.modals import SelectMenuModal

        result_future: asyncio.Future[int] = asyncio.get_event_loop().create_future()

        def _on_dismiss(value: int | None) -> None:
            if result_future.done():
                return
            result_future.set_result(int(value) if value is not None else -1)

        modal = SelectMenuModal(question, options, external_result=external_result)
        self.push_screen(modal, _on_dismiss)
        return await result_future

    def action_toggle_mouse(self) -> None:
        """Toggle Textual's mouse capture so text can be selected natively.

        When OFF: the terminal handles drag-to-select normally, but Textual
        no longer sees mouse events (settings panel becomes keyboard-only).
        When ON: Textual receives all mouse events as usual.
        """
        driver = self._driver
        if driver is None:
            return
        self._mouse_capture_enabled = not self._mouse_capture_enabled
        if self._mouse_capture_enabled:
            driver._enable_mouse_support()
            self.add_tool_use(
                "[MOUSE] capture ON — text-select blocked; settings clickable"
            )
        else:
            driver._disable_mouse_support()
            self.add_tool_use(
                "[MOUSE] capture OFF — drag to select text freely (Ctrl+T to restore)"
            )

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
        prev = self._status
        self._status = status
        # Audible cues for visualiser state transitions:
        #   Ping (any → listening): the mic just opened — wake-word fired,
        #                           permission ask, or menu prompt.
        #   Pop  (listening → thinking, OR thinking → idle): a transient
        #                           "done with this stage" cue. Fires both
        #                           when the user finishes speaking and when
        #                           Claude finishes replying.
        if status == "listening" and prev != "listening":
            self._chime("Ping")
        elif (status == "thinking" and prev == "listening") or (
            status == "idle" and prev == "thinking"
        ):
            self._chime("Pop")
        # Clear Claude-set status label when returning to idle so the next
        # turn doesn't inherit stale flavour text.
        if status == "idle":
            self._custom_label = ""
        if status != "listening":
            self.query_one("#cmd-input", Input).placeholder = "Type a command or speak..."
        self._render_visualiser()

    def set_label(self, label: str) -> None:
        """Override the cycling thinking label with Claude-controlled text.

        Pass an empty string to revert to the default crab puns. Called from
        the channel driver in response to the `set_status` MCP tool.
        """
        self._custom_label = (label or "").strip()
        self._render_visualiser()

    def _chime(self, name: str) -> None:
        """Fire-and-forget macOS afplay for a short system sound."""
        try:
            subprocess.Popen(
                ["afplay", f"/System/Library/Sounds/{name}.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass

    def set_partial(self, text: str) -> None:
        inp = self.query_one("#cmd-input", Input)
        if not inp.value:
            inp.placeholder = text if text else "Type a command or speak..."

    def add_user_message(self, text: str) -> None:
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
            self._narrate_scan_buf = ""
            self._history.append({"role": "assistant", "segments": [("text", clean)]})
        self._process_narrate_stream(clean)
        self._render_history()

    def _process_narrate_stream(self, new_text: str) -> None:
        """Scan streaming text for ``<narrate>`` tags and speak them.

        Appends the incoming chunk to a running scan buffer, extracts any
        complete ``<narrate>...</narrate>`` blocks, and fires a TTS task
        for each. Truncates the buffer once consumed (or when it grows
        large with no open ``<narrate`` prefix) so memory stays bounded.

        Args:
            new_text: Newly arrived assistant text segment, already
                ANSI-stripped.
        """
        self._narrate_scan_buf += new_text
        matches = list(_NARRATE_TAG_RE.finditer(self._narrate_scan_buf))
        for m in matches:
            narrate_text = m.group(1).strip()
            if narrate_text:
                self._enqueue_tts(narrate_text, wait=False)
        if matches:
            self._narrate_scan_buf = self._narrate_scan_buf[matches[-1].end():]
        elif (
            len(self._narrate_scan_buf) > 500
            and "<narrate" not in self._narrate_scan_buf
        ):
            self._narrate_scan_buf = ""

    def add_tool_use(self, label: str) -> None:
        if label.startswith("[DONE]"):
            self._finalise_assistant_tts()
            self._history.append({"role": "done", "text": label})
        elif self._history and self._history[-1]["role"] == "assistant":
            self._history[-1]["segments"].append(("tool", label))
        else:
            self._history.append({"role": "assistant", "segments": [("tool", label)]})
        self._render_history()

    async def finalise_assistant_turn(self) -> None:
        """Queue the `<tts>` block of the latest assistant turn AND wait for it.

        Awaits whatever is currently playing PLUS this clip — guarantees that
        the next listening cycle doesn't open the mic while we're still
        speaking. Safe to call multiple times on the same turn (no-op after
        the first; the turn is marked as finalised).
        """
        fut = self._finalise_assistant_tts()
        if fut is not None:
            try:
                await fut
            except asyncio.CancelledError:
                raise

    def _finalise_assistant_tts(self) -> asyncio.Future[None] | None:
        """Extract the ``<tts>`` block from the most recent assistant turn
        and queue it for playback.

        Returns a Future that resolves once the clip finishes playing, or
        ``None`` if the turn has nothing to say (or was already finalised).
        """
        for msg in reversed(self._history):
            if msg["role"] != "assistant":
                continue
            if "tts" in msg:
                return None
            text_chunks = [
                seg for kind, seg in msg["segments"] if kind == "text"
            ]
            if not text_chunks:
                msg["tts"] = ""
                return None
            combined = _NARRATE_TAG_RE.sub("", "".join(text_chunks))
            _, tts_text = _extract_tts(combined)
            msg["tts"] = tts_text
            if tts_text:
                _LOGGER.info("TTS: %s", tts_text)
                return self._enqueue_tts(tts_text, wait=True)
            return None
        return None

    def speak(self, text: str) -> None:
        """Fire-and-forget TTS. Queued; never interrupts whatever is playing."""
        if not text.strip():
            return
        self._enqueue_tts(text, wait=False)

    async def speak_and_wait(self, text: str) -> None:
        """Queue `text` and block until it finishes playing (after anything ahead)."""
        if not text.strip():
            return
        fut = self._enqueue_tts(text, wait=True)
        if fut is not None:
            try:
                await fut
            except asyncio.CancelledError:
                raise

    def _enqueue_tts(
        self, text: str, *, wait: bool
    ) -> asyncio.Future[None] | None:
        """Append a clip to the TTS playback queue.

        Returns a Future that resolves when *this clip* finishes (only when
        `wait=True`). Callers passing wait=False get None and rely on the
        queue's FIFO ordering for correctness.
        """
        fut: asyncio.Future[None] | None = None
        if wait:
            fut = asyncio.get_event_loop().create_future()
        try:
            self._tts_queue.put_nowait((text, fut))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("TTS enqueue failed: %s", exc)
            if fut is not None and not fut.done():
                fut.set_result(None)
        return fut

    async def _tts_worker(self) -> None:
        """Single consumer that plays queued clips one at a time."""
        while True:
            try:
                item = await self._tts_queue.get()
            except asyncio.CancelledError:
                return
            if item is None:
                return
            text, fut = item
            try:
                if self._tts_enabled:
                    await self._speak_one(text)
            except asyncio.CancelledError:
                if fut is not None and not fut.done():
                    fut.set_result(None)
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("TTS error: %s", exc)
            finally:
                if fut is not None and not fut.done():
                    fut.set_result(None)

    async def _speak_one(self, text: str) -> None:
        """Play a single TTS clip via the configured provider."""
        if self._tts_provider == _TTS_PROVIDER_MACOS:
            try:
                self._tts_proc = await asyncio.create_subprocess_exec(
                    "say", "-v", "Daniel (Enhanced)", text,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await self._tts_proc.wait()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("TTS failed: %s", exc)
            finally:
                self._tts_proc = None

    def add_error_message(self, text: str) -> None:
        self._history.append({"role": "error", "text": text})
        self._render_history()

    # -- Speechmatics integration -------------------------------------------

    async def _run_speechmatics(self) -> None:
        assert self._stop_event is not None
        if self._local_wake_word:
            await self._run_with_local_wake_word(self._stop_event)
        else:
            await self._run_sm_session(self._stop_event)

        if not self._exiting and not self._restarting_sm:
            self.exit()

    async def _run_with_local_wake_word(self, stop_event: asyncio.Event) -> None:
        from crab.asr.wake_word import OpenWakeWordDetector
        detector = OpenWakeWordDetector(
            model=self._wake_word_model,
            threshold=self._wake_word_threshold,
        )
        while not stop_event.is_set():
            self.set_status("idle")
            try:
                detected = await detector.wait_for_wake(self._device_index, stop_event)
            except Exception as exc:
                self.add_error_message(f"[ERROR] Wake word detector: {exc}")
                return
            if not detected:
                return
            # Transition to listening (triggers Ping sound) then run one command cycle.
            self.set_status("listening")
            session_stop = asyncio.Event()
            await self._run_sm_session(stop_event, session_stop=session_stop, one_shot=True)
            if not stop_event.is_set():
                await asyncio.sleep(0.5)

    async def _run_sm_session(
        self,
        stop_event: asyncio.Event,
        session_stop: asyncio.Event | None = None,
        one_shot: bool = False,
    ) -> None:
        """One Speechmatics connection lifetime.

        In *one_shot* mode the session ends automatically after the first
        complete response cycle (used when local wake word pre-fires the
        trigger).  Otherwise it runs until *stop_event* is set.
        """
        controller = VoiceController(enrolled_labels=self._enrolled_labels, ui=self)
        if one_shot:
            # Wake word already detected locally — skip SM-side wake word detection.
            controller.state = _State.ACCUMULATING
        self._controller = controller

        mic = Microphone(
            sample_rate=self._audio_format.sample_rate,
            chunk_size=self._audio_format.chunk_size,
            device_index=self._device_index,
        )
        if not mic.start():
            self.add_error_message("[ERROR] Microphone not available — install with `pip install pyaudio`.")
            return

        try:
            async with AsyncClient(api_key=self._api_key, url=self._rt_url) as client:

                @client.on(ServerMessageType.RECOGNITION_STARTED)
                def _on_started(message: dict[str, Any]) -> None:
                    if _DEBUG:
                        self.add_tool_use("[DBG MSG] RECOGNITION_STARTED")
                    if not one_shot:
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
                    if session_stop is not None:
                        session_stop.set()

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
                    channel_driver(
                        controller=controller,
                        ui=self,
                        stop_event=stop_event,
                    ),
                    name="channel-driver",
                )

                if one_shot and session_stop is not None:
                    async def _one_shot_watch() -> None:
                        await controller.response_done.wait()
                        session_stop.set()

                    async def _either_stop() -> None:
                        t1 = asyncio.create_task(stop_event.wait())
                        t2 = asyncio.create_task(session_stop.wait())
                        try:
                            await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                        finally:
                            t1.cancel()
                            t2.cancel()

                    watcher_task: asyncio.Task[None] | None = asyncio.create_task(
                        _one_shot_watch(), name="one-shot-watch"
                    )
                    stop_task = asyncio.create_task(_either_stop(), name="stop-wait")
                else:
                    watcher_task = None
                    stop_task = asyncio.create_task(stop_event.wait(), name="stop-wait")

                all_tasks: set[asyncio.Task[Any]] = {pump_task, driver_task, stop_task}
                if watcher_task is not None:
                    all_tasks.add(watcher_task)

                try:
                    await asyncio.wait(all_tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if not one_shot:
                        stop_event.set()
                    for task in all_tasks:
                        task.cancel()
                    for task in all_tasks:
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
                    try:
                        await asyncio.wait_for(client.stop_session(), timeout=3.0)
                    except Exception:
                        _LOGGER.debug("stop_session error (ignored)", exc_info=True)

        except AuthenticationError as exc:
            self.add_error_message(f"[ERROR] Authentication failed: {exc}")
            await asyncio.sleep(2)
        finally:
            mic.stop()
