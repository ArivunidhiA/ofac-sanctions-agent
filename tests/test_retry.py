import asyncio

import pytest

from ofac_sanctions_agent.retry import RetryExhausted, retry_with_backoff


async def _flaky(counter: dict, fail_times: int) -> str:
    counter["calls"] += 1
    if counter["calls"] <= fail_times:
        raise TimeoutError("boom")
    return "ok"


def test_retry_succeeds_before_max_retries() -> None:
    counter = {"calls": 0}

    result = asyncio.run(
        retry_with_backoff(
            _flaky,
            counter,
            1,  # fail once, then succeed
            max_retries=3,
            base_delay=0.001,
            max_delay=0.001,
            jitter=False,
            exceptions=(TimeoutError,),
        )
    )

    assert result == "ok"
    # One failure + one success
    assert counter["calls"] == 2


def test_retry_raises_after_exhaustion() -> None:
    counter = {"calls": 0}

    with pytest.raises(RetryExhausted) as excinfo:
        asyncio.run(
            retry_with_backoff(
                _flaky,
                counter,
                5,  # always fail within retry budget
                max_retries=2,
                base_delay=0.001,
                max_delay=0.001,
                jitter=False,
                exceptions=(TimeoutError,),
            )
        )

    # max_retries + first attempt
    assert excinfo.value.attempts == 3
    assert counter["calls"] == 3

