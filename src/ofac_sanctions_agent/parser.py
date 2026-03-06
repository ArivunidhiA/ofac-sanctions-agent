"""
parser.py — DOM parsing utilities for OFAC SDN search result pages.

The OFAC SDN search (sanctionssearch.ofac.treas.gov) is an ASP.NET WebForms
application.  Results are rendered in a GridView whose outer table has the id
``ctl00_MainContent_gvSearchResults``.  Each data row contains cells for:

    Col 0 — Name
    Col 1 — Type  (Individual / Entity / Vessel / Aircraft)
    Col 2 — Program (sanction program code, e.g. SDGT, NPWMD)
    Col 3 — List   (SDN, CONS, etc.)
    Col 4 — Score  (integer %)

Detail remarks are available only after clicking a row, which opens an inline
detail panel or navigates to a detail page.  The parser handles both cases.

All functions accept a Playwright ``Page`` object and return plain dicts /
lists so callers remain decoupled from the Playwright API.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from playwright.async_api import Page


logger = logging.getLogger(__name__)


# CSS selectors tried first (fastest); XPath fallbacks used if all CSS selectors fail.
_RESULTS_TABLE_SELECTORS_CSS = [
    "#ctl00_MainContent_gvSearchResults",
    "table#gvSearchResults",
    "table[id*='gvSearchResults']",
    "table[id*='GridView']",
    "table.resultsTable",
]

_RESULTS_TABLE_SELECTORS_XPATH = [
    "//table[contains(@id,'gvSearchResults')]",
    "//table[contains(@id,'GridView')]",
    "//table[contains(@class,'resultsTable')]",
    "//table[.//tr[contains(@class,'gridRow') or contains(@class,'dataRow') or contains(@class,'GridRow')]]",
]

# Keep the old name as an alias so existing callers continue to work.
_RESULTS_TABLE_SELECTORS = _RESULTS_TABLE_SELECTORS_CSS

_EMPTY_RESULT_PATTERNS = [
    r"no records found",
    r"no results found",
    r"0 results",
    r"returned 0",
    r"no match",
]

_DETAIL_PANEL_SELECTORS = [
    "#ctl00_MainContent_lblRemarks",
    "[id*='lblRemarks']",
    ".remarks",
    "#ctl00_MainContent_pnlDetails",
    "[id*='pnlDetails']",
]

_NEXT_PAGE_SELECTORS = [
    "a[id*='lnkNext']",
    "td.gridPager a:text('>')",
    "td.gridPager a:text('Next')",
    "tr.gridPager a:last-child",
]

_CAPTCHA_KEYWORDS = [
    "captcha",
    "recaptcha",
    "are you human",
    "verify you are",
    "robot",
    "challenge",
]


async def has_captcha(page: Page) -> bool:
    """Return True when the current page content looks like a CAPTCHA challenge."""
    try:
        content = (await page.content()).lower()
        return any(kw in content for kw in _CAPTCHA_KEYWORDS)
    except Exception as exc:
        logger.debug("has_captcha check failed: %s", exc)
        return False


async def has_results(page: Page) -> bool:
    """
    Return True when a non-empty results table is present.
    Returns False for empty-result pages and pages with errors.
    """
    try:
        content = (await page.content()).lower()
        for pattern in _EMPTY_RESULT_PATTERNS:
            if re.search(pattern, content):
                return False
    except Exception:
        pass

    table = await _find_results_table(page)
    if table is None:
        return False

    try:
        rows = await table.query_selector_all("tr")
        data_rows = [r for r in rows[1:] if await _is_data_row(r)]
        return len(data_rows) > 0
    except Exception as exc:
        logger.debug("has_results row count failed: %s", exc)
        return False


async def parse_results_table(page: Page) -> list[dict[str, Any]]:
    """
    Parse all rows from the SDN results table and return a list of records.

    Each record is::

        {
            "name":     str,
            "type":     str,
            "program":  str,
            "list":     str,
            "score":    int | None,
            "remarks":  str,
            "raw_row":  int,
        }

    Pagination is followed automatically; all pages are combined.
    """
    all_records: list[dict[str, Any]] = []
    page_number = 1

    while True:
        logger.debug("Parsing results table — page %d", page_number)
        records = await _parse_current_page(page)
        all_records.extend(records)

        next_link = await _find_next_page_link(page)
        if next_link is None:
            break

        try:
            logger.debug("Navigating to results page %d", page_number + 1)
            await next_link.click()
            await page.wait_for_load_state("networkidle", timeout=15_000)
            page_number += 1
        except Exception as exc:
            logger.warning(
                "Pagination click failed on page %d: %s — stopping",
                page_number,
                exc,
            )
            break

    logger.info("Parsed %d total records across %d page(s)", len(all_records), page_number)
    return all_records


async def fetch_row_remarks(page: Page, row_index: int) -> str:
    """
    Click table row ``row_index`` to reveal its inline detail / remarks panel
    and return the remarks text.  Returns empty string on any error.
    """
    try:
        table = await _find_results_table(page)
        if table is None:
            return ""

        rows = await table.query_selector_all("tr")
        target_index = row_index + 1
        if target_index >= len(rows):
            logger.debug("Row %d out of range (table has %d rows)", row_index, len(rows) - 1)
            return ""

        row = rows[target_index]
        link = await row.query_selector("a")
        clickable = link if link else row
        await clickable.click()
        await page.wait_for_load_state("networkidle", timeout=10_000)

        for sel in _DETAIL_PANEL_SELECTORS:
            panel = await page.query_selector(sel)
            if panel:
                text = (await panel.inner_text()).strip()
                if text:
                    return text

        detail_text = await _scrape_detail_text(page)
        return detail_text

    except Exception as exc:
        logger.debug("fetch_row_remarks(%d) failed: %s", row_index, exc)
        return ""


async def get_result_count_text(page: Page) -> str:
    """
    Return the 'Showing X of Y results' summary text if present, else ''.
    """
    try:
        selectors = [
            "[id*='lblResultCount']",
            "[id*='lblCount']",
            ".resultCount",
            "span:text-matches('result', 'i')",
        ]
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
    except Exception:
        pass
    return ""


async def _find_results_table(page: Page):
    """
    Locate the SDN results table using a CSS → XPath fallback chain.

    Each successful match is logged at DEBUG so DOM drift (e.g. ASP.NET
    ViewState renaming the control) is captured in ``agent.log`` without
    requiring a full re-run.
    """
    # --- CSS selectors (preferred: fast, stable) ---
    for sel in _RESULTS_TABLE_SELECTORS_CSS:
        try:
            el = await page.query_selector(sel)
            if el:
                logger.debug("Results table matched via CSS selector: %r", sel)
                return el
        except Exception:
            continue

    # --- XPath fallbacks (used if CSS fails — log at INFO for drift alerting) ---
    for xpath in _RESULTS_TABLE_SELECTORS_XPATH:
        try:
            el = await page.query_selector(f"xpath={xpath}")
            if el:
                logger.info(
                    "Results table matched via XPath fallback: %r "
                    "(all CSS selectors failed — possible DOM drift)",
                    xpath,
                )
                return el
        except Exception:
            continue

    logger.warning(
        "Results table NOT found — exhausted %d CSS and %d XPath selectors; "
        "the page structure may have changed",
        len(_RESULTS_TABLE_SELECTORS_CSS),
        len(_RESULTS_TABLE_SELECTORS_XPATH),
    )
    return None


async def _is_data_row(row) -> bool:
    try:
        cells = await row.query_selector_all("td")
        return len(cells) >= 2
    except Exception:
        return False


async def _parse_current_page(page: Page) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    table = await _find_results_table(page)
    if table is None:
        logger.debug("_parse_current_page: no results table found")
        return records

    rows = await table.query_selector_all("tr")
    data_rows = []
    for i, row in enumerate(rows):
        if await _is_data_row(row):
            data_rows.append((i - 1, row))

    if not data_rows:
        logger.debug("_parse_current_page: table found but no data rows")
        return records

    for row_idx, row in data_rows:
        try:
            record = await _parse_row(row, row_idx)
            if record:
                records.append(record)
        except Exception as exc:
            logger.warning("Failed to parse row %d: %s", row_idx, exc)

    return records


async def _parse_row(row, row_idx: int) -> dict[str, Any] | None:
    cells = await row.query_selector_all("td")
    if len(cells) == 0:
        return None

    cell_texts: list[str] = []
    for cell in cells:
        try:
            text = (await cell.inner_text()).strip()
            text = re.sub(r"\s+", " ", text)
        except Exception:
            text = ""
        cell_texts.append(text)

    if not any(cell_texts):
        return None

    name = cell_texts[0] if len(cell_texts) > 0 else ""
    typ = cell_texts[1] if len(cell_texts) > 1 else ""
    program = cell_texts[2] if len(cell_texts) > 2 else ""
    lst = cell_texts[3] if len(cell_texts) > 3 else ""
    score_raw = cell_texts[4] if len(cell_texts) > 4 else ""

    score: int | None = None
    if score_raw:
        m = re.search(r"\d+", score_raw)
        if m:
            try:
                score = int(m.group())
            except ValueError:
                pass

    if not name:
        return None

    return {
        "name": name,
        "type": typ,
        "program": program,
        "list": lst,
        "score": score,
        "remarks": "",
        "raw_row": row_idx,
    }


async def _find_next_page_link(page: Page):
    for sel in _NEXT_PAGE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                logger.debug("Next-page link matched via selector: %r", sel)
                return el
        except Exception:
            continue
    return None


async def _scrape_detail_text(page: Page) -> str:
    try:
        candidates = await page.query_selector_all(
            "div.detailPanel, div.entityDetails, #detailContent, #pnlDetails, .entityDetail"
        )
        for el in candidates:
            text = (await el.inner_text()).strip()
            if len(text) > 10:
                return text
    except Exception:
        pass
    return ""

