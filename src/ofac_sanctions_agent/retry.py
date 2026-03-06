"""
retry.py — Async retry logic with exponential backoff and jitter.

Usage:
    from ofac_sanctions_agent.retry import retry_with_backoff, RetryExhausted

    result = await retry_with_backoff(
        my_async_fn,
        arg1, arg2,
        max_retries=3,
        base_delay=1.0,
        exceptions=(TimeoutError, SomeOtherError),
    )
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from typing import Any, Callable, Coroutine, Dict, Tuple, Type


logger = logging.getLogger(__name__)


class RetryExhausted(Exception):
    """Raised when all retry attempts have been exhausted."""

    def __init__(self, message: str, attempts: int, last_exception: Exception):
        super().__init__(message)
        self.attempts = attempts
        self.last_exception = last_exception


async def retry_with_backoff(
    func: Callable[..., Coroutine[Any, Any, Any]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    on_retry: Callable[[int, Exception, float], None] | None = None,
    **kwargs: Any,
) -> Any:
    """
    Call ``func(*args, **kwargs)`` up to ``max_retries + 1`` times.

    Delay between attempts follows: delay = min(base_delay * 2^attempt, max_delay)
    If jitter=True, delay is multiplied by a uniform random value in [0.5, 1.5].

    Args:
        func:         Async callable to execute.
        *args:        Positional arguments forwarded to ``func``.
        max_retries:  Number of *additional* attempts after the first failure.
        base_delay:   Initial delay in seconds before the first retry.
        max_delay:    Upper cap on computed delay (seconds).
        jitter:       Add ±50 % random noise to each delay to avoid thundering herd.
        exceptions:   Tuple of exception types that trigger a retry.
                      Any other exception propagates immediately.
        on_retry:     Optional callback(attempt, exc, next_delay) called before each sleep.
        **kwargs:     Keyword arguments forwarded to ``func``.

    Returns:
        Return value of ``func`` on success.

    Raises:
        RetryExhausted: When every attempt fails with a retryable exception.
        Exception:      Any non-retryable exception from ``func`` (re-raised immediately).
    """
    total_attempts = max_retries + 1
    last_exc: Exception = RuntimeError("retry_with_backoff: no attempts made")

    for attempt in range(total_attempts):
        attempt_start = time.monotonic()
        try:
            return await func(*args, **kwargs)

        except exceptions as exc:  # retryable
            last_exc = exc
            elapsed = time.monotonic() - attempt_start

            if attempt == max_retries:
                logger.error(
                    "[retry] %s failed on attempt %d/%d after %.2fs: %s",
                    _func_name(func),
                    attempt + 1,
                    total_attempts,
                    elapsed,
                    exc,
                )
                break

            delay = _compute_delay(attempt, base_delay, max_delay, jitter)

            logger.warning(
                "[retry] %s attempt %d/%d failed (%.2fs): %s — retrying in %.2fs",
                _func_name(func),
                attempt + 1,
                total_attempts,
                elapsed,
                exc,
                delay,
            )

            if on_retry is not None:
                try:
                    on_retry(attempt + 1, exc, delay)
                except Exception:
                    pass  # never let callback crash the retry loop

            await asyncio.sleep(delay)

    raise RetryExhausted(
        f"{_func_name(func)}: all {total_attempts} attempt(s) exhausted",
        attempts=total_attempts,
        last_exception=last_exc,
    ) from last_exc


def _compute_delay(
    attempt: int,
    base_delay: float,
    max_delay: float,
    jitter: bool,
) -> float:
    """Return the sleep duration before attempt ``attempt + 1``."""
    delay = min(base_delay * (2**attempt), max_delay)
    if jitter:
        # Multiply by uniform [0.5, 1.5] — keeps average delay at ``delay``
        delay *= 0.5 + random.random()
    return delay


def _func_name(func: Callable) -> str:
    return getattr(func, "__name__", repr(func))


# ---------------------------------------------------------------------------
# Error taxonomy — structured failure classification
# ---------------------------------------------------------------------------

class ErrorKind(enum.Enum):
    """Taxonomy of failure modes with distinct retry semantics."""
    NETWORK  = "NETWORK"   # connectivity / DNS / socket failures → retry with backoff
    SELECTOR = "SELECTOR"  # DOM element not found → try fallback chain first
    CAPTCHA  = "CAPTCHA"   # bot challenge detected → skip immediately, no retry
    TIMEOUT  = "TIMEOUT"   # Playwright / network timeout → retry with backoff
    UNKNOWN  = "UNKNOWN"   # unclassified → conservative single retry


# Per-kind retry policy: max_retries=0 means "fail immediately, no retry".
RETRY_POLICIES: Dict[ErrorKind, Dict[str, Any]] = {
    ErrorKind.NETWORK:  {"max_retries": 3, "base_delay": 2.0, "max_delay": 30.0},
    ErrorKind.TIMEOUT:  {"max_retries": 2, "base_delay": 3.0, "max_delay": 20.0},
    ErrorKind.SELECTOR: {"max_retries": 1, "base_delay": 1.0, "max_delay": 10.0},
    ErrorKind.CAPTCHA:  {"max_retries": 0, "base_delay": 0.0, "max_delay": 0.0},
    ErrorKind.UNKNOWN:  {"max_retries": 1, "base_delay": 2.0, "max_delay": 15.0},
}


def classify_error(exc: Exception) -> ErrorKind:
    """
    Classify *exc* into an :class:`ErrorKind` to select the right retry policy.

    Uses type-name and message heuristics so that this module stays free of
    Playwright imports at load time.
    """
    exc_type = type(exc).__name__.lower()
    exc_str  = str(exc).lower()

    if "captcha" in exc_str or "recaptcha" in exc_str:
        return ErrorKind.CAPTCHA

    if "timeout" in exc_type or "timeout" in exc_str:
        return ErrorKind.TIMEOUT

    if (
        isinstance(exc, (ConnectionError, OSError))
        or any(kw in exc_str for kw in (
            "connection refused", "network error", "unreachable",
            "name resolution", "dns", "socket",
        ))
        or any(kw in exc_type for kw in ("connection", "network", "socket"))
    ):
        return ErrorKind.NETWORK

    if any(kw in exc_str for kw in (
        "selector", "element not found", "locator", "not visible", "not attached",
        "waiting for selector",
    )):
        return ErrorKind.SELECTOR

    return ErrorKind.UNKNOWN


async def retry_with_taxonomy(
    func: Callable[..., Coroutine[Any, Any, Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    Execute *func* with an adaptive retry strategy determined by classifying
    the first exception via :func:`classify_error`.

    Differences from :func:`retry_with_backoff`:

    * The retry count, base delay, and max delay are chosen automatically from
      :data:`RETRY_POLICIES` based on the error kind.
    * CAPTCHA failures raise :class:`RetryExhausted` immediately (0 retries).
    * SELECTOR failures get 1 quick retry; NETWORK/TIMEOUT get more retries
      with longer delays.
    * If the exception kind changes on a subsequent attempt the original policy
      is kept for consistency within a single call.
    """
    last_exc: Exception = RuntimeError("retry_with_taxonomy: no attempts made")

    # ---- first attempt -------------------------------------------------------
    try:
        return await func(*args, **kwargs)
    except Exception as exc:
        last_exc = exc
        kind = classify_error(exc)
        policy = RETRY_POLICIES[kind]
        logger.warning(
            "[taxonomy] Attempt 1 failed — %s (%s): %s",
            _func_name(func), kind.value, exc,
        )

    # ---- check policy --------------------------------------------------------
    max_retries: int = policy["max_retries"]
    if max_retries == 0:
        logger.info("[taxonomy] %s — 0 retries configured, failing immediately", kind.value)
        raise RetryExhausted(
            f"retry_with_taxonomy: {kind.value} error, no retries allowed",
            attempts=1,
            last_exception=last_exc,
        ) from last_exc

    # ---- retry loop ----------------------------------------------------------
    for attempt in range(max_retries):
        delay = _compute_delay(attempt, policy["base_delay"], policy["max_delay"], jitter=True)
        logger.warning(
            "[taxonomy] %s — retry %d/%d in %.2fs",
            kind.value, attempt + 1, max_retries, delay,
        )
        await asyncio.sleep(delay)
        try:
            return await func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[taxonomy] Retry %d/%d failed — %s: %s",
                attempt + 1, max_retries, classify_error(exc).value, exc,
            )

    raise RetryExhausted(
        f"retry_with_taxonomy: {_func_name(func)} exhausted {max_retries + 1} attempt(s) ({kind.value})",
        attempts=max_retries + 1,
        last_exception=last_exc,
    ) from last_exc


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    jitter: bool = True,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
):
    """
    Decorator that wraps an async function with retry-on-failure logic.

    Example::

        @with_retry(max_retries=2, base_delay=0.5, exceptions=(TimeoutError,))
        async def fetch_page(url: str) -> str:
            ...
    """

    def decorator(func: Callable[..., Coroutine]) -> Callable[..., Coroutine]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await retry_with_backoff(
                func,
                *args,
                max_retries=max_retries,
                base_delay=base_delay,
                max_delay=max_delay,
                jitter=jitter,
                exceptions=exceptions,
                **kwargs,
            )

        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator

