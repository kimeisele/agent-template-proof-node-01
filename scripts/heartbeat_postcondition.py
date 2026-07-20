#!/usr/bin/env python3
"""Capture and verify heartbeat message IDs in the steward-federation hub.

Usage:
    python scripts/heartbeat_postcondition.py capture \
      --outbox data/federation/nadi_outbox.json \
      --output heartbeat-proof.json

    python scripts/heartbeat_postcondition.py verify \
      --proof heartbeat-proof.json

Exit codes:
    0 — all captured heartbeat IDs confirmed in hub
    1 — postcondition not met
    2 — usage or I/O error
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path


# ── capture ─────────────────────────────────────────────────────────────────


def cmd_capture(outbox_path: str, output_path: str) -> int:
    """Read outbox and save proof of heartbeat/claim message IDs."""
    opath = Path(outbox_path)
    if not opath.exists():
        print(f"error: outbox not found: {opath}", file=sys.stderr)
        return 2

    try:
        raw = json.loads(opath.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: outbox is not valid JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(raw, list) or not raw:
        print("error: outbox is empty", file=sys.stderr)
        return 1

    # Validate: all messages must share the same crypto source
    sources = {m.get("source") for m in raw if isinstance(m, dict) and m.get("source")}
    if len(sources) != 1:
        print(
            f"error: outbox contains messages from {len(sources)} different sources. "
            f"All messages in one capture must share the same source.",
            file=sys.stderr,
        )
        return 2
    source = sources.pop()

    # Filter: require at least one heartbeat
    heartbeat_msgs = [
        m for m in raw
        if isinstance(m, dict)
        and m.get("operation") == "heartbeat"
        and m.get("id")
    ]
    if not heartbeat_msgs:
        print("error: no heartbeat message found in outbox", file=sys.stderr)
        return 1

    # Additional messages from the same emit cycle (e.g. federation.agent_claim)
    heartbeat_ids = [m["id"] for m in heartbeat_msgs]
    other_ids = [
        m["id"] for m in raw
        if isinstance(m, dict)
        and m.get("id")
        and m.get("operation") != "heartbeat"
        and m.get("source") == source
    ]

    proof = {
        "source_node_id": source,
        "heartbeat_message_ids": heartbeat_ids,
        "additional_message_ids": other_ids,
        "captured_at": time.time(),
    }

    Path(output_path).write_text(json.dumps(proof, indent=2) + "\n")
    print(f"Captured {len(heartbeat_ids)} heartbeat + {len(other_ids)} additional "
          f"message(s) from source {source}")
    for mid in heartbeat_ids:
        print(f"  heartbeat: {mid[:16]}…")
    for mid in other_ids:
        print(f"  additional: {mid[:16]}…")
    return 0


# ── verify ─────────────────────────────────────────────────────────────────


def _list_hub_nadi_files() -> list[dict] | None:
    token = (os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        print("error: GH_TOKEN not set", file=sys.stderr)
        return None
    result = subprocess.run(
        ["gh", "api", "repos/kimeisele/steward-federation/contents/nadi"],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "GH_TOKEN": token},
    )
    if result.returncode != 0:
        print(f"error: cannot list hub files: {result.stderr.strip()[:120]}",
              file=sys.stderr)
        return None
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("error: hub API returned invalid JSON", file=sys.stderr)
        return None
    return entries if isinstance(entries, list) else None


def _fetch_hub_file(api_url: str) -> list | None:
    """Fetch a single hub nadi file via the GitHub Contents API.

    The response must be a JSON object with ``encoding: "base64"`` and
    a ``content`` field.  The decoded content must be a JSON list.
    Returns ``None`` on any failure.
    """
    result = subprocess.run(
        ["gh", "api", api_url],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN", "")},
    )
    if result.returncode != 0:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    # Must be a Contents API object (dict with encoding/content), not a raw list
    if not isinstance(data, dict):
        return None

    encoding = data.get("encoding")
    if encoding != "base64":
        return None

    encoded = data.get("content")
    if not isinstance(encoded, str):
        return None

    try:
        raw_bytes = base64.b64decode(encoded)
        raw_text = raw_bytes.decode("utf-8")
        parsed = json.loads(raw_text)
    except Exception:
        return None

    if not isinstance(parsed, list):
        return None

    return parsed


def cmd_verify(proof_path: str) -> int:
    ppath = Path(proof_path)
    if not ppath.exists():
        print(f"error: proof file not found: {ppath}", file=sys.stderr)
        return 2

    try:
        proof = json.loads(ppath.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: proof file invalid: {exc}", file=sys.stderr)
        return 2

    source = proof.get("source_node_id", "")
    heartbeat_ids = proof.get("heartbeat_message_ids", [])
    captured_at = proof.get("captured_at", 0)

    if not source or not heartbeat_ids:
        print("error: proof missing source or heartbeat_message_ids",
              file=sys.stderr)
        return 2

    # Timestamp sanity
    if captured_at > time.time() + 300:
        print("error: captured_at is in the future", file=sys.stderr)
        return 1

    # List hub files
    entries = _list_hub_nadi_files()
    if entries is None:
        return 1

    # Find files matching crypto source
    prefix = f"{source}_to_"
    matching = [e for e in entries
                if isinstance(e, dict)
                and e.get("name", "").startswith(prefix)]

    if not matching:
        print(
            f"error: no hub files for source {source}. "
            f"Files: {', '.join(e.get('name','?') for e in entries[:10]) or '(none)'}",
            file=sys.stderr,
        )
        return 1

    # Read matching files, validate heartbeat IDs with correct source + operation
    found_heartbeat_ids: set[str] = set()
    for entry in matching:
        api_url = entry.get("url", "")
        if not api_url:
            continue
        content = _fetch_hub_file(api_url)
        if isinstance(content, list):
            for msg in content:
                if not isinstance(msg, dict):
                    continue
                mid = msg.get("id")
                msg_source = msg.get("source")
                msg_op = msg.get("operation")
                # Must match source, operation, and be one of our heartbeat IDs
                if (mid and mid in heartbeat_ids
                        and msg_source == source
                        and msg_op == "heartbeat"):
                    found_heartbeat_ids.add(mid)

    missing = [mid for mid in heartbeat_ids if mid not in found_heartbeat_ids]
    if missing:
        print(
            f"error: {len(missing)}/{len(heartbeat_ids)} heartbeat ID(s) "
            f"not confirmed in hub for source {source}",
            file=sys.stderr,
        )
        for mid in missing:
            print(f"  missing: {mid[:16]}…", file=sys.stderr)
        return 1

    print(f"Hub postcondition verified: {len(found_heartbeat_ids)} heartbeat "
          f"ID(s) for source {source} in {len(matching)} hub file(s)")
    for mid in found_heartbeat_ids:
        print(f"  confirmed: {mid[:16]}…")
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Capture and verify heartbeat hub postcondition")
    sub = parser.add_subparsers(dest="command")

    cap = sub.add_parser("capture")
    cap.add_argument("--outbox", default="data/federation/nadi_outbox.json")
    cap.add_argument("--output", default="heartbeat-proof.json")

    ver = sub.add_parser("verify")
    ver.add_argument("--proof", default="heartbeat-proof.json")

    args = parser.parse_args()

    if args.command == "capture":
        return cmd_capture(args.outbox, args.output)
    if args.command == "verify":
        return cmd_verify(args.proof)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
