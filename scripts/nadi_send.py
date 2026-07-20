#!/usr/bin/env python3
"""Queue a message to the Nadi outbox for federation relay pickup.

Uses the canonical :class:`NadiNode` from the pinned ``nadi-kit`` —
every message carries correct source, signature, payload hash, and
is written atomically via ``NadiTransport``.

Requires nadi-kit. Install with: pip install -e '.[federation]'

Usage:
    python scripts/nadi_send.py --to agent-research --op inquiry --payload '{"question":"What is dark matter?"}'
    python scripts/nadi_send.py --to agent-city --op heartbeat
    python scripts/nadi_send.py --list          # show pending outbox messages
    python scripts/nadi_send.py --clear         # clear outbox after relay pickup
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PEER_PATH = REPO_ROOT / "data" / "federation" / "peer.json"

from federation_utils import (  # noqa: E402
    NadiPathError,
    resolve_and_validate_nadi_paths,
)

_FEDERATION_INSTALL_HINT = (
    "nadi-kit is required for federation operations. "
    "Install with: pip install -e '.[federation]'"
)


def _load_nadi_node():
    """Return a :class:`NadiNode` loaded from the canonical peer.json.

    Returns ``(node, None)`` on success.
    Returns ``(None, exit_code)`` on failure.
    """
    if importlib.util.find_spec("nadi_kit") is None:
        print(
            "nadi-kit is not installed.\n" + _FEDERATION_INSTALL_HINT,
            file=sys.stderr,
        )
        return None, 1

    try:
        from nadi_kit import NadiNode  # noqa: E402
    except ImportError as exc:
        print(
            f"error: nadi-kit import failed ({exc}). "
            f"Module is findable but broken — not treated as absent.",
            file=sys.stderr,
        )
        return None, 1

    # Validate paths before constructing the node (which creates keys)
    try:
        resolve_and_validate_nadi_paths(_PEER_PATH)
    except NadiPathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 1

    if not _PEER_PATH.exists():
        print(
            f"error: {_PEER_PATH} not found. "
            f"Run scripts/setup_node.py first.",
            file=sys.stderr,
        )
        return None, 1

    try:
        node = NadiNode.from_peer_json(_PEER_PATH)
    except Exception as exc:
        print(
            f"error: failed to load node from {_PEER_PATH}: {exc}",
            file=sys.stderr,
        )
        return None, 1

    return node, 0


def cmd_send(args: argparse.Namespace) -> int:
    """Emit a message to the outbox via NadiNode.emit()."""
    if not args.to or not args.op:
        print("error: --to and --op are required", file=sys.stderr)
        return 1

    payload: dict | None = None
    if args.payload:
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError:
            print("error: --payload must be valid JSON", file=sys.stderr)
            return 1
        if not isinstance(payload, dict):
            print(
                "error: --payload must be a JSON object (dict/mapping), "
                f"got {type(payload).__name__}",
                file=sys.stderr,
            )
            return 1

    node, exit_code = _load_nadi_node()
    if node is None:
        return exit_code

    # Emit via the canonical node — gives us correct source, signature,
    # payload hash, atomic transport, and buffer discipline.
    try:
        messages = node.emit(
            operation=args.op,
            payload=payload,
            target=args.to,
            priority=args.priority,
            ttl_s=args.ttl_seconds,
        )
    except Exception as exc:
        print(f"error: emit failed: {exc}", file=sys.stderr)
        return 1

    if not messages:
        print("error: emit returned no messages", file=sys.stderr)
        return 1

    msg = messages[0]

    # Postcondition: re-read via transport and verify the message is there.
    outbox = node.transport.read_outbox()
    found = any(m.id == msg.id for m in outbox)
    if not found:
        print("error: message emitted but re-read verification failed",
              file=sys.stderr)
        return 1

    federation_dir = node.transport.federation_dir
    print(
        f"Queued message {msg.id[:8]}… → {msg.target} ({msg.operation})"
    )
    print(
        f"  source:  {msg.source}"
    )
    print(
        f"  ttl:     {msg.ttl_s}s  priority: {msg.priority}"
    )
    print(
        f"  signed:  {bool(msg.signature)}  "
        f"payload_hash: {msg.payload_hash[:16] if msg.payload_hash else 'N/A'}…"
    )
    print(
        f"Outbox ({federation_dir}/nadi_outbox.json): {len(outbox)} message(s)"
    )
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    """List pending outbox messages via NadiTransport."""
    node, exit_code = _load_nadi_node()
    if node is None:
        return exit_code

    outbox = node.transport.read_outbox()
    federation_dir = node.transport.federation_dir
    if not outbox:
        print(f"Outbox ({federation_dir}/nadi_outbox.json): empty.")
        return 0

    print(f"{len(outbox)} pending message(s):\n")
    for i, msg in enumerate(outbox, 1):
        print(
            f"  {i}. [{msg.id[:8]}…] "
            f"→ {msg.target} ({msg.operation}) "
            f"ttl={msg.ttl_s}s"
        )
    print(f"\nOutbox path: {federation_dir}/nadi_outbox.json")
    return 0


def cmd_clear(_args: argparse.Namespace) -> int:
    """Clear the outbox via NadiTransport, with re-read postcondition."""
    node, exit_code = _load_nadi_node()
    if node is None:
        return exit_code

    federation_dir = node.transport.federation_dir
    cleared = node.transport.clear_outbox()

    # Postcondition: outbox is empty.
    verify = node.transport.read_outbox()
    if len(verify) != 0:
        print("error: clear succeeded but re-read found messages",
              file=sys.stderr)
        return 1

    print(
        f"Outbox ({federation_dir}/nadi_outbox.json) cleared "
        f"({cleared} message(s) removed)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Nadi outbox message tool (requires nadi-kit)"
    )
    sub = parser.add_subparsers(dest="command")

    send = sub.add_parser("send", help="Queue a message for relay")
    send.add_argument("--to", required=True, help="Target node ID (e.g. agent-research)")
    send.add_argument("--op", required=True, help="Operation name (e.g. inquiry, heartbeat)")
    send.add_argument("--payload", default=None, help="JSON payload string")
    send.add_argument("--priority", type=int, default=5, help="Priority 1-10 (default 5)")
    send.add_argument("--ttl-seconds", type=float, default=300.0,
                      help="TTL in seconds (default 300)")

    sub.add_parser("list", help="List pending outbox messages")
    sub.add_parser("clear", help="Clear the outbox")

    # Flat convenience flags
    parser.add_argument("--list", action="store_true", help="List pending messages")
    parser.add_argument("--clear", action="store_true", help="Clear the outbox")
    parser.add_argument("--to", default=None, help="Target node ID")
    parser.add_argument("--op", default=None, help="Operation name")
    parser.add_argument("--payload", default=None, help="JSON payload")
    parser.add_argument("--priority", type=int, default=5)
    parser.add_argument("--ttl-seconds", type=float, default=300.0,
                        help="TTL in seconds (default 300)")

    args = parser.parse_args()

    # Backward-compat: map --ttl (old ms) to --ttl-seconds if only --ttl given
    # (not needed with explicit --ttl-seconds, but kept for docs transition)

    if args.list or args.command == "list":
        return cmd_list(args)
    if args.clear or args.command == "clear":
        return cmd_clear(args)
    if args.command == "send" or (args.to and args.op):
        return cmd_send(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
