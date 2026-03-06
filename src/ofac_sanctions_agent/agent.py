"""
agent.py — Async Playwright agent for OFAC SDN sanctions screening.

Workflow for each entity in ``config/targets.json``:
  1. Navigate to https://sanctionssearch.ofac.treas.gov/
  2. Fill in the entity name and submit the search form.
  3. Wait for results; detect CAPTCHA / empty / timeout conditions.
  4. Parse the results table (name, type, program, list, score, remarks).
  5. Attempt to fetch per-row remarks by clicking each row.
  6. Accumulate results; never crash — log failures and continue.

Outputs:
  - output/results.json  : structured JSON with all findings
  - logs/agent.log       : timestamped log of every action
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

from .config import CONFIG_PATH, LOG_PATH, OUTPUT_PATH, AgentConfig, load_config
from .logging_config import configure_logging
from .parser import (
    fetch_row_remarks,
    get_result_count_text,
    has_captcha,
    has_results,
    parse_results_table,
)
from .retry import RETRY_POLICIES, ErrorKind, classify_error


log = configure_logging(LOG_PATH)


OFAC_URL = "https://sanctionssearch.ofac.treas.gov/"
AGENT_VERSION = "0.2.0"

_INPUT_SELECTORS = [
    "#ctl00_MainContent_txtLastName",
    "input[name='ctl00$MainContent$txtLastName']",
    "input#txtLastName",
    "input[name='txtLastName']",
    "input[type='text']",
]

_BUTTON_SELECTORS = [
    "#ctl00_MainContent_btnSearch",
    "input[name='ctl00$MainContent$btnSearch']",
    "input#btnSearch",
    "input[value='Search']",
    "button[type='submit']",
    "input[type='submit']",
]

_LOADING_SELECTORS = [
    "#ctl00_MainContent_UpdateProgress1",
    "#UpdateProgress1",
    ".loading",
    "[id*='UpdateProgress']",
]


async def _find_element(page: Page, selectors: List[str], timeout: int = 5_000):
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout, state="visible")
            if el:
                log.debug("Selector matched: %s", sel)
                return el
        except (PWTimeoutError, Exception):
            continue
    return None


async def _clear_and_type(page: Page, selector: str, text: str) -> None:
    await page.click(selector)
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Delete")
    await page.type(selector, text, delay=50)


async def _wait_for_page_idle(page: Page, timeout: int = 20_000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeoutError:
        log.debug("networkidle timed out — continuing anyway")

    for sel in _LOADING_SELECTORS:
        try:
            spinner = await page.query_selector(sel)
            if spinner and await spinner.is_visible():
                await page.wait_for_selector(sel, state="hidden", timeout=15_000)
                break
        except Exception:
            continue


async def _screenshot_on_failure(
    page: Page,
    entity_name: str,
    reason: str,
) -> Optional[str]:
    """
    Capture a full-page PNG to ``logs/`` when an extraction step fails.

    File name format: ``failure_<entity>_<timestamp>.png``

    Returns the absolute path string on success, or ``None`` if the screenshot
    itself fails (e.g. browser already closed).
    """
    try:
        safe_name = re.sub(r"[^\w\-]", "_", entity_name)[:40].strip("_")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = LOG_PATH.parent / f"failure_{safe_name}_{ts}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True)
        log.info("Failure screenshot saved: %s  (reason: %s)", path, reason)
        return str(path)
    except Exception as exc:
        log.debug("Screenshot capture failed: %s", exc)
        return None


async def _navigate_to_search(page: Page) -> None:
    log.info("Navigating to %s", OFAC_URL)
    await page.goto(OFAC_URL, wait_until="domcontentloaded", timeout=30_000)
    await _wait_for_page_idle(page, timeout=15_000)

    input_el = await _find_element(page, _INPUT_SELECTORS, timeout=10_000)
    if input_el is None:
        raise RuntimeError("Search input field not found on OFAC page")
    log.info("OFAC search page loaded and input field confirmed")


async def _perform_search(page: Page, entity_name: str) -> None:
    log.info("Searching for: %r", entity_name)

    input_el = await _find_element(page, _INPUT_SELECTORS, timeout=10_000)
    if input_el is None:
        raise RuntimeError(f"Search input not found while searching for {entity_name!r}")

    for sel in _INPUT_SELECTORS:
        try:
            await page.wait_for_selector(sel, state="visible", timeout=3_000)
            await _clear_and_type(page, sel, entity_name)
            log.debug("Typed %r into field %s", entity_name, sel)
            break
        except Exception:
            continue
    else:
        raise RuntimeError("Could not type into any known search field")

    btn = await _find_element(page, _BUTTON_SELECTORS, timeout=10_000)
    if btn is None:
        raise RuntimeError("Search button not found")

    log.debug("Clicking search button")
    await btn.click()
    await _wait_for_page_idle(page, timeout=25_000)


def _make_entity_result(
    entity: Dict[str, Any],
    status: str,
    hits: List[Dict[str, Any]],
    error: str = "",
    duration_s: float = 0.0,
    failure_reason: Optional[Dict[str, Any]] = None,
    screenshot_path: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "entity_id": entity.get("id", ""),
        "query": entity.get("name", ""),
        "notes": entity.get("notes", ""),
        "status": status,
        "hit_count": len(hits),
        "hits": hits,
        "error": error,
        # Structured failure taxonomy — populated for status "error" and "captcha".
        "failure_reason": failure_reason,
        # Path to failure screenshot PNG (logs/), or null if none was captured.
        "screenshot_path": screenshot_path,
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(duration_s, 3),
    }


async def _search_single_entity(
    page: Page,
    entity: Dict[str, Any],
    fetch_remarks_flag: bool = True,
    remarks_timeout: float = 8.0,
) -> Dict[str, Any]:
    name = entity.get("name", "")
    start = time.monotonic()

    log.info("=== Starting search for entity: %s ===", name)

    try:
        await _navigate_to_search(page)

        if await has_captcha(page):
            log.warning("CAPTCHA detected on landing page for %r — skipping", name)
            return _make_entity_result(
                entity,
                "captcha",
                [],
                error="CAPTCHA on landing page",
                duration_s=time.monotonic() - start,
            )

        await _perform_search(page, name)

        if await has_captcha(page):
            log.warning("CAPTCHA detected after search for %r — skipping", name)
            shot = await _screenshot_on_failure(page, name, "captcha-post-search")
            return _make_entity_result(
                entity,
                "captcha",
                [],
                error="CAPTCHA after search submission",
                duration_s=time.monotonic() - start,
                failure_reason={"kind": ErrorKind.CAPTCHA.value, "message": "CAPTCHA detected after search submission"},
                screenshot_path=shot,
            )

        result_summary = await get_result_count_text(page)
        if result_summary:
            log.info("Result summary text: %s", result_summary)

        if not await has_results(page):
            log.info("No results found for %r", name)
            return _make_entity_result(
                entity,
                "empty",
                [],
                duration_s=time.monotonic() - start,
            )

        hits = await parse_results_table(page)
        log.info("Parsed %d raw hit(s) for %r", len(hits), name)

        if fetch_remarks_flag and hits:
            log.debug("Fetching remarks for %d hit(s) …", len(hits))
            for hit in hits:
                row_idx = hit.get("raw_row", -1)
                if row_idx < 0:
                    continue
                try:
                    remarks = await asyncio.wait_for(
                        fetch_row_remarks(page, row_idx),
                        timeout=remarks_timeout,
                    )
                    if remarks:
                        hit["remarks"] = remarks
                        log.debug("Row %d remarks: %.80s …", row_idx, remarks)
                except asyncio.TimeoutError:
                    log.debug("Remarks timeout for row %d", row_idx)
                except Exception as exc:
                    log.debug("Remarks fetch error for row %d: %s", row_idx, exc)

        return _make_entity_result(
            entity,
            "ok",
            hits,
            duration_s=time.monotonic() - start,
        )

    except PWTimeoutError as exc:
        log.error("Playwright timeout searching for %r: %s", name, exc)
        shot = await _screenshot_on_failure(page, name, "timeout")
        return _make_entity_result(
            entity,
            "error",
            [],
            error=f"TimeoutError: {exc}",
            duration_s=time.monotonic() - start,
            failure_reason={"kind": ErrorKind.TIMEOUT.value, "message": str(exc)},
            screenshot_path=shot,
        )
    except Exception as exc:
        kind = classify_error(exc)
        log.error("Unexpected error searching for %r: %s", name, exc, exc_info=True)
        shot = await _screenshot_on_failure(page, name, kind.value.lower())
        return _make_entity_result(
            entity,
            "error",
            [],
            error=f"{type(exc).__name__}: {exc}",
            duration_s=time.monotonic() - start,
            failure_reason={"kind": kind.value, "message": str(exc)},
            screenshot_path=shot,
        )


async def _search_with_retry(
    page: Page,
    entity: Dict[str, Any],
    fetch_remarks_flag: bool = True,
    max_retries: int = 2,
    base_delay: float = 2.0,
) -> Dict[str, Any]:
    """
    Run ``_search_single_entity`` with adaptive retry logic based on
    :func:`.classify_error`.

    * CAPTCHA results are never retried.
    * TIMEOUT / NETWORK errors use the per-kind policy from :data:`RETRY_POLICIES`.
    * SELECTOR / UNKNOWN errors get at most one retry.
    * Non-error statuses (ok, empty) are returned immediately.
    """
    name = entity.get("name", "")

    for attempt in range(max_retries + 1):
        result = await _search_single_entity(
            page, entity, fetch_remarks_flag=fetch_remarks_flag
        )

        # Non-error statuses always exit the loop immediately.
        if result["status"] != "error":
            return result

        error_msg = result.get("error", "")
        kind = classify_error(RuntimeError(error_msg))
        policy = RETRY_POLICIES[kind]

        # Ensure failure_reason reflects the taxonomy kind (may already be set
        # by _search_single_entity, but override kind for consistency).
        result["failure_reason"] = {"kind": kind.value, "message": error_msg}

        # No retries allowed for this kind, or we have exhausted our budget.
        if policy["max_retries"] == 0:
            log.info("[taxonomy] %s — no retries for %r", kind.value, name)
            return result

        if attempt >= max_retries:
            log.error("All %d attempt(s) exhausted for %r", max_retries + 1, name)
            return result

        # Compute jittered delay from the kind-specific policy.
        raw_delay = min(policy["base_delay"] * (2 ** attempt), policy["max_delay"])
        delay = raw_delay * (0.5 + random.random())
        log.warning(
            "[retry] %s for %r — retry %d/%d in %.2fs",
            kind.value, name, attempt + 1, max_retries, delay,
        )
        await asyncio.sleep(delay)

    # Should be unreachable, but guard against off-by-one edge cases.
    return _make_entity_result(entity, "error", [], error="retry loop exited unexpectedly")


def _save_results(run_results: Dict[str, Any]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(run_results, fh, indent=2, ensure_ascii=False)
    log.info("Results saved to %s", OUTPUT_PATH)


def _build_run_envelope(
    entity_results: List[Dict[str, Any]],
    config: AgentConfig,
    started_at: str,
    run_total_duration_s: float = 0.0,
) -> Dict[str, Any]:
    total = len(entity_results)
    ok = sum(1 for r in entity_results if r["status"] == "ok")
    empty = sum(1 for r in entity_results if r["status"] == "empty")
    captcha = sum(1 for r in entity_results if r["status"] == "captcha")
    errors = sum(1 for r in entity_results if r["status"] == "error")
    hits = sum(r["hit_count"] for r in entity_results)

    search_settings = {
        "score_threshold": config.search_settings.score_threshold,
        "max_results_per_entity": config.search_settings.max_results_per_entity,
        "search_type": config.search_settings.search_type,
    }

    return {
        "metadata": {
            "agent_version": AGENT_VERSION,
            "run_started_at": started_at,
            "run_finished_at": datetime.now(timezone.utc).isoformat(),
            "run_total_duration_s": round(run_total_duration_s, 3),
            "source_url": OFAC_URL,
            "total_entities": total,
            "ok": ok,
            "empty": empty,
            "captcha_skipped": captcha,
            "errors": errors,
            "total_hits": hits,
        },
        "search_settings": search_settings,
        "results": entity_results,
    }


async def run_dry_run() -> int:
    """
    Validate configuration and test live connectivity to the OFAC portal.

    Performs two checks:

    1. **Config validation** — loads ``config/targets.json`` and verifies it
       parses without errors and contains at least one entity.
    2. **Connectivity check** — launches a headless browser, navigates to the
       OFAC search URL, and confirms the search input field is reachable.

    Prints a clear PASS / FAIL summary and returns an exit code (0 = pass,
    1 = fail) suitable for use in CI pre-flight scripts.
    """
    log.info("========== DRY RUN — pre-deployment check ==========")
    exit_code = 0

    # --- 1. Config validation -------------------------------------------------
    log.info("[dry-run] Validating config: %s", CONFIG_PATH)
    try:
        config = load_config(CONFIG_PATH)
        log.info("[dry-run] Config OK — %d entity/entities loaded", len(config.entities))
        if not config.entities:
            log.warning("[dry-run] No entities defined — nothing would be searched")
    except FileNotFoundError:
        log.error("[dry-run] Config file not found: %s", CONFIG_PATH)
        return 1
    except Exception as exc:
        log.error("[dry-run] Config validation failed: %s", exc)
        return 1

    # --- 2. Connectivity check ------------------------------------------------
    log.info("[dry-run] Testing connectivity to %s", OFAC_URL)
    try:
        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(
                headless=True,
                slow_mo=0,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page: Page = await context.new_page()
            page.set_default_timeout(30_000)

            await page.goto(OFAC_URL, wait_until="domcontentloaded", timeout=30_000)
            await _wait_for_page_idle(page, timeout=15_000)

            input_el = await _find_element(page, _INPUT_SELECTORS, timeout=10_000)
            if input_el is None:
                log.error(
                    "[dry-run] OFAC portal is reachable but search input not found "
                    "— possible DOM change or blocked access"
                )
                exit_code = 1
            else:
                log.info("[dry-run] OFAC portal connectivity OK — search input confirmed")

            await context.close()
            await browser.close()

    except Exception as exc:
        log.error("[dry-run] Connectivity check failed: %s", exc)
        exit_code = 1

    # --- Summary --------------------------------------------------------------
    if exit_code == 0:
        log.info("========== DRY RUN PASSED — system ready for production ==========")
    else:
        log.error("========== DRY RUN FAILED — review errors above before deploying ==========")

    return exit_code


async def run_agent(
    headless: bool = True,
    slow_mo: int = 250,
    fetch_remarks: bool = True,
) -> Dict[str, Any]:
    """
    Main agent coroutine.

    Args:
        headless:       Run browser headlessly (True) or with a visible window.
        slow_mo:        Milliseconds of artificial delay between Playwright actions.
        fetch_remarks:  Whether to click each row to fetch detailed remarks text.
    """
    run_start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    log.info("========== OFAC SDN Sanctions Agent v%s starting ==========", AGENT_VERSION)
    log.info("Config:  %s", CONFIG_PATH)
    log.info("Output:  %s", OUTPUT_PATH)
    log.info("Log:     %s", LOG_PATH)
    log.info("Headless: %s  |  SlowMo: %dms", headless, slow_mo)

    config: AgentConfig = load_config(CONFIG_PATH)
    if not config.entities:
        log.warning("No entities found in %s — nothing to do", CONFIG_PATH)
        return {}

    log.info("Loaded %d entity/entities to search", len(config.entities))

    entity_results: List[Dict[str, Any]] = []

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=headless,
            slow_mo=slow_mo,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            accept_downloads=False,
        )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page: Page = await context.new_page()

        page.set_default_timeout(30_000)
        page.set_default_navigation_timeout(30_000)

        page.on("console", lambda msg: log.debug("[browser-console] %s", msg.text))
        page.on("pageerror", lambda exc: log.warning("[browser-error] %s", exc))

        log.info("Browser launched — beginning searches")

        for idx, entity in enumerate(config.entities, start=1):
            entity_name = entity.get("name", f"<unnamed-{idx}>")
            log.info(
                "--- [%d/%d] Processing entity: %s ---",
                idx,
                len(config.entities),
                entity_name,
            )

            result = await _search_with_retry(
                page, entity,
                fetch_remarks_flag=fetch_remarks,
                max_retries=2,
                base_delay=2.0,
            )
            entity_results.append(result)

            # Persist partial results so a killed run still leaves structured output
            partial_output = _build_run_envelope(entity_results, config, started_at)
            _save_results(partial_output)

            log.info(
                "Entity %r => status=%s, hits=%d, duration=%.2fs",
                entity_name,
                result["status"],
                result["hit_count"],
                result["duration_s"],
            )

            if idx < len(config.entities):
                await asyncio.sleep(1.5)

        await context.close()
        await browser.close()

    run_output = _build_run_envelope(
        entity_results, config, started_at,
        run_total_duration_s=time.monotonic() - run_start,
    )
    _save_results(run_output)

    meta = run_output["metadata"]
    log.info("========== Agent run complete ==========")
    log.info(
        "Summary: %d searched | %d ok | %d empty | %d captcha | %d errors | %d total hits",
        meta["total_entities"],
        meta["ok"],
        meta["empty"],
        meta["captcha_skipped"],
        meta["errors"],
        meta["total_hits"],
    )

    return run_output

