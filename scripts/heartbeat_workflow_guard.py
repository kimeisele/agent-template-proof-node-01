#!/usr/bin/env python3
"""Workflow guard: check federation secret configuration state.

Reads ``FEDERATION_PAT`` and ``NODE_PRIVATE_KEY`` from environment
variables and outputs a structured JSON status line.  Never prints
secret values.

Exit codes:
  0 — status determined successfully (enabled or disabled)
  2 — unexpected error

Output (JSON on stdout):
  {"status": "REMOTE_ENABLED"}
  {"status": "REMOTE_DISABLED_MISSING_PAT", "reason": "FEDERATION_PAT not set"}
  {"status": "REMOTE_DISABLED_MISSING_NODE_KEY", "reason": "NODE_PRIVATE_KEY not set"}
"""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    pat = os.environ.get("FEDERATION_PAT", "").strip()
    node_key = os.environ.get("NODE_PRIVATE_KEY", "").strip()

    if not pat:
        json.dump({
            "status": "REMOTE_DISABLED_MISSING_PAT",
            "reason": "FEDERATION_PAT not set",
        }, sys.stdout)
        print()
        return 0

    if not node_key:
        json.dump({
            "status": "REMOTE_DISABLED_MISSING_NODE_KEY",
            "reason": "NODE_PRIVATE_KEY not set",
        }, sys.stdout)
        print()
        return 0

    json.dump({"status": "REMOTE_ENABLED"}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
