"""What happens before a conversation opens: the workspace's model settings, and which robot it gets.

`prepare_workspace` creates the workspace from the template when it is missing and, while the model
provider has no API key, asks for the provider, its URL, the key and the model, one full-screen step at
a time. `choose_robot` asks which environment and which scene a new conversation's robot starts from;
it asks through any `Pick`, so the same questions run as full-screen steps before the TUI opens
(`ask`) and in the TUI's palette for /new.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

from emerge.config import CONFIG_FILE, api_key_missing, init_workspace, load_config
from robot import Launch, SceneChoice, environment_names, load_driver_class
from tui.theme import BRAND, style

# (key, label): one option of a step. The key is what is returned when it is chosen.
Option = tuple[str, str]
# Asks one question: a title and its options; returns the chosen key, or None when the person backs out.
Pick = Callable[[str, list[Option]], Awaitable[str | None]]

NO_ROBOT = ""
_PROVIDERS: list[Option] = [
    ("openai/responses", "OpenAI · Responses API"),
    ("openai/chat_completions", "OpenAI-compatible · Chat Completions API"),
    ("anthropic/anthropic_messages", "Anthropic · Messages API"),
]


class Cancelled(Exception):
    """The person backed out of a question."""


@dataclass(frozen=True)
class RobotChoice:
    launch: Launch
    first_scene: bytes


# -- one full-screen step --------------------------------------------------------------------------


async def ask(
    title: str,
    description: str = "",
    *,
    options: list[Option] | None = None,
    default: str = "",
    secret: bool = False,
    step: str = "",
    light: bool = False,
) -> str | None:
    """One full-screen question: pick from `options` (typing filters them), or type a line of text.

    Returns the chosen key or the text; None when the person presses Esc or Ctrl+C.
    """
    field = TextArea(text="" if options else default, multiline=False, password=secret, prompt=" › ", style="class:input")
    field.buffer.cursor_position = len(field.text)
    selected = 0

    def matches() -> list[Option]:
        query = field.text.strip().lower()
        return [option for option in options or [] if query in f"{option[0]} {option[1]}".lower()]

    def listing() -> list[tuple[str, str]]:
        shown = matches()
        if not shown:
            return [("class:step.detail", "   nothing matches")]
        index = selected % len(shown)
        start = max(0, min(index - 7, len(shown) - 15))
        lines = []
        for position, (_, label) in enumerate(shown[start : start + 15], start):
            token = "class:selected" if position == index else "class:step.option"
            lines.append((token, f" {'›' if position == index else ' '} {label} \n"))
        if len(shown) > 15:
            lines.append(("class:step.detail", f"   {index + 1} / {len(shown)}"))
        return lines

    def reset_selection(_buffer: object) -> None:
        nonlocal selected
        selected = 0

    field.buffer.on_text_changed += reset_selection
    keys = KeyBindings()

    @keys.add("up")
    def up(_event: object) -> None:
        nonlocal selected
        selected -= 1

    @keys.add("down")
    def down(_event: object) -> None:
        nonlocal selected
        selected += 1

    @keys.add("enter")
    def accept(event: object) -> None:
        if options is None:
            if field.text.strip():
                event.app.exit(result=field.text.strip())
            return
        shown = matches()
        if shown:
            event.app.exit(result=shown[selected % len(shown)][0])

    @keys.add("escape")
    @keys.add("c-c")
    def back_out(event: object) -> None:
        event.app.exit(result=None)

    hint = "↑↓ choose · type to filter · Enter select · Esc back" if options else "Enter continue · Esc back"
    body = [
        Window(FormattedTextControl([("class:brand.logo", "\n".join("  " + line for line in BRAND))]), height=4),
        Window(height=1),
        Window(FormattedTextControl([("class:step.counter", f"  {step}")]), height=1),
        Window(FormattedTextControl([("class:step.title", f"  {title}")]), height=1),
        Window(FormattedTextControl([("class:step.description", description)]), height=3, wrap_lines=True),
        field,
        Window(height=1),
    ]
    if options is not None:
        body.append(Window(FormattedTextControl(listing), height=16))
    body += [Window(), Window(FormattedTextControl([("class:step.keys", f"  {hint}")]), height=1)]
    screen: Application[str | None] = Application(
        layout=Layout(HSplit(body, style="class:background"), focused_element=field),
        key_bindings=keys,
        style=style(light),
        full_screen=True,
    )
    return await screen.run_async()


# -- the workspace ---------------------------------------------------------------------------------


async def prepare_workspace(workspace: Path, *, light: bool = False) -> bool:
    """Make the workspace ready to open: created from the template, with a model provider and its key.

    Returns False when the person backs out; the workspace then stays as it was.
    """
    init_workspace(workspace)
    config = load_config(workspace)
    if not api_key_missing(config):
        return True

    async def step(number: int, title: str, description: str, **kwargs: object) -> str:
        answer = await ask(title, description, step=f"WORKSPACE SETUP · {number} / 4", light=light, **kwargs)
        if answer is None:
            raise Cancelled
        return answer

    path = workspace / CONFIG_FILE
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        chosen = await step(1, "Model provider", f"The service the main agent talks to. Saved in {path}.", options=_PROVIDERS)
        provider, api_mode = chosen.split("/")
        entry = raw.setdefault("providers", {}).setdefault(provider, {})
        entry["base_url"] = await step(2, "API URL", "Where the provider's API is served.", default=entry.get("base_url", ""))
        entry["api_key"] = await step(3, "API key", "The key for that URL; it is stored in the workspace's config.json.", secret=True)
        model = raw.setdefault("model", {})
        model["name"] = await step(
            4, "Model", "The model's name at that provider. Its limits and options stay in config.json, to edit there.",
            default=model.get("name", "") if model.get("provider") == provider else "",
        )
    except Cancelled:
        return False
    model.update(provider=provider, api_mode=api_mode)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


# -- the robot -------------------------------------------------------------------------------------


async def choose_robot(pick: Pick) -> RobotChoice | None:
    """Ask for an environment, then a group of its scenes (e.g. a task), then the scene (e.g. a layout).

    Returns None for a conversation without a robot. Raises Cancelled when the person backs out.
    """
    environments = {name: load_driver_class(name) for name in environment_names()}
    options = [(name, f"{name}  ·  {_summary(cls)}") for name, cls in environments.items()]
    options.append((NO_ROBOT, "no robot  ·  the main agent alone"))
    name = await _required(pick("Robot", options))
    if name == NO_ROBOT:
        return None
    scenes = environments[name].scenes()
    groups = list(dict.fromkeys(scene.group for scene in scenes))
    group = await _required(pick(f"{name} · scene", [(group, group) for group in groups]))
    in_group = [scene for scene in scenes if scene.group == group]
    index = int(await _required(pick(f"{name} · {group}", [(str(i), scene.name) for i, scene in enumerate(in_group)])))
    return _choice(name, in_group[index])


def ask_choice(light: bool = False) -> Pick:
    """`ask` as a Pick: full-screen steps, for before the TUI opens."""
    asked = 0

    async def pick(title: str, options: list[Option]) -> str | None:
        nonlocal asked
        asked += 1
        return await ask(title, options=options, step=f"NEW CONVERSATION · {asked} / 3", light=light)

    return pick


def _choice(environment: str, scene: SceneChoice) -> RobotChoice:
    first_scene = json.dumps(scene.scene, ensure_ascii=False, indent=2).encode("utf-8")
    return RobotChoice(Launch(environment, dict(scene.config)), first_scene)


async def _required(answer: Awaitable[str | None]) -> str:
    value = await answer
    if value is None:
        raise Cancelled
    return value


def _summary(cls: type) -> str:
    doc = (cls.__doc__ or cls.__name__).strip()
    return doc.splitlines()[0]
