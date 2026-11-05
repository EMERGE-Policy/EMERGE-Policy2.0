"""The full-screen TUI (prompt_toolkit + rich), laid out like 1.0's: header, conversation, sidebar,
input box, footer, and a palette of slash commands.

It draws what it is told and asks the application for everything else: a callback handler feeds
`ViewState` with events, a `values` stream subscription keeps the sidebar's run picture current, and
commands go through the main agent (`control`, `submit`) or the runtime's public calls. The user can
keep typing while the main agent works; messages go to its inbox.

Control: `/stop`, `/pause`, `/resume` (the whole run, or one node) and `/interrupt` (the main
agent's current turn) go through the control channel, never through the model. Ctrl+C copies a
selection; otherwise it quits when nothing runs, and when something runs it stops it (the agent's
turn and the run) and quits once that is confirmed. `/quit` quits at once: the session closes the
usual way and open runs carry on when it is opened again.

The robot: each conversation can have a simulation, run as a process of its own on the conversation's
folder (`robot.SimulationProcess`). /new asks which robot and scene the new conversation starts from;
opening a conversation (/sessions) starts its simulation again where it was; switching or quitting
stops it first.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import json
import logging
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application as PromptApplication
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.scroll import scroll_page_down, scroll_page_up
from prompt_toolkit.layout import ConditionalContainer, Float, FloatContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.widgets import Frame, TextArea
from rich.console import Console
from rich.markdown import Markdown

from emerge.agent import list_sessions
from emerge.application import Application
from emerge.callbacks import CallbackHandler
from emerge.events import Event
from emerge.exceptions import RuntimeControlError
from emerge.graph.visualization import to_text
from emerge.runtime import VALUES_EVENT
from emerge.runtime.run import ENDED_STATUSES
from emerge.storage.artifacts import node_dir, run_dir
from robot import SimulationProcess, record_launch, recorded_launch
from tui.onboarding import Cancelled, Option, RobotChoice, ask_choice, choose_robot, prepare_workspace
from tui.selection import CopyTextArea, TerminalClipboard
from tui.state import ENDED, ViewState
from tui.theme import BRAND, style

COMMANDS = [
    ("/stop", "Stop the whole run (it ends as stopped), or one node: /stop <node>"),
    ("/pause", "Pause the whole run, or one node: /pause <node>"),
    ("/resume", "Resume the whole run, or one node: /resume <node>"),
    ("/interrupt", "Cancel the main agent's current turn"),
    ("/graph", "Show the current graph, its file and uncommitted edits"),
    ("/status", "Show every node's state, outcome and errors"),
    ("/runs", "Show this session's runs and their artifact folders"),
    ("/nodes", "Show the node types the main agent can use"),
    ("/new", "Start a new conversation: choose its robot and scene"),
    ("/sessions", "Open another conversation and carry it on, its robot where it was"),
    ("/sidebar", "Show or hide the sidebar"),
    ("/logs", "Switch between the conversation and the logs"),
    ("/theme", "Switch between the dark and the light theme"),
    ("/quit", "Quit; open runs carry on when the session is opened again"),
]

# The TUI's own settings in the workspace: theme and sidebar.
PREFERENCES_FILE = ".tui.json"

STATE_MARK = {
    "pending": "○", "starting": "◔", "running": "●", "paused": "‖", "stopping": "◌",
    "finished": "✓", "failed": "✗", "stopped": "■", "skipped": "–",
}


class TuiEvents(CallbackHandler):
    """Hands every event to the view state."""

    def __init__(self, state: ViewState) -> None:
        self.state = state
        # Events created before this moment belong to a session no longer shown.
        self.since = 0.0

    async def on_event(self, event: Event) -> None:
        if event.timestamp >= self.since:
            self.state.accept(event)


class LogBuffer(logging.Handler):
    """Keeps recent log records for `/logs`; nothing may write to the terminal under the TUI."""

    def __init__(self, state: ViewState, size: int = 400) -> None:
        super().__init__(level=logging.INFO)
        self.state = state
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
        self.state.dirty = True


class LineStyles(Lexer):
    """One style per transcript line, worked out when the transcript is rendered."""

    def __init__(self) -> None:
        self.styles: list[str] = []

    def lex_document(self, document: Document) -> Any:
        def line(index: int) -> list[tuple[str, str]]:
            token = self.styles[index] if index < len(self.styles) else ""
            return [(f"class:{token}" if token else "", document.lines[index])]

        return line


class WorkspaceApp:
    SIDEBAR_WIDTH = 46

    def __init__(self, app: Application, state: ViewState, events: TuiEvents, logs: LogBuffer) -> None:
        self.app = app
        self.state = state
        self.events = events
        self.logs = logs
        self.workspace = app.workspace
        self.preferences_path = self.workspace / PREFERENCES_FILE
        preferences = _preferences(self.workspace)
        self.sidebar = bool(preferences.get("sidebar", True))
        self.light = bool(preferences.get("light", False))
        self.show_logs = False
        self.stopping = False
        self.switching = False
        self.palette_open = False
        self.palette_entries: list[tuple[str, str]] = []
        self.palette_index = 0
        self.palette_title = "Slash commands"
        # A question asked through the palette (`pick`), answered by Enter or Esc.
        self._pick: asyncio.Future[str | None] | None = None
        # The open conversation's simulation, when it has one.
        self.simulation: SimulationProcess | None = None
        self._followed: str | None = None
        self._follow_task: asyncio.Task[None] | None = None
        self._render_cache: dict[int, tuple[Any, list[tuple[str, str]]]] = {}
        self._last_tick = 0.0
        # Commands still running; closing waits for them, so none is cut off half way.
        self._tasks: set[asyncio.Task[Any]] = set()

        self.line_styles = LineStyles()
        self.transcript = CopyTextArea(read_only=True, scrollbar=True, wrap_lines=True, lexer=self.line_styles)
        self.editor = CopyTextArea(height=4, multiline=True, style="class:input", prompt=" › ")
        self.editor.buffer.on_text_changed += lambda _: self.clear_selection()
        self.palette_input = TextArea(height=1, multiline=False, style="class:palette")
        self.palette_input.buffer.on_text_changed += self._palette_changed
        palette = Frame(
            HSplit([self.palette_input, Window(FormattedTextControl(self._palette_text), height=14)]),
            title=lambda: self.palette_title,
        )
        self.side = CopyTextArea(read_only=True, width=self.SIDEBAR_WIDTH, wrap_lines=True)
        self.header = CopyTextArea(read_only=True, height=4, wrap_lines=False, style="class:header")
        self.copy_areas = (self.transcript, self.editor, self.side, self.header)
        for area in self.copy_areas:
            area.focus_after_copy = self.editor.control
        body = VSplit(
            [
                Frame(self.transcript, title=lambda: "Logs" if self.show_logs else "Conversation"),
                ConditionalContainer(
                    Frame(self.side, title="Run"),
                    filter=Condition(lambda: self.sidebar and self.application.output.get_size().columns >= 100),
                ),
            ]
        )
        root = FloatContainer(
            HSplit(
                [
                    self.header,
                    body,
                    Frame(self.editor, title="Message · Enter send · Alt+Enter newline · / commands"),
                    Window(FormattedTextControl(self._footer), height=1, style="class:footer"),
                ]
            ),
            floats=[Float(content=ConditionalContainer(palette, filter=Condition(lambda: self.palette_open)), width=80, top=4)],
            style="class:background",
        )
        self.application: PromptApplication[int] = PromptApplication(
            layout=Layout(root, focused_element=self.editor),
            key_bindings=self._bindings(),
            style=style(self.light),
            full_screen=True,
            mouse_support=True,
            clipboard=TerminalClipboard(),
            min_redraw_interval=0.03,
        )

    # -- running ----------------------------------------------------------------------------------

    async def run(self, robot: RobotChoice | None = None) -> int:
        """Show the open conversation and start its simulation (from `robot` for a new one) until quit."""
        self.show_session()
        self.start_robot(robot)
        # Log records go to /logs while the screen is ours; a stream handler would draw over it.
        root = logging.getLogger()
        streams = [handler for handler in root.handlers if isinstance(handler, logging.StreamHandler)]
        for handler in streams:
            root.removeHandler(handler)
        root.addHandler(self.logs)
        level = root.level
        root.setLevel(min(level, logging.INFO) if level else logging.INFO)
        ticker = asyncio.create_task(self._tick())
        try:
            with patch_stdout(raw=True):
                await self.application.run_async()
        finally:
            root.removeHandler(self.logs)
            root.setLevel(level)
            for handler in streams:
                root.addHandler(handler)
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self._unfollow()
            await self.app.aclose()
            await self.stop_robot()
        return 0

    def _start(self, work: Any) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def show_session(self) -> None:
        """Show the application's session from its stored conversation; live events continue it."""
        session = self.app.session
        assert session is not None
        self.state.reset(session.id, session.record.title)
        self.events.since = time.time()
        self.state.workspace = self.workspace.name
        self.state.model = self.app.config.model.name
        self.state.agent_status = self.app.agent.status
        self.state.load_history(session.history.flatten_messages())
        self.state.note(f"session {session.id} opened" + (f": {session.record.title}" if session.record.title else ""), "session")
        self._render_cache.clear()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            run_id = self.app.session.current_run_id if self.app.session is not None and not self.switching else None
            if run_id != self._followed:
                await self._unfollow()
                self._follow(run_id)
            now = time.monotonic()
            if self.state.dirty or now - self._last_tick >= 1.0:
                self._last_tick = now
                self.refresh()

    def _follow(self, run_id: str | None) -> None:
        """Keep the sidebar on the current run through a `values` stream subscription."""
        self._followed = run_id
        self.state.run_id = run_id
        self.state.nodes.clear()
        if run_id is None:
            return
        run = self._run()
        self.state.run_status = run.status if run else "ended"
        try:
            subscription = self.app.runtime.stream(run_id, ["values"])
        except RuntimeControlError:
            return

        async def follow() -> None:
            async with subscription:
                async for event in subscription:
                    if event.type == VALUES_EVENT:
                        self.state.accept_values(run_id, event.payload)

        self._follow_task = asyncio.create_task(follow(), name=f"tui-values:{run_id}")

    async def _unfollow(self) -> None:
        if self._follow_task is not None:
            self._follow_task.cancel()
            await asyncio.gather(self._follow_task, return_exceptions=True)
            self._follow_task = None
        self._followed = None

    def _run(self) -> Any:
        run_id = self.state.run_id
        return next((run for run in self.app.runtime.runs() if run.run_id == run_id), None)

    @property
    def busy(self) -> bool:
        run = self._run()
        return self.app.agent.status == "working" or (run is not None and run.status in ("running", "paused"))

    # -- keys ------------------------------------------------------------------------------------------

    def _bindings(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("enter")
        def submit(event: Any) -> None:
            if self.palette_open and self._pick is not None:
                choices = self._choices()
                if choices:
                    self._answer(choices[self.palette_index % len(choices)][0])
                return
            if self.palette_open:
                typed = self.palette_input.text.strip()
                if " " in typed and typed.split(" ", 1)[0] in {command for command, _ in COMMANDS}:
                    # A command with an argument, e.g. "/pause camera": run it as typed.
                    self.close_palette()
                    self.command(typed)
                    return
                choices = self._choices()
                if choices:
                    command = choices[self.palette_index % len(choices)][0]
                    self.close_palette()
                    self.command(command)
                return
            text = self.editor.text.strip()
            if not text:
                return
            self.editor.text = ""
            if text.startswith("/"):
                self.command(text)
            elif self.switching:
                self.editor.text = text
                self.state.note("switching sessions; the draft is kept")
            else:
                self.state.user_typed(text)
                self._start(self._submit(text))

        @keys.add("/", filter=Condition(lambda: not self.palette_open))
        def slash(event: Any) -> None:
            if event.current_buffer is self.editor.buffer and not self.editor.text:
                self.open_palette(COMMANDS, "Slash commands", query="/")
            else:
                event.current_buffer.insert_text("/")

        @keys.add("escape", "enter")
        @keys.add("c-j")
        def newline(event: Any) -> None:
            if not self.palette_open:
                self.editor.buffer.insert_text("\n")

        @keys.add("up", filter=Condition(lambda: self.palette_open))
        def up(event: Any) -> None:
            self.palette_index -= 1

        @keys.add("down", filter=Condition(lambda: self.palette_open))
        def down(event: Any) -> None:
            self.palette_index += 1

        @keys.add("pageup", filter=Condition(lambda: not self.palette_open))
        @keys.add("pagedown", filter=Condition(lambda: not self.palette_open))
        def scroll(event: Any) -> None:
            focused = event.app.layout.current_control
            self.transcript.buffer.exit_selection()
            event.app.layout.focus(self.transcript)
            (scroll_page_up if event.key_sequence[0].key == "pageup" else scroll_page_down)(event)
            event.app.layout.focus(focused)

        @keys.add("escape")
        def escape(event: Any) -> None:
            if self._pick is not None:
                self._answer(None)
            elif self.palette_open:
                self.close_palette()
            self.clear_selection()
            self.application.layout.focus(self.editor)

        @keys.add("c-c")
        def control_c(event: Any) -> None:
            if any(area.copy_selection() for area in self.copy_areas):
                return
            if self._pick is not None:
                self._answer(None)
            elif self.palette_open:
                self.close_palette()
            elif self.stopping:
                self.state.note("still stopping; it quits once the stop is confirmed (/quit leaves at once)")
            elif not self.busy:
                self.application.exit(result=0)
            else:
                self.stopping = True
                self._start(self._stop_and_quit())

        return keys

    # -- commands ----------------------------------------------------------------------------------------

    def command(self, text: str) -> None:
        name, _, arg = text.strip().partition(" ")
        arg = arg.strip()
        background = {
            "/stop": lambda: self._control("stop", arg or None),
            "/pause": lambda: self._control("pause", arg or None),
            "/resume": lambda: self._control("resume", arg or None),
            "/interrupt": lambda: self._control("interrupt", None),
            "/graph": self._show_graph,
            "/status": self._show_status,
            "/runs": self._show_runs,
            "/new": self._new,
            "/open": lambda: self._switch(arg),
        }
        if name in background:
            if self.switching and name in ("/new", "/open"):
                self.state.note("already switching sessions")
            else:
                self._start(background[name]())
        elif name == "/nodes":
            self._show_nodes()
        elif name == "/sessions":
            self._start(self._open_sessions())
        elif name in ("/commands", "/help"):
            self.open_palette(COMMANDS, "Slash commands", query="/")
        elif name == "/sidebar":
            self.sidebar = not self.sidebar
            self._save_preferences()
        elif name == "/logs":
            self.show_logs = not self.show_logs
        elif name == "/theme":
            self.light = not self.light
            self.application.style = style(self.light)
            self._save_preferences()
        elif name == "/quit":
            self.application.exit(result=0)
        else:
            self.state.note(f"unknown command {name}; press / at an empty prompt for the list")
        self.state.dirty = True

    async def _submit(self, text: str) -> None:
        session = self.app.session
        if session is not None and not session.record.title and not self.state.session_title:
            title = " ".join(text.split())[:60]
            self.state.session_title = title
            await self.app.storage.update_session(title=title)
        await self.app.agent.submit(text)

    async def _control(self, action: str, node_id: str | None) -> None:
        result = await self.app.agent.control(action, node_id)  # type: ignore[arg-type]
        if not result.done:
            self.state.note(f"{action}: {result.text}")

    async def _stop_and_quit(self) -> None:
        self.state.note("stopping: the main agent's turn and the run; quitting once both have stopped…")
        if self.app.agent.status == "working":
            await self.app.agent.control("interrupt")
        run = self._run()
        if run is not None and run.status not in ENDED_STATUSES:
            await self.app.agent.control("stop")
        self.application.exit(result=0)

    async def _new(self) -> None:
        try:
            choice = await choose_robot(self.pick)
        except Cancelled:
            self.state.note("no new conversation")
            return
        await self._switch(None, choice)

    async def _switch(self, session_id: str | None, choice: RobotChoice | None = None) -> None:
        """Open `session_id`, or a new conversation with the robot `choice`; its simulation starts after."""
        self.switching = True
        self.state.note("closing this conversation…")
        self.refresh()
        try:
            await self.stop_robot()
            await self._unfollow()
            await self.app.switch_session(session_id)
        except Exception as e:
            self.state.note(f"could not switch conversations: {e}", "error")
            return
        finally:
            self.switching = False
        self.show_session()
        self.start_robot(choice)

    # -- the robot -----------------------------------------------------------------------------------------

    def start_robot(self, choice: RobotChoice | None = None) -> None:
        """Start the open conversation's simulation: from `choice` when given, else the one it already has."""
        folder = self.app.session_dir
        if choice is not None:
            record_launch(folder, choice.launch, choice.first_scene)
        if recorded_launch(folder) is None:
            return
        self.simulation = SimulationProcess(folder)
        self.state.note(f"starting the simulation ({self.simulation.launch.driver}); its output goes to {self._log_path()}", "robot")

    def _log_path(self) -> str:
        return (self.app.session_dir / "robot" / "simulation.log").relative_to(self.workspace).as_posix()

    async def stop_robot(self) -> None:
        simulation, self.simulation = self.simulation, None
        if simulation is None or simulation.process.poll() is not None:
            return
        if self.application.is_running:
            self.state.note("stopping the simulation…", "warn")
            self.refresh()
        else:
            # The screen is gone (quitting) and closing a simulator can take a while.
            print("emerge: stopping the simulation…", file=sys.stderr, flush=True)
        await asyncio.to_thread(simulation.stop)

    async def pick(self, title: str, options: list[Option]) -> str | None:
        """Ask in the palette: the chosen option's key, or None on Esc."""
        self._pick = asyncio.get_running_loop().create_future()
        self.open_palette(options, f"{title} · Enter selects · Esc backs out", query="")
        try:
            return await self._pick
        finally:
            self._pick = None

    def _answer(self, key: str | None) -> None:
        if self._pick is not None and not self._pick.done():
            self._pick.set_result(key)
        self.close_palette()

    async def _open_sessions(self) -> None:
        entries: list[tuple[str, str]] = []
        for summary in await list_sessions(self.workspace, limit=50):
            record, runs = summary.session, summary.runs
            flags = []
            if record.status == "turn_open":
                flags.append("turn unfinished")
            open_runs = [run for run in runs if run.status in ("running", "idle", "paused", "closed")]
            if open_runs:
                flags.append(f"{len(open_runs)} run(s) open")
            current = " [current]" if self.app.session and record.id == self.app.session.id else ""
            when = time.strftime("%m-%d %H:%M", time.localtime(record.updated_at))
            title = record.title or "(untitled)"
            entries.append((f"/open {record.id}", f"{when}  {title[:36]}{current}{'  · ' + ', '.join(flags) if flags else ''}"))
        if not entries:
            self.state.note("no sessions in this workspace yet")
            return
        self.open_palette(entries, "Sessions · Enter opens", query="")

    async def _show_graph(self) -> None:
        run_id = self.state.run_id
        if run_id is None:
            self.state.note("no run yet: ask the main agent to start one")
            return
        try:
            report = await self.app.runtime.status(run_id)
        except RuntimeControlError as e:
            self.state.note(str(e))
            return
        states = {node.node_id: node.state for node in report.nodes}
        outcomes = {node.node_id: node.outcome for node in report.nodes if node.outcome}
        lines = [to_text(self.app.runtime.graph(run_id), states, outcomes)]
        status = report.graph_file
        path = Path(status.path).relative_to(self.workspace).as_posix()
        if not status.has_changes:
            lines.append(f"graph file {path}: same as graph version {status.current_version}")
        else:
            lines.append(f"graph file {path}: UNCOMMITTED EDITS{' (written from an older version)' if status.stale else ''}")
            if status.error:
                lines.append(f"  cannot be read as a graph: {status.error}")
            elif status.diff is not None:
                diff = status.diff
                for label, items in (
                    ("added", diff.nodes.added),
                    ("removed", diff.nodes.removed),
                    ("modified", [f"{m.node} ({', '.join(m.fields)})" for m in diff.nodes.modified]),
                    ("new instance", [r.node for r in diff.nodes.replaced]),
                    ("connected", [f"{s.source_node}.{s.source_port} → {s.target_node}.{s.target_port}" for s in diff.subscriptions.added]),
                    ("disconnected", [f"{s.source_node}.{s.source_port} → {s.target_node}.{s.target_port}" for s in diff.subscriptions.removed]),
                    ("rewired", [f"{a.target_node}.{a.target_port}: {b.source_node}.{b.source_port} → {a.source_node}.{a.source_port}" for b, a in diff.subscriptions.rewired]),
                ):
                    if items:
                        lines.append(f"  {label}: {', '.join(items)}")
        self.state.note("\n".join(lines))

    async def _show_status(self) -> None:
        run_id = self.state.run_id
        if run_id is None:
            self.state.note("no run yet")
            return
        try:
            report = await self.app.runtime.status(run_id)
        except RuntimeControlError as e:
            self.state.note(str(e))
            return
        run = report.run
        lines = [f"run {run.run_id}: {run.status}{' (whole run paused)' if run.paused else ''}, graph version {run.graph_version}"]
        for node in report.nodes:
            detail = f" {node.outcome}" + (f" ({node.reason})" if node.reason else "") if node.outcome else ""
            error = f" error: {node.error}" if node.error else ""
            mark = " ⚠ acts on environment" if node.acts_on_environment else ""
            lines.append(f"  {node.node_id} ({node.type}): {node.state}{detail}{error}{mark}")
        for ask in report.pending_asks:
            lines.append(f"  question {ask.id} from {ask.node_id}: {ask.question}")
        self.state.note("\n".join(lines))

    async def _show_runs(self) -> None:
        session = self.app.session
        if session is None:
            return
        runs = await self.app.storage.runs()
        if not runs:
            self.state.note("no runs in this session yet")
            return
        lines = []
        for run in runs:
            directory = run_dir(self.app.session_dir, run.id).relative_to(self.workspace).as_posix()
            current = " [current]" if run.id == session.current_run_id else ""
            lines.append(f"run {run.id}: {run.status}{current} · {directory}/ (trace.jsonl, graph.json)")
            graph = await self.app.storage.load_graph(run.id)
            for spec in graph.nodes if graph is not None else ():
                # Every instance's folder: what a replaced or rerun instance wrote stays where it was.
                folders = sorted(node_dir(self.app.session_dir, run.id, spec.id, spec.instance_id).parent.glob("*/"))
                for folder in folders:
                    current = " [current]" if folder.name == spec.instance_id else ""
                    lines.append(f"  {spec.id}: {folder.relative_to(self.workspace).as_posix()}/{current}")
        self.state.note("\n".join(lines))

    def _show_nodes(self) -> None:
        catalog = self.app.registry.catalog()
        if not catalog:
            self.state.note('no node types are registered: list node modules in config.json ("nodes"), or let the agent write one')
            return
        lines = []
        for entry in catalog:
            mark = " ⚠ acts on environment" if entry.acts_on_environment else ""
            version = f" (generated v{entry.version})" if entry.version else ""
            lines.append(f"{entry.name}{version}{mark}: {entry.description}  · outcomes: {', '.join(entry.outcomes)}")
        self.state.note("\n".join(lines))

    def _save_preferences(self) -> None:
        try:
            self.preferences_path.write_text(json.dumps({"light": self.light, "sidebar": self.sidebar}), encoding="utf-8")
        except OSError:
            pass

    # -- palette ------------------------------------------------------------------------------------------

    def _palette_changed(self, _buffer: Any) -> None:
        self.palette_index = 0

    def _choices(self) -> list[tuple[str, str]]:
        query = self.palette_input.text.strip().lower()
        return [entry for entry in self.palette_entries if query in " ".join(entry).lower()]

    def _palette_text(self) -> list[tuple[str, str]]:
        choices = self._choices()
        if not choices:
            return [("class:palette", " nothing matches")]
        selected = self.palette_index % len(choices)
        start = max(0, selected - 11)
        commands = self.palette_title == "Slash commands"
        return [
            (
                "class:selected" if index == selected else "class:palette",
                f" {'›' if index == selected else ' '} " + (f"{command:<12} {title}" if commands else title) + "\n",
            )
            for index, (command, title) in enumerate(choices[start : start + 14], start)
        ]

    def open_palette(self, entries: list[tuple[str, str]], title: str, *, query: str) -> None:
        self.clear_selection()
        self.palette_entries = entries
        self.palette_title = title
        self.palette_index = 0
        self.palette_open = True
        self.palette_input.text = query
        self.palette_input.buffer.cursor_position = len(query)
        self.application.layout.focus(self.palette_input)

    def close_palette(self) -> None:
        self.palette_open = False
        self.application.layout.focus(self.editor)

    def clear_selection(self) -> None:
        for area in self.copy_areas:
            area.buffer.exit_selection()

    # -- drawing --------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        if self.show_logs:
            lines = [(line, "system") for line in self.logs.lines] or [("no log lines yet", "system")]
        else:
            lines = self.render_transcript()
        content = "\n".join(text for text, _ in lines)
        self.line_styles.styles = [token for _, token in lines]
        at_end = self.transcript.buffer.cursor_position >= len(self.transcript.text)
        cursor = len(content) if at_end else min(self.transcript.buffer.cursor_position, len(content))
        if self.transcript.buffer.selection_state is None:
            self.transcript.buffer.set_document(Document(content, cursor), bypass_readonly=True)
        self.side.set_formatted_text(self._sidebar())
        self.header.set_formatted_text(self._header())
        self.state.dirty = False
        self.application.invalidate()

    def render_transcript(self) -> list[tuple[str, str]]:
        width = max(30, self.application.output.get_size().columns - (self.SIDEBAR_WIDTH + 6 if self.sidebar else 8))
        lines: list[tuple[str, str]] = []
        for entry in self.state.entries:
            block = self._render(entry, width)
            if block:
                if lines:
                    lines.append(("", ""))
                lines.extend(block)
        return lines or [
            ("E M E R G E", "assistant"),
            ("", ""),
            ("Tell the main agent what to do, or press / for commands.", "system"),
        ]

    def _render(self, entry: dict[str, Any], width: int) -> list[tuple[str, str]]:
        role = entry["role"]
        key = id(entry)
        signature = (width, json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True))
        cached = self._render_cache.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        lines: list[tuple[str, str]]
        if role == "user":
            lines = [("YOU" + (" · queued, the agent reads it at its next step" if entry.get("queued") else ""), "user")]
            lines += [(line, "") for line in entry["text"].splitlines()]
        elif role == "assistant":
            body = entry["text"] if entry.get("streaming") else _markdown(entry["text"], width)
            lines = [("EMERGE", "assistant")] + [(line, "") for line in body.splitlines()]
        elif role == "thinking":
            text = entry["text"].strip()
            if not entry.get("streaming"):
                first = text.splitlines()[0] if text else ""
                text = (first[: width - 20] + " …") if first else "…"
            lines = [("  thinking", "thinking")] + [("  " + line, "thinking") for line in text.splitlines()]
        elif role == "tool":
            lines = _tool_lines(entry, width)
        elif role == "notice":
            text_lines = entry["text"].splitlines() or [""]
            lines = [("◆ " + text_lines[0], "notice")] + [("  " + line, "notice") for line in text_lines[1:]]
        elif role == "fork":
            lines = [(f"  ? node {entry.get('node')} asks: {entry.get('question')}", "fork")]
            answer = entry.get("answer")
            lines.append((f"    → {answer}" if answer is not None else "    → answering on the side…", "fork"))
        else:
            text_lines = str(entry["text"]).splitlines() or [""]
            kind = entry.get("kind")
            token = f"system.{kind}" if kind else "system"
            lines = [("· " + text_lines[0], token)] + [("  " + line, token) for line in text_lines[1:]]
        self._render_cache[key] = (signature, lines)
        return lines

    def _header(self) -> list[tuple[str, str]]:
        columns = self.application.output.get_size().columns
        status = self.state.agent_status
        if self.switching:
            status = "switching sessions"
        elif self.stopping:
            status = "stopping"
        meta = f"{self.state.workspace}  ·  {self.state.model}  ·  {status}"
        if columns < 64:
            return [("class:brand.name", "  EmergePolicy\n"), ("class:brand.meta", f"  {meta}")]
        logo = "\n".join("  " + line for line in BRAND)
        return [("class:brand.logo", logo + "\n"), ("class:brand.name", "  EmergePolicy"), ("class:brand.meta", f"  /  {meta}")]

    def _footer(self) -> str:
        clipboard = self.application.clipboard
        if isinstance(clipboard, TerminalClipboard) and time.monotonic() < clipboard.notice_until:
            return f" {clipboard.notice} · Esc clears the selection"
        usage = self.state.usage
        seconds = self.state.turn_seconds if self.state.turn_started is None else time.monotonic() - self.state.turn_started
        cached = f" ({usage['cached']} cached)" if usage["cached"] else ""
        return f" / commands  ·  drag to copy  ·  Ctrl+C copy/stop/quit  ·  {usage['total']} tokens{cached}  ·  turn {seconds:.1f}s"

    def _sidebar(self) -> list[tuple[str, str]]:
        fragments: list[tuple[str, str]] = []

        def row(*parts: tuple[str, str]) -> None:
            for token, text in parts:
                fragments.append(("class:" + token if token else "", text))
            fragments.append(("", "\n"))

        def section(title: str) -> None:
            if fragments:
                row(("sidebar", ""))
            row(("sidebar.section", "◆ " + title))

        state = self.state
        section("SESSION")
        row(("sidebar.value", state.session_title or state.session_id[:12]))
        row(("sidebar.label", "agent  "), ("sidebar.value", state.agent_status))
        section("ROBOT")
        simulation = self.simulation
        if simulation is None:
            row(("sidebar.label", "no simulation"))
        else:
            status = simulation.status
            token = "running" if status == "running" else "pending" if status == "starting" else "failed"
            tick = simulation.tick
            row(("sidebar.value", simulation.launch.driver), ("sidebar.label", "  " + _config_text(simulation.launch.config)))
            row((f"state.{token}", status), ("sidebar.label", f"  · tick {tick}" if tick is not None else ""))
            if token == "failed":
                row(("sidebar.path", self._log_path()))
        section("RUN")
        if state.run_id is None:
            row(("sidebar.label", "no run yet"))
        else:
            run = self._run()
            status = run.status if run is not None else state.run_status or "ended"
            row(("sidebar.label", "id      "), ("sidebar.value", state.run_id[:16]))
            row(("sidebar.label", "status  "), (f"state.{_state_token(status)}", status), ("sidebar.label", f"  · graph v{state.graph_version}"))
            if status == "idle":
                row(("sidebar.warn", "graph idle: every node has ended"))
            elif status == "running":
                row(("sidebar.label", "nodes are running"))
            section("NODES")
            if not state.nodes:
                row(("sidebar.label", "no nodes yet"))
            for node_id, view in state.nodes.items():
                node_state = str(view.get("state", "pending"))
                entry = self.app.registry.find_entry(state.node_types.get(node_id, ""))
                acts_on_environment = entry is not None and entry.acts_on_environment
                detail = view.get("outcome") or (view.get("error") or "")[:40] if node_state in ENDED else ""
                row(
                    (f"state.{_state_token(node_state)}", f"{STATE_MARK.get(node_state, '?')} "),
                    ("acts_on_environment" if acts_on_environment else "sidebar.value", node_id + (" ⚠" if acts_on_environment else "")),
                    ("sidebar.label", f"  {node_state}"),
                    (f"state.{_state_token(node_state)}", f" {detail}" if detail else ""),
                )
            if state.asks:
                section("NODE QUESTIONS")
                for node_id, question in state.asks.values():
                    row(("fork", f"{node_id}: {question[:80]}"))
            if state.recent:
                section("RECENT")
                for line in list(state.recent)[-6:]:
                    row(("sidebar", line[: self.SIDEBAR_WIDTH * 2]))
            section("ARTIFACTS")
            directory = run_dir(self.app.session_dir, state.run_id).relative_to(self.workspace)
            row(("sidebar.path", directory.as_posix() + "/"))
        return fragments


