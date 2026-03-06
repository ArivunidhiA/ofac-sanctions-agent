import json
from typing import Any, Dict, List

import pytest

from ofac_sanctions_agent import agent as agent_mod
from ofac_sanctions_agent.config import AgentConfig, SearchSettings
from ofac_sanctions_agent.retry import RetryExhausted, retry_with_backoff


class FakeElement:
    async def click(self) -> None:
        return None

    async def is_visible(self) -> bool:
        return True


class FakeBrowserContext:
    def __init__(self, page: Any) -> None:
        self._page = page

    async def new_context(self, **_: Any) -> "FakeBrowserContext":
        return self

    async def new_page(self) -> Any:
        return self._page

    async def add_init_script(self, *_: Any, **__: Any) -> None:
        return None

    async def close(self) -> None:
        return None


class FakeBrowser:
    def __init__(self, page: Any) -> None:
        self._ctx = FakeBrowserContext(page)

    async def new_context(self, **_: Any) -> FakeBrowserContext:
        return self._ctx

    async def close(self) -> None:
        return None


class FakePlaywright:
    def __init__(self, page: Any) -> None:
        self._browser = FakeBrowser(page)

    @property
    def chromium(self) -> Any:
        class _Chromium:
            def __init__(self, browser: FakeBrowser) -> None:
                self._browser = browser

            async def launch(self, **_: Any) -> FakeBrowser:
                return self._browser

        return _Chromium(self._browser)


class FakeAsyncPlaywrightContext:
    def __init__(self, page: Any) -> None:
        self._playwright = FakePlaywright(page)

    async def __aenter__(self) -> FakePlaywright:
        return self._playwright

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None


class FakePage:
    def __init__(self) -> None:
        self.selector_calls: List[str] = []

    # Methods touched by _find_element / run_agent (but mostly no-op for tests)
    async def wait_for_selector(self, sel: str, **_: Any) -> FakeElement:
        self.selector_calls.append(sel)
        # Default: always succeed
        return FakeElement()

    async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        return None

    async def query_selector(self, *_: Any, **__: Any) -> None:
        return None

    async def goto(self, *_: Any, **__: Any) -> None:
        return None

    async def click(self, *_: Any, **__: Any) -> None:
        return None

    async def type(self, *_: Any, **__: Any) -> None:
        return None

    @property
    def keyboard(self) -> Any:
        class _Keyboard:
            async def press(self, *_: Any, **__: Any) -> None:
                return None

        return _Keyboard()

    def set_default_timeout(self, *_: Any, **__: Any) -> None:
        return None

    def set_default_navigation_timeout(self, *_: Any, **__: Any) -> None:
        return None

    def on(self, *_: Any, **__: Any) -> None:
        return None


@pytest.fixture
def fake_page() -> FakePage:
    return FakePage()


@pytest.fixture
def patch_playwright(monkeypatch: pytest.MonkeyPatch, fake_page: FakePage) -> None:
    def _fake_async_playwright() -> FakeAsyncPlaywrightContext:
        return FakeAsyncPlaywrightContext(fake_page)

    monkeypatch.setattr(agent_mod, "async_playwright", _fake_async_playwright)


@pytest.fixture
def simple_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Dict[str, Any]:
    entities = [
        {"id": f"T00{i}", "name": f"ENTITY-{i}", "notes": ""} for i in range(1, 9)
    ]
    cfg = AgentConfig(
        entities=entities,
        search_settings=SearchSettings(
            score_threshold=0,
            max_results_per_entity=50,
            search_type="name",
        ),
    )

    monkeypatch.setattr(agent_mod, "CONFIG_PATH", tmp_path / "targets.json", raising=False)
    monkeypatch.setattr(agent_mod, "OUTPUT_PATH", tmp_path / "results.json", raising=False)
    monkeypatch.setattr(agent_mod, "LOG_PATH", tmp_path / "agent.log", raising=False)

    def _fake_load_config(_path=None) -> AgentConfig:
        return cfg

    monkeypatch.setattr(agent_mod, "load_config", _fake_load_config)
    return {"entities": entities}


