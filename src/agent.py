"""
Thin wrapper to keep ``python src/agent.py`` working.

The real implementation now lives in the ``ofac_sanctions_agent`` package.
"""

from __future__ import annotations

from ofac_sanctions_agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

