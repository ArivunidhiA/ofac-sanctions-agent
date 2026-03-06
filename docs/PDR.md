## OFAC Sanctions Agent — MVP Product & Design Requirements (PDR)

### 1. Overview

**Product name**: OFAC Sanctions Agent  
**Owner**: (TBD)  
**Status**: MVP-spec

The OFAC Sanctions Agent is a headless, scriptable tool that screens one or more
entities against the U.S. Treasury OFAC SDN sanctions list
(`https://sanctionssearch.ofac.treas.gov/`) using an automated browser.

Given a list of entities (persons, organizations, vessels, etc.) described in a
configuration file, the agent:

- Submits each entity to the OFAC SDN search form.
- Parses the returned results table (name, type, program, list, score).
- Optionally drills into each row to collect detailed remarks.
- Produces structured JSON output suitable for downstream ingestion
  (e.g. case management, risk engines, manual review workflows).
- Logs a complete, timestamped trace of actions and errors for auditing.

The MVP is **CLI-first** and runs as a batch job. It is designed to be easily
embedded in a larger system (e.g. invoked from an orchestrator, API worker, or
job runner).


### 2. Goals & Non-goals

#### 2.1 Goals

- **G1 — Deterministic batch screening**: Given an input list of entities in
  `config/targets.json`, execute a full search for each entity and write a
  stable, machine-readable results file to `output/results.json`.
- **G2 — Robustness over perfection**: Never crash the process due to a single
  failing entity. Capture errors per-entity, log them, and continue.
- **G3 — High observability**: Provide detailed logs (`logs/agent.log`) and
  clear per-run metadata (timestamps, counts, status breakdown) in the results
  envelope.
- **G4 — Extensibility & cleanliness**: Ship a package-style architecture
  (`ofac_sanctions_agent`) with clear layering so that future integrations
  (e.g. REST API, job scheduler) can reuse the core logic without rewriting.
- **G5 — Developer ergonomics**: Simple `python -m ofac_sanctions_agent.cli`
  and `python src/agent.py` entrypoints, typed modules, and testable
  components (`parser`, `retry`, `agent` orchestration).

#### 2.2 Non-goals (for MVP)

- **N1 — No UI**: No web UI or desktop UI. CLI only.
- **N2 — No persistence layer**: Beyond JSON output and file-based logs, MVP
  does not integrate with databases or message queues.
- **N3 — No advanced entity matching**: The MVP delegates all fuzzy matching and
  scoring logic to OFAC’s own search; it does not implement custom matching.
- **N4 — No concurrency guarantees**: The MVP processes entities sequentially
  within a single Playwright browser context. Parallelization across workers is
  a post-MVP concern.
- **N5 — No full CAPTCHA automation**: If OFAC presents a CAPTCHA, the agent
  detects it and skips the affected entity (or run segment); it does not attempt
  automated solving.


### 3. User Personas & Primary Use Cases

#### 3.1 Personas

- **Risk / Compliance engineer**
  - Needs a programmable way to enrich internal workflows with sanctions
    screening without building their own scraper.
- **Backend / Platform engineer**
  - Integrates sanctions checks into account opening, payments, or KYC flows.
- **Analyst / Ops**
  - Runs ad-hoc or scheduled batches of names and inspects exported JSON in
    downstream systems.

#### 3.2 Primary use cases

- **U1 — Batch KYC screening**
  - Input: Daily list of customers or counterparties.
  - Output: JSON with hits per entity, including SDN program, list, and score,
    used by internal rule engines or manual review.
- **U2 — Sanctions monitoring regression**
  - Input: Regression list of “known” high-risk entities (e.g. test cases).
  - Output: JSON used to verify the tool still detects known SDN entities.
- **U3 — Ad-hoc investigation**
  - Input: Short list of entities for one-off checks.
  - Output: JSON and logs used by analysts to inspect and document findings.


### 4. Functional Requirements

#### 4.1 Input configuration

- **FR1 — Config source**
  - The agent reads configuration from `config/targets.json` by default.
- **FR2 — Entities list**
  - `targets.json` MUST contain:
    - `entities`: array of objects with:
      - `id` (string, optional but recommended)
      - `name` (string, required)
      - `notes` (string, optional freeform)
    - `search_settings`: optional object with:
      - `score_threshold` (int, default 0)
      - `max_results_per_entity` (int, default 50) — currently advisory; actual
        cap may be implemented in future revisions.
      - `search_type` (string, default `"name"`).
- **FR3 — Validation**
  - If `targets.json` is missing, agent logs a critical error and raises
    `FileNotFoundError`.
  - If `entities` is empty or absent, agent logs a warning and exits cleanly
    without performing any searches.

#### 4.2 Execution and browser behavior