def _config_text(config: dict[str, Any]) -> str:
    return " · ".join(str(value) for value in config.values())


def _state_token(state: str) -> str:
    if state in ("running", "paused", "pending", "finished", "failed", "stopped", "skipped"):
        return state
    return {"idle": "finished", "starting": "running", "stopping": "paused"}.get(state, "other")


def _markdown(text: str, width: int) -> str:
    stream = io.StringIO()
    Console(file=stream, width=width, color_system=None).print(Markdown(text))
    return stream.getvalue().rstrip()


def _tool_lines(entry: dict[str, Any], width: int) -> list[tuple[str, str]]:
    name, arguments = entry.get("tool"), entry.get("arguments") or {}
    status = {"running": "…", "done": "done", "error": "error"}.get(entry.get("status", ""), entry.get("status", ""))
    content = str(entry.get("content") or "")
    lines: list[tuple[str, str]] = []
    if name == "edit_file":
        lines.append((f"  ↳ edit_file {arguments.get('path')} · {status}", "tool"))
        before = str(arguments.get("old_text", "")).splitlines()
        after = str(arguments.get("new_text", "")).splitlines()
        for line in difflib.unified_diff(before, after, lineterm="", n=1):
            if line.startswith(("---", "+++", "@@")):
                continue
            token = "diff.add" if line.startswith("+") else "diff.remove" if line.startswith("-") else "tool"
            lines.append(("    " + line[: width - 6], token))
    elif name == "read_file":
        span = ""
        if arguments.get("offset") or arguments.get("limit"):
            span = f" (from line {arguments.get('offset') or 1}" + (f", {arguments['limit']} lines)" if arguments.get("limit") else ")")
        lines.append((f"  ↳ read_file {arguments.get('path')}{span} · {status}", "tool"))
    elif name == "write_file":
        count = len(str(arguments.get("content", "")).splitlines())
        lines.append((f"  ↳ write_file {arguments.get('path')} ({count} lines) · {status}", "tool"))
    elif name == "commit_graph":
        dry = " (dry run)" if arguments.get("dry_run") else ""
        lines.append((f"  ↳ commit_graph{dry} · {status}", "tool"))
        lines += [("    " + line, "tool") for line in content.splitlines()]
    else:
        shown = json.dumps(arguments, ensure_ascii=False)
        shown = shown if len(shown) <= 100 else shown[:97] + "..."
        lines.append((f"  ↳ {name}" + (f" {shown}" if arguments else "") + f" · {status}", "tool"))
    if content and name not in ("commit_graph",) and (entry.get("status") == "error" or name not in ("edit_file", "write_file")):
        first = content.strip().splitlines()[0] if content.strip() else ""
        if first:
            lines.append(("    " + (first if len(first) <= width - 6 else first[: width - 9] + "..."), "tool"))
    return lines


