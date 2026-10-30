import json
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from emerge.exceptions import ConfigError

CONFIG_FILE = "config.json"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(_Section):
    """How to reach a provider: what every provider entry has."""

    base_url: str = Field(min_length=1)
    api_key: str = ""
    # Milliseconds without a single byte from the service before a connection counts as dead.
    # Heartbeats count, so a model may think as long as it likes on a streamed request; requests
    # without streaming hear nothing until the end and are not limited (see models/factory.py).
    timeout: float = Field(default=600000, gt=0)


class OpenAIOptions(_Section):
    # Responses API only. How much of the reasoning comes back as readable text: "auto", "concise"
    # or "detailed"; empty asks for none. The raw reasoning never comes back.
    reasoning_summary: str = "auto"


class ThinkingConfig(_Section):
    # "adaptive" lets Claude decide whether and how much to think, its depth set by reasoning_effort.
    type: Literal["adaptive", "disabled"] = "adaptive"
    # Whether thinking blocks carry a readable summary or come back empty (the default on newer
    # Claude models); billing is the same either way.
    display: Literal["summarized", "omitted"] = "summarized"


class AnthropicOptions(_Section):
    thinking: ThinkingConfig = ThinkingConfig()


class OpenAIProviderConfig(ProviderConfig):
    options: OpenAIOptions = OpenAIOptions()


class AnthropicProviderConfig(ProviderConfig):
    options: AnthropicOptions = AnthropicOptions()


class ProvidersConfig(_Section):
    """The providers that can be used, each with options only it understands.

    `model.provider` picks one. Both can stay configured, so switching providers is a one-word edit.
    """

    openai: OpenAIProviderConfig | None = None
    anthropic: AnthropicProviderConfig | None = None

    def get(self, name: str) -> OpenAIProviderConfig | AnthropicProviderConfig | None:
        return getattr(self, name) if name in type(self).model_fields else None


class ModelLimit(_Section):
    """What the model itself can take: its context window and its longest output. Never sent."""

    context: int = Field(gt=0)
    output: int = Field(gt=0)


class ModelOptions(_Section):
    """Call options every provider understands; each provider sends them under its own names."""

    temperature: float = Field(default=0.0, ge=0, le=2)
    max_output_tokens: int = Field(default=16384, gt=0)
    # How much the model thinks before answering; empty sends nothing.
    reasoning_effort: str = "medium"
    # Faster inference at a higher price; each api_mode sends its own parameter and supports its own tiers.
    speed: Literal["standard", "fast", "ultrafast"] = "standard"


class ModelConfig(_Section):
    provider: Literal["openai", "anthropic"]
    api_mode: Literal["chat_completions", "responses", "anthropic_messages"]
    name: str = Field(min_length=1)
    limit: ModelLimit
    options: ModelOptions = ModelOptions()

    @model_validator(mode="after")
    def _output_within_model_limit(self) -> "ModelConfig":
        if self.options.max_output_tokens > self.limit.output:
            raise ValueError(
                f"model.options.max_output_tokens ({self.options.max_output_tokens}) is above "
                f"model.limit.output ({self.limit.output}), the most this model can produce"
            )
        return self


class RunLimits(_Section):
    # Per-turn budget. All fields default to None (no limit). Omit the entire
    # "limits" key from config.json to disable all limits.
    max_iterations: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens_total: int | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, gt=0)


class AgentConfig(_Section):
    # How much context the agent may use, chosen for cost and speed; at most model.limit.context.
    # Compaction starts at max_context_tokens * compact_threshold.
    max_context_tokens: int = Field(default=100000, gt=0)
    compact_threshold: float = Field(default=0.8, ge=0, le=1)
    max_retries: int = Field(default=3, ge=0)
    limits: RunLimits = RunLimits()


class RuntimeConfig(_Section):
    stop_timeout: float = Field(default=30, gt=0)
    # Messages kept per output port so a consumer can continue past the one it last saw.
    port_buffer: int = Field(default=64, gt=0)


class WorkspaceConfig(_Section):
    providers: ProvidersConfig = ProvidersConfig()
    model: ModelConfig
    agent: AgentConfig = AgentConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    nodes: list[str] = []

    _workspace: Path = PrivateAttr()

    @model_validator(mode="after")
    def _context_within_model_limit(self) -> "WorkspaceConfig":
        if self.agent.max_context_tokens > self.model.limit.context:
            raise ValueError(
                f"agent.max_context_tokens ({self.agent.max_context_tokens}) is above "
                f"model.limit.context ({self.model.limit.context}), the context window of the model"
            )
        return self

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def config_path(self) -> Path:
        return self._workspace / CONFIG_FILE

    @property
    def agents_md(self) -> Path:
        return self._workspace / "AGENTS.md"

    @property
    def user_md(self) -> Path:
        return self._workspace / "USER.md"

    @property
    def memory_md(self) -> Path:
        return self._workspace / "memory" / "MEMORY.md"

    @property
    def code_dir(self) -> Path:
        return self._workspace / "code"

    @property
    def runs_dir(self) -> Path:
        return self._workspace / "runs"

    @property
    def skills_dir(self) -> Path:
        return self._workspace / "skills"


def _format_errors(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"]) or "<root>"
        lines.append(f"  {field}: {item['msg']}")
    return "\n".join(lines)


DEFAULT_WORKSPACE = Path.home() / ".emerge" / "workspace"

# Files copied from the template on first-run workspace init.
_TEMPLATE_FILES = (
    "config.json",
    "AGENTS.md",
    "USER.md",
    "memory/MEMORY.md",
    "code/.gitkeep",
    "runs/.gitkeep",
    "skills/recovery-after-irreversible-failure.md",
    "skills/sense-before-act.md",
)


def _template_dir() -> Path:
    # emerge/workspace_template/ is a sibling of this file inside the package
    return Path(__file__).resolve().parent / "workspace_template"


def init_workspace(workspace: Path | None = None) -> bool:
    """Copy the template workspace to the target path if it does not exist.

    Returns True if a new workspace was created, False if it already existed
    (in which case nothing is touched).
    """
    target = Path(workspace).expanduser().resolve() if workspace else DEFAULT_WORKSPACE
    if target.exists():
        return False
    target.mkdir(parents=True, exist_ok=True)
    src = _template_dir()
    for rel in _TEMPLATE_FILES:
        s = src / rel
        d = target / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    return True


def load_config(workspace: Path | None = None) -> WorkspaceConfig:
    target = Path(workspace).expanduser().resolve() if workspace else DEFAULT_WORKSPACE
    path = target / CONFIG_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: line {e.lineno} column {e.colno}: {e.msg}") from None
    try:
        config = WorkspaceConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"invalid config {path}:\n{_format_errors(e)}") from None
    config._workspace = target
    return config