- **FR4 — Browser automation**
  - Use Playwright Chromium with:
    - Configurable **headless** mode (default: headless).
    - `slow_mo` (default: 250ms) for stability.
  - For each entity:
    - Navigate to `https://sanctionssearch.ofac.treas.gov/`.
    - Confirm the presence of the search input field.
    - Type the `name` into the input field using resilient selectors.
    - Click the search button using resilient selectors.
    - Wait for the page/network to become “idle” and for loading indicators to
      disappear (when present).
- **FR5 — CAPTCHA detection**
  - Before and after search submission, check page content for CAPTCHA-like
    keywords.
  - If detected:
    - Log a warning.
    - Mark the entity result as `status = "captcha"`, with an explanatory error
      string.
    - Skip further parsing for that entity.
- **FR6 — Results parsing**
  - Detect whether a non-empty results table is present.
  - If the table is absent or the page clearly signals “no records”:
    - Mark the entity as `status = "empty"` with zero hits.
  - If present:
    - Parse each data row (skipping headers/pagers).
    - Extract: `name`, `type`, `program`, `list`, `score`, and a 0-based
      `raw_row` index.
    - Normalize whitespace; parse `score` as integer when possible.
- **FR7 — Remarks enrichment (optional)**
  - When CLI flag `--no-remarks` is NOT set:
    - For each hit, attempt to click the corresponding row to reveal a detail
      panel or detail page.
    - Extract “remarks” text using robust selectors and fallbacks.
    - Respect a per-row timeout; on timeout or error, log at debug level and
      continue without remarks for that row.
- **FR8 — Retry behavior**
  - For transient failures (e.g. Playwright `TimeoutError`, transient connectivity
    issues), the agent:
    - Retries the per-entity search with exponential backoff and jitter.
    - Uses a maximum of 2 retries (3 total attempts).
  - When retries are exhausted:
    - Mark the entity as `status = "error"`, with `error` field containing
      context (`RetryExhausted: ...`).

#### 4.3 Output format

- **FR9 — Run envelope**
  - The agent writes a single JSON file to `output/results.json` with the shape:
    - `metadata`:
      - `run_started_at` (ISO 8601 UTC timestamp)
      - `run_finished_at` (ISO 8601 UTC timestamp)
      - `source_url` (string, OFAC URL)
      - `total_entities` (int)
      - `ok` (int count)
      - `empty` (int count)
      - `captcha_skipped` (int count)
      - `errors` (int count)
      - `total_hits` (int count)
    - `search_settings`:
      - `score_threshold`, `max_results_per_entity`, `search_type`
      (reflecting the effective configuration).
    - `results`: array of per-entity results.
- **FR10 — Per-entity results**
  - Each entry in `results` MUST contain:
    - `entity_id` (string, from config; empty string if absent)
    - `query` (string, the search name)
    - `notes` (string, freeform from config; may be empty)
    - `status` (one of `"ok"`, `"empty"`, `"captcha"`, `"error"`)
    - `hit_count` (int)
    - `hits` (array of hit records)
    - `error` (string, non-empty only when status is `"error"` or `"captcha"`)
    - `searched_at` (ISO 8601 UTC timestamp)
    - `duration_s` (float, seconds for this entity)
- **FR11 — Hit records**
  - Each hit in `hits` MUST contain:
    - `name`, `type`, `program`, `list` (strings, possibly empty)
    - `score` (int or `null`)
    - `remarks` (string, possibly empty)
    - `raw_row` (int, 0-based index in the table)


### 5. Non-functional Requirements

#### 5.1 Reliability & robustness

- **NFR1 — Per-entity isolation**
  - A failure for one entity (timeouts, DOM changes, CAPTCHAs) MUST NOT abort
    the entire run. The run continues for remaining entities.
- **NFR2 — Error visibility**
  - All unexpected errors are logged with stack traces (at error level).
  - Per-entity errors are reflected in the JSON output, not only logs.

#### 5.2 Performance

- **NFR3 — Scale target (MVP)**
  - MVP target: up to ~100 entities per batch with sequential processing, where
    typical per-entity wall time is seconds to low tens of seconds depending on
    OFAC responsiveness.
- **NFR4 — Timeouts**
  - Use conservative browser timeouts (e.g. 30s navigation, 20s network idle)
    with clear logging when timeouts occur.

#### 5.3 Security & compliance

- **NFR5 — No secrets in repo**
  - The agent MUST NOT embed credentials or secrets; it only accesses a public
    OFAC endpoint.
- **NFR6 — Respectful scraping**
  - Apply minimal slow-down between entity searches (e.g. 1.5s pause) to avoid
    overloading OFAC’s service.