@pytest.mark.asyncio
async def test_agent_recovers_from_selector_drift(
    fake_page: FakePage, patch_playwright: None, simple_config: Dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")

    # Simulate primary selector failing and secondary succeeding on second call
    selectors = ["#primary", "#secondary"]
    call_counts = {"calls": 0}

    async def drift_wait_for_selector(sel: str, **_: Any) -> FakeElement:
        call_counts["calls"] += 1
        fake_page.selector_calls.append(sel)
        if sel == "#primary":
            # first selector drifts and fails
            raise Exception("selector drift")
        return FakeElement()

    fake_page.wait_for_selector = drift_wait_for_selector  # type: ignore[assignment]

    el = await agent_mod._find_element(fake_page, selectors, timeout=100)
    assert isinstance(el, FakeElement)
    # Ensure fallback selector was attempted
    assert "#secondary" in fake_page.selector_calls
    # And debug log captured the matched fallback selector
    assert any("Selector matched: #secondary" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_retry_exhaustion_produces_structured_error() -> None:
    async def always_timeout(entity_id: str) -> None:
        raise TimeoutError(f"timeout for {entity_id}")

    entity_id = "ENTITY-XYZ"
    with pytest.raises(RetryExhausted) as excinfo:
        await retry_with_backoff(
            always_timeout,
            entity_id,
            max_retries=2,
            base_delay=0.0,
            max_delay=0.0,
            jitter=False,
            exceptions=(TimeoutError,),
        )

    exc = excinfo.value
    assert exc.attempts == 3
    assert isinstance(exc.last_exception, TimeoutError)
    assert entity_id in str(exc.last_exception)


@pytest.mark.asyncio
async def test_partial_run_saves_completed_results(
    monkeypatch: pytest.MonkeyPatch,
    fake_page: FakePage,
    patch_playwright: None,
    simple_config: Dict[str, Any],
    tmp_path: Any,
) -> None:
    output_path = tmp_path / "results.json"
    monkeypatch.setattr(agent_mod, "OUTPUT_PATH", output_path, raising=False)

    processed: List[Dict[str, Any]] = []

    async def fake_search_with_retry(page, entity, **kwargs):  # type: ignore[unused-argument]
        # Simulate successful searches for first 4, then a hard failure
        if len(processed) >= 4:
            raise RuntimeError("simulated kill")
        result = {
            "entity_id": entity.get("id"),
            "query": entity.get("name"),
            "notes": entity.get("notes", ""),
            "status": "ok",
            "hit_count": 0,
            "hits": [],
            "error": "",
            "searched_at": "2024-01-01T00:00:00Z",
            "duration_s": 0.1,
        }
        processed.append(result)
        return result

    monkeypatch.setattr(agent_mod, "_search_with_retry", fake_search_with_retry)

    with pytest.raises(RuntimeError):
        await agent_mod.run_agent(headless=True)

    # After failure, partial results should have been flushed to disk
    assert output_path.exists()
    data = json.loads(output_path.read_text(encoding="utf-8"))
    results = data["results"]
    assert len(results) == 4
    # Basic schema checks
    for r in results:
        assert {"entity_id", "query", "status", "hit_count", "hits"} <= r.keys()


@pytest.mark.asyncio
async def test_captcha_detection_skips_gracefully(
    monkeypatch: pytest.MonkeyPatch,
    fake_page: FakePage,
    patch_playwright: None,
    simple_config: Dict[str, Any],
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    output_path = tmp_path / "results.json"
    monkeypatch.setattr(agent_mod, "OUTPUT_PATH", output_path, raising=False)

    entities = simple_config["entities"]
    # Restrict to two entities for this test
    cfg = AgentConfig(
        entities=entities[:2],
        search_settings=SearchSettings(
            score_threshold=0,
            max_results_per_entity=50,
            search_type="name",
        ),
    )

    def _fake_load_config(_path=None) -> AgentConfig:
        return cfg

    monkeypatch.setattr(agent_mod, "load_config", _fake_load_config)

    call_index = {"i": 0}

    async def fake_has_captcha(page) -> bool:  # type: ignore[unused-argument]
        # First entity: CAPTCHA present, second: clean
        call_index["i"] += 1
        return call_index["i"] == 1

    async def fake_has_results(page) -> bool:  # type: ignore[unused-argument]
        return True

    async def fake_parse_results_table(page) -> List[Dict[str, Any]]:  # type: ignore[unused-argument]
        return [
            {
                "name": "HIT",
                "type": "Individual",
                "program": "SDGT",
                "list": "SDN",
                "score": 100,
                "remarks": "",
                "raw_row": 0,
            }
        ]

    async def fake_get_result_count_text(page) -> str:  # type: ignore[unused-argument]
        return "Showing 1 result"

    # Short-circuit navigation / search to avoid relying on DOM
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_mod, "has_captcha", fake_has_captcha)
    monkeypatch.setattr(agent_mod, "has_results", fake_has_results)
    monkeypatch.setattr(agent_mod, "parse_results_table", fake_parse_results_table)
    monkeypatch.setattr(agent_mod, "get_result_count_text", fake_get_result_count_text)
    monkeypatch.setattr(agent_mod, "_navigate_to_search", noop)
    monkeypatch.setattr(agent_mod, "_perform_search", noop)

    caplog.set_level("INFO")
    result = await agent_mod.run_agent(headless=True)

    assert output_path.exists()
    data = json.loads(output_path.read_text(encoding="utf-8"))
    results = data["results"]
    assert len(results) == 2

    # First entity should be marked as CAPTCHA and skipped
    assert results[0]["status"] == "captcha"
    assert "CAPTCHA" in results[0]["error"]

    # Second entity should be processed normally
    assert results[1]["status"] == "ok"
    assert results[1]["hit_count"] == 1

    # Verify logging mentions CAPTCHA detection
    assert any("CAPTCHA detected" in rec.message for rec in caplog.records)

