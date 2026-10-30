import asyncio
import contextlib
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import anthropic
import httpx
import httpx2
import openai

from emerge.exceptions import ModelError

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    # No time limit of its own: how long a model may think is not a failure. A dead connection is
    # found by the SDK's HTTP timeouts (see models/factory.py) and raised as a connection error.
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0

    def delay(self, attempt: int, error: BaseException) -> float:
        retry_after = _retry_after(error)
        if retry_after is not None:
            return min(retry_after, self.max_delay)
        return random.uniform(0, min(self.max_delay, self.base_delay * 2**attempt))


class RateLimiter:
    """In-process cap on concurrent model requests; None means unlimited."""

    def __init__(self, max_concurrency: int | None = None) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._semaphore is None:
            yield
            return
        async with self._semaphore:
            yield


_UNLIMITED = RateLimiter()
# Transport errors raised while a stream is being read (a read timeout after too long without any
# data, a dropped connection) can come through the SDKs unwrapped: from httpx for OpenAI, from
# httpx2, the Anthropic SDK's own HTTP library, for Anthropic.
_TRANSPORT_ERRORS = (httpx.TransportError, httpx2.TransportError)
_PROVIDER_ERRORS = (openai.OpenAIError, anthropic.AnthropicError, *_TRANSPORT_ERRORS)


def is_retryable(error: BaseException) -> bool:
    # Connection errors include the SDKs' timeouts: no connection, or no data at all for too long.
    if isinstance(error, (openai.APIConnectionError, anthropic.APIConnectionError, *_TRANSPORT_ERRORS)):
        return True
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        return error.status_code in (408, 409, 429) or error.status_code >= 500
    return False


async def with_retry(
    call: Callable[[], Awaitable[T]], policy: RetryPolicy, limiter: RateLimiter | None = None
) -> T:
    limiter = limiter or _UNLIMITED
    attempt = 0
    while True:
        try:
            async with limiter.slot():
                return await call()
        except ModelError:
            raise
        except _PROVIDER_ERRORS as e:
            if not is_retryable(e) or attempt >= policy.max_retries:
                raise _model_error(e, attempt) from e
            await _backoff(policy, attempt, e)
        attempt += 1


async def retry_stream(
    open_stream: Callable[[], AsyncIterator[T]], policy: RetryPolicy, limiter: RateLimiter | None = None
) -> AsyncIterator[T]:
    """Retries only until the first event is yielded; after that a failure would duplicate output downstream."""
    limiter = limiter or _UNLIMITED
    attempt = 0
    while True:
        yielded = False
        try:
            async with limiter.slot():
                stream = open_stream()
                try:
                    while True:
                        try:
                            item = await anext(stream)
                        except StopAsyncIteration:
                            return
                        yielded = True
                        yield item
                finally:
                    await stream.aclose()  # type: ignore[attr-defined]
        except ModelError:
            raise
        except _PROVIDER_ERRORS as e:
            if yielded or not is_retryable(e) or attempt >= policy.max_retries:
                raise _model_error(e, attempt) from e
            await _backoff(policy, attempt, e)
        attempt += 1


async def _backoff(policy: RetryPolicy, attempt: int, error: BaseException) -> None:
    delay = policy.delay(attempt, error)
    logger.warning("model request failed (%s), retry %d/%d in %.1fs", _describe(error), attempt + 1, policy.max_retries, delay)
    await asyncio.sleep(delay)


def _retry_after(error: BaseException) -> float | None:
    if not isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        return None
    value = error.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _describe(error: BaseException) -> str:
    if isinstance(error, (httpx.TimeoutException, httpx2.TimeoutException)):
        return f"{type(error).__name__}: no data from the service within the time limit"
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        return f"HTTP {error.status_code}: {error.message}"
    return f"{type(error).__name__}: {error}"


def _model_error(error: BaseException, attempt: int) -> ModelError:
    return ModelError(f"model request failed after {attempt + 1} attempt(s): {_describe(error)}")
