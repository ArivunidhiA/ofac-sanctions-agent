## 🚀 OFAC Sanctions Agent

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](#-changelog)
[![Python](https://img.shields.io/badge/python-3.11%2B-brightgreen)](#-tech-stack)
[![Status](https://img.shields.io/badge/status-MVP%20ready-success)](#-overview)
[![License](https://img.shields.io/badge/license-TBD-lightgrey)](#-license)
[![Playwright](https://img.shields.io/badge/automation-Playwright-2ea44f)](#-tech-stack)

Async Playwright agent for OFAC SDN sanctions screening — built for stability, observability, and easy integration into risk / compliance pipelines.

---

## 📚 Table of Contents

- [🔎 Overview](#-overview)
- [✨ Features](#-features)
- [🏗️ Architecture](#️-architecture)
- [🧰 Tech Stack](#-tech-stack)
- [⚡ Quick Start](#-quick-start)
- [🛠️ Configuration](#️-configuration)
- [✅ Testing](#-testing)
- [📦 Project Layout](#-project-layout)
- [🗺️ Roadmap](#️-roadmap)
- [📄 License](#-license)

---

## 🔎 Overview

The **OFAC Sanctions Agent** screens one or more entities against the U.S. Treasury OFAC SDN sanctions list (`https://sanctionssearch.ofac.treas.gov/`) by driving a real Chromium browser with Playwright.

Given a configuration file of entities, the agent:

- Submits each entity to the OFAC SDN search form using **adaptive selectors**.
- Parses the results table (name, type, program, list, score).
- Optionally drills into each row to fetch detailed **remarks**.
- Emits structured JSON suitable for downstream ingestion and auditing.
- Produces verbose logs for **investigation and compliance evidence**.

Built as a small, focused library + CLI, it can run as a cron job, a CI step, or inside a larger KYC / sanctions pipeline.

---

## ✨ Features

### Core screening

- **Batch entity search** via `config/targets.json`.
- **Resilient selectors** for OFAC’s ASP.NET WebForms UI (fallback chains).
- **Per-entity status**: `ok`, `empty`, `captcha`, `error`.
- **Structured hits**: name, type, program, list, score, remarks, raw row index.

### Reliability & robustness

- **Exponential backoff + jitter** for transient timeouts and network issues.
- **Per-entity isolation** — one failing entity never crashes the whole run.
- **Incremental result flushing** — partial runs still leave a valid JSON file.
- **CAPTCHA detection** with graceful skipping and logging.

### Observability

- **Rich logging** to `logs/agent.log` (INFO/DEBUG/WARNING/ERROR).
- **Run envelope metadata**: start/end timestamps, hit counts, error counts.
- **Test suite** with unit + integration tests (Playwright mocked in tests).

---

## 🏗️ Architecture

### High-level diagram

```text
                   +-----------------------------+
                   |     CLI / Python caller     |
                   |  (ofac_sanctions_agent.cli) |
                   +---------------+-------------+
                                   |
                                   v
                     +-------------+-------------+
                     |     Agent Orchestrator    |
                     |  (ofac_sanctions_agent)   |
                     +------+------+-------------+
                            |      |
                config      |      |  parsing / retry
                            |      |
            +---------------+      +-------------------+
            v                                      v
  +---------+-----------+              +-----------+-----------+
  |  Config & Models    |              | Parser & Retry Utils  |
  | (config.SearchSettings,           | (parser, retry)        |
  |  AgentConfig)                     +-----------+-----------+
  +---------+-----------+                          |
            |                                      |
            v                                      v
      +-----+-----------------------------+   network / DOM
      |     Playwright Chromium Browser   |------------------> OFAC SDN Portal
      +----------------+------------------+
                       |
         +-------------+------------------------+
         v                                      v
  logs/agent.log                        output/results.json
```

### Component reference

| Component                          | Description                                               |
|------------------------------------|-----------------------------------------------------------|
| `ofac_sanctions_agent.agent`       | Main orchestration: browser lifecycle, per-entity search |
| `ofac_sanctions_agent.config`      | Path handling and `AgentConfig` / `SearchSettings`       |
| `ofac_sanctions_agent.parser`      | DOM parsing, CAPTCHA detection, pagination, remarks      |
| `ofac_sanctions_agent.retry`       | Async retry with exponential backoff and jitter          |
| `ofac_sanctions_agent.logging_config` | Central logging setup                               |
| `ofac_sanctions_agent.cli`         | CLI entrypoint and argument parsing                      |

---

## 🧰 Tech Stack

### Backend

- **Language**: Python 3.11+
- **Automation**: Playwright (Chromium)
- **Testing**: `pytest`, `pytest-asyncio`

### Frontend

- None — this is a **CLI + library** project. Any UI (web/desktop) is expected to wrap the library.

### Infrastructure

- Runs anywhere you can install Python + Playwright:
  - Dev machines
  - CI runners (GitHub Actions, GitLab, etc.)
  - Containers / Kubernetes jobs

---

## ⚡ Quick Start

### 1. Prerequisites

- Python **3.11+**
- `git`
- Playwright browsers installed (once):

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Installation

```bash
git clone https://github.com/ArivunidhiA/ofac-sanctions-agent.git
cd ofac-sanctions-agent
pip install -r requirements.txt
```

### 3. Configure targets

Edit `config/targets.json` and add entities to screen:

```json
{
  "entities": [
    { "id": "T001", "name": "KIM JONG UN", "notes": "SDN list example" }
  ],
  "search_settings": {
    "score_threshold": 0,
    "max_results_per_entity": 50,
    "search_type": "name"
  }
}
```

### 4. Run the agent

Visible browser:

```bash
python -m ofac_sanctions_agent.cli --visible
```

Headless (default):

```bash
python -m ofac_sanctions_agent.cli
```

Legacy entrypoint (still supported):

```bash
python src/agent.py --visible
```

Results and logs:

- JSON output: `output/results.json`
- Logs: `logs/agent.log`

---

## 🛠️ Configuration

### JSON configuration (primary)

The agent is configured via `config/targets.json`:

```json
{
  "entities": [
    { "id": "T001", "name": "ACME BANK", "notes": "Sample counterparty" }
  ],
  "search_settings": {
    "score_threshold": 0,
    "max_results_per_entity": 50,
    "search_type": "name"
  }
}
```

Key points:

- `entities`: array of objects with `id` (string, optional), `name` (string, required), `notes` (string, optional).
- `search_settings`:
  - `score_threshold`: minimum OFAC score to consider (currently informational).
  - `max_results_per_entity`: advisory cap for downstream processing.
  - `search_type`: reserved for future search modes; default `"name"`.

### Environment variables (optional patterns)

The core library does **not** require environment variables, but when you wrap it (e.g. in a job runner or container), the following pattern works well:

```bash
export OFAC_AGENT_HEADLESS=true
export OFAC_AGENT_SLOW_MO_MS=250
```

Then in your own wrapper script you can map these into the Python API:

```python
import os
from ofac_sanctions_agent import run_agent

headless = os.getenv("OFAC_AGENT_HEADLESS", "true").lower() == "true"
slow_mo = int(os.getenv("OFAC_AGENT_SLOW_MO_MS", "250"))

run_output = asyncio.run(run_agent(headless=headless, slow_mo=slow_mo))
```

---

## ✅ Testing

Run the full test suite (unit + integration, Playwright mocked in tests):

```bash
pytest
```

Key coverage:

- `tests/test_retry.py` — retry semantics and `RetryExhausted`.
- `tests/test_parser.py` — CAPTCHA detection logic.
- `tests/test_agent_integration.py` — incremental saving, selector drift, CAPTCHA flows, and retry behavior (with Playwright objects mocked).

---

## 📦 Project Layout

```text
config/
  targets.json          # Input entities and search settings
docs/
  PDR.md                # Product & Design Requirements (MVP)
src/
  ofac_sanctions_agent/
    __init__.py
    agent.py            # Orchestrator and Playwright wiring
    config.py           # Paths and AgentConfig/SearchSettings
    parser.py           # DOM parsing & CAPTCHA detection
    retry.py            # Async retry utilities
    logging_config.py   # Logging setup
    cli.py              # CLI entrypoint
tests/
  test_retry.py
  test_parser.py
  test_agent_integration.py
output/
  results.json          # Generated JSON output (runtime)
logs/
  agent.log             # Runtime logs
```

---

## 🗺️ Roadmap

- Parallelization across multiple browser contexts / workers.
- First-class HTTP API wrapper for on-demand screening.
- Structured logging (JSON) and metrics for observability stacks.
- Pluggable storage backends (PostgreSQL, S3, message queues).

---

## 📄 License

License is currently **TBD**.  
Choose a license (e.g. MIT, Apache-2.0) before using this in production or distributing binaries.