async def run_tui(workspace: Path, session_id: str | None) -> int:
    """Open the workspace in the TUI: `session_id` carried on, or a new conversation.

    Before the screen is taken over: the workspace is set up when it needs a model provider, and a new
    conversation's robot is chosen. Start-up errors (`StartupError`) are raised before then too, so they
    print plainly. Backing out of a question quits.
    """
    workspace = Path(workspace).expanduser().resolve()
    light = bool(_preferences(workspace).get("light", False))
    try:
        if not await prepare_workspace(workspace, light=light):
            return 1
        robot = await choose_robot(ask_choice(light)) if session_id is None else None
    except Cancelled:
        return 1
    state = ViewState()
    events = TuiEvents(state)
    logs = LogBuffer(state)
    # Start-up already logs (node modules, the model check); keep it for /logs, off the terminal.
    logging.getLogger().addHandler(logs)
    logging.getLogger().setLevel(logging.INFO)
    print(f"emerge: opening {workspace} …", file=sys.stderr, flush=True)
    app = await Application.create(workspace, session_id=session_id, new_session=session_id is None, handlers=(events,))
    return await WorkspaceApp(app, state, events, logs).run(robot)


def _preferences(workspace: Path) -> dict[str, Any]:
    try:
        preferences = json.loads((workspace / PREFERENCES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return preferences if isinstance(preferences, dict) else {}