- **NFR7 — Data handling**
  - The agent writes only to local JSON and log files; any persistence or PII
  - handling controls are the responsibility of the embedding system.

#### 5.4 Observability

- **NFR8 — Logging**
  - Logs are written to `logs/agent.log` with:
    - Timestamps
    - Log level
    - Logger name
    - Message text
  - Log levels:
    - INFO: high-level progress, per-entity summaries.
    - DEBUG: detailed step traces, DOM selector matches, remarks fetching.
    - WARNING: retry attempts, CAPTCHAs, partial failures.
    - ERROR: unexpected exceptions, exhausted retries.


### 6. Architecture & Design

#### 6.1 High-level components

- **`ofac_sanctions_agent.config`**
  - Encapsulates path resolution and configuration loading.
  - Exposes:
    - `CONFIG_PATH`, `OUTPUT_PATH`, `LOG_PATH`
    - `load_config()` returning an `AgentConfig` dataclass.
- **`ofac_sanctions_agent.logging_config`**
  - Centralizes logging configuration and returns a named logger
    (`ofac_sanctions_agent`).
- **`ofac_sanctions_agent.parser`**
  - Playwright-agnostic parsing utilities for:
    - Detecting CAPTCHAs
    - Checking for results
    - Parsing the results table & pagination
    - Fetching row-level remarks
    - Extracting result count summaries
- **`ofac_sanctions_agent.retry`**
  - General-purpose async retry with exponential backoff and jitter.
  - Exported primitives:
    - `retry_with_backoff`, `RetryExhausted`, `with_retry`.
- **`ofac_sanctions_agent.agent`**
  - Orchestrates the full run:
    - Sets up browser and context
    - Iterates over entities
    - Coordinates search, parsing, remarks, and retries
    - Builds and saves the run envelope JSON.
- **`ofac_sanctions_agent.cli`**
  - CLI interface:
    - Parses flags: `--visible`, `--slow-mo`, `--no-remarks`.
    - Invokes `run_agent()` with chosen options.

#### 6.2 Layering principles

- **Infrastructure layer**
  - Playwright integration, retry logic, logging configuration, paths.
- **Domain layer**
  - Entity configuration models, parser output contracts, run-envelope shape.
- **Application layer**
  - Agent orchestration, CLI, external interfaces (future API, schedulers).

#### 6.3 Module boundaries

- `parser` must not know about configuration or file paths.
- `retry` must not depend on Playwright (generic async).
- `agent` must not contain low-level DOM parsing specifics beyond what is
  delegated to `parser`.


### 7. API & Interfaces

#### 7.1 Python API

- **Entry point**
  - `ofac_sanctions_agent.run_agent(headless: bool = True, slow_mo: int = 250, fetch_remarks: bool = True) -> dict`
    - Callable from other Python services for embedding.

#### 7.2 CLI

- **Command**
  - `python -m ofac_sanctions_agent.cli [--visible] [--slow-mo MS] [--no-remarks]`
  - For backward compatibility:
    - `python src/agent.py [flags...]` delegates to the same CLI.


### 8. Operational Considerations

#### 8.1 Environments

- MVP assumption: single environment, developer or internal server.
- No environment-specific configuration beyond file paths and standard Python
  runtime.

#### 8.2 Deployment

- Deployment pattern examples:
  - Package as a Docker container with Python and Playwright dependencies.
  - Triggered via cron, Airflow, or other orchestration tools.

#### 8.3 Failure modes

- OFAC site unavailable:
  - Repeated timeouts; entities end up in `"error"` status.
- HTML structure changes:
  - Parser fails to detect table/rows; entities may show `"empty"` or `"error"`.
- Excessive CAPTCHAs:
  - Entities flagged as `"captcha"` with explanatory `error` text.


### 9. Testing Strategy (MVP)

- **Unit tests**
  - `parser`:
    - HTML fixtures or mocked `Page` objects to verify:
      - Detection of results vs. no results.
      - Correct extraction of columns and pagination.
  - `retry`:
    - Deterministic functions to verify backoff behavior and `RetryExhausted`.
- **Integration tests (smoke)**
  - Sanity run against a very small entity list, possibly using a mock or
  - controlled environment, to validate Playwright wiring and JSON envelope.


### 10. Roadmap (Post-MVP)

- **R1 — Concurrency**
  - Parallelize entity searches across multiple browser contexts or processes.
- **R2 — API surface**
  - Provide a simple HTTP API service for on-demand screening.
- **R3 — Configuration sources**
  - Support alternative configuration sources (DB, message queues, REST).
- **R4 — Observability enhancements**
  - Structured logs (JSON), metrics (e.g. Prometheus), tracing hooks.
- **R5 — Advanced matching**
  - Additional normalization/matching layers on top of OFAC search output.

