from pathlib import Path

from anthropic import AsyncAnthropic
from anthropic import Timeout as AnthropicTimeout
from openai import AsyncOpenAI
from openai import Timeout as OpenAITimeout

from emerge.callbacks import EventEmitter
from emerge.config import AnthropicProviderConfig, ModelConfig, OpenAIProviderConfig, WorkspaceConfig
from emerge.exceptions import ConfigError, ModelAPIModeError
from emerge.models.base import ModelProvider
from emerge.models.client import ModelClient
from emerge.models.providers.anthropic.messages import AnthropicMessagesProvider
from emerge.models.providers.openai.chat_completions import OpenAIChatCompletionsProvider
from emerge.models.providers.openai.responses import OpenAIResponsesProvider
from emerge.models.retry import RateLimiter, RetryPolicy

# Opening a connection involves no model, so a short limit cannot cut off any thinking.
CONNECT_TIMEOUT = 30.0

_OPENAI: dict[str, type[OpenAIResponsesProvider | OpenAIChatCompletionsProvider]] = {
    "responses": OpenAIResponsesProvider,
    "chat_completions": OpenAIChatCompletionsProvider,
}


def create_provider(
    model: ModelConfig, provider: OpenAIProviderConfig | AnthropicProviderConfig, workspace: Path | None = None
) -> ModelProvider:
    """The provider for `model`, built from its entry in `providers` (connection and provider options)."""
    key = (model.provider, model.api_mode)
    if key not in {("openai", "responses"), ("openai", "chat_completions"), ("anthropic", "anthropic_messages")}:
        raise ModelAPIModeError(f"unsupported provider / api_mode: {model.provider} / {model.api_mode}")
    cls = AnthropicMessagesProvider if model.provider == "anthropic" else _OPENAI[model.api_mode]
    speed = model.options.speed
    if speed != "standard" and speed not in cls.speeds:
        raise ConfigError(f"model.options.speed {speed!r} is not supported by {model.provider} / {model.api_mode}")
    if not provider.api_key:
        raise ConfigError(f"providers.{model.provider}.api_key is empty")
    # provider.timeout (milliseconds) bounds silence, not duration: read and write timeouts restart
    # on every byte, heartbeats included. Retries are handled by retry.py, not the SDK.
    silence = provider.timeout / 1000
    limits = {"connect": CONNECT_TIMEOUT, "read": silence, "write": silence, "pool": silence}
    if model.provider == "anthropic":
        # The Anthropic SDK appends /v1 itself, so a configured ".../v1" base_url is trimmed.
        base_url = provider.base_url.rstrip("/").removesuffix("/v1")
        # The Anthropic SDK runs on its own HTTP library (httpx2) and takes only its own Timeout.
        client = AsyncAnthropic(base_url=base_url, api_key=provider.api_key, timeout=AnthropicTimeout(**limits), max_retries=0)
        return AnthropicMessagesProvider(client, model, provider.options, workspace)
    openai_client = AsyncOpenAI(base_url=provider.base_url, api_key=provider.api_key, timeout=OpenAITimeout(**limits), max_retries=0)
    if model.api_mode == "responses":
        return OpenAIResponsesProvider(openai_client, model, provider.options, workspace)
    # Chat Completions has no options of its own: the OpenAI ones are for the Responses API.
    return OpenAIChatCompletionsProvider(openai_client, model, workspace)


def create_client(
    config: WorkspaceConfig, callbacks: EventEmitter | None = None, limiter: RateLimiter | None = None
) -> ModelClient:
    provider = config.providers.get(config.model.provider)
    if provider is None:
        raise ConfigError(f"model.provider {config.model.provider!r} has no entry in providers")
    return ModelClient(
        create_provider(config.model, provider, config.workspace),
        model=config.model.name,
        retry=RetryPolicy(max_retries=config.agent.max_retries),
        limiter=limiter,
        callbacks=callbacks,
    )
