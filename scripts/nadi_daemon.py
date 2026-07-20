#!/usr/bin/env python3
"""NADI federation daemon — heartbeat + inbox sync for new nodes.

Requires nadi-kit for relay modes.  Local diagnostic (``--once``
without ``--relay``) works without nadi-kit.

Install: pip install -e '.[federation]'

Modes
-----

``--once``
    Strictly read-only local diagnostic.
    Reads peer.json, outbox, and inbox — no mutations, no keys,
    no heartbeat, no hub access.

``--once --relay``
    Exactly one real federation sync cycle with hub pull/push.

``--relay``
    Continuous daemon loop with heartbeat + hub pull/push.

Usage:
    python scripts/nadi_daemon.py --once            # local diagnostic
    python scripts/nadi_daemon.py --once --relay    # single relay cycle
    python scripts/nadi_daemon.py --relay           # continuous relay
"""

import argparse
import importlib.util
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PEER_PATH = REPO_ROOT / "data" / "federation" / "peer.json"

log = logging.getLogger("nadi_daemon")


# ── nadi-kit guard ──────────────────────────────────────────────────────────


def _check_nadi_kit_available() -> int | None:
    """Return ``None`` if nadi-kit is importable, or an exit code.

    Genuine absence → prints install hint, returns 1.
    Findable but broken → prints error, returns 1.
    """
    if importlib.util.find_spec("nadi_kit") is None:
        print(
            "nadi-kit is not installed.\n"
            "Install with: pip install -e '.[federation]'",
            file=sys.stderr,
        )
        return 1

    try:
        import nadi_kit  # noqa: F401
    except ImportError as exc:
        print(
            f"error: nadi-kit import failed ({exc}). "
            f"Module is findable but broken — not treated as absent.",
            file=sys.stderr,
        )
        return 1

    return None  # available


# ── Transport loader (read-only, no keys) ───────────────────────────────────


def _load_nadi_transport_readonly():
    """Return ``(NadiTransport, exit_code_or_None)`` for local diagnostic.

    Does **not** construct a NadiNode — no keys are generated, no
    files are created.
    """
    exit_code = _check_nadi_kit_available()
    if exit_code is not None:
        return None, exit_code

    try:
        from nadi_kit import NadiTransport  # noqa: E402
    except ImportError as exc:
        print(
            f"error: NadiTransport import failed ({exc}). "
            f"Module is findable but broken — not treated as absent.",
            file=sys.stderr,
        )
        return None, 1

    try:
        contract = resolve_and_validate_nadi_paths(_PEER_PATH)
    except NadiPathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 1

    return NadiTransport(str(contract.federation_dir)), 0


# ── Node loader (for relay modes — creates keys) ───────────────────────────


def _load_nadi_node():
    """Load a NadiNode from the canonical peer.json.

    **WARNING:** This constructs a full NadiNode which generates keys.
    Only use for relay modes.
    """
    exit_code = _check_nadi_kit_available()
    if exit_code is not None:
        return None, exit_code

    try:
        from nadi_kit import NadiNode  # noqa: E402
    except ImportError as exc:
        print(
            f"error: NadiNode import failed ({exc}). "
            f"Module is findable but broken — not treated as absent.",
            file=sys.stderr,
        )
        return None, 1

    try:
        resolve_and_validate_nadi_paths(_PEER_PATH)
    except NadiPathError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 1

    try:
        node = NadiNode.from_peer_json(_PEER_PATH)
    except Exception as exc:
        print(f"ERROR: failed to load node from {_PEER_PATH}: {exc}",
              file=sys.stderr)
        return None, 1

    return node, 0


# ── Path validation (imported from federation_utils, re-exported for tests) ─

from federation_utils import (  # noqa: E402
    NadiPathError,
    resolve_and_validate_nadi_paths,
)

# ── Local diagnostic (read-only, no NadiNode) ───────────────────────────────


def _do_local_diagnostic() -> int:
    """Read-only local diagnostic — no NadiNode, no keys, no mutations."""
    transport, exit_code = _load_nadi_transport_readonly()
    if transport is None:
        return exit_code

    peer = json.loads(_PEER_PATH.read_text())
    city_id = peer.get("identity", {}).get("city_id", "unknown")
    contract = resolve_and_validate_nadi_paths(_PEER_PATH)

    try:
        outbox = transport.read_outbox()
        inbox = transport.read_inbox()
    except Exception as exc:
        log.error("transport read failed: %s", exc)
        return 1

    print(f"Node:  {city_id}")
    print(f"Peer:  {contract.peer_path}")
    print(f"Dir:   {contract.federation_dir}")
    print(f"Outbox: {len(outbox)} pending message(s)")
    for msg in outbox[:5]:
        print(f"  [{msg.id[:8]}…] → {msg.target} ({msg.operation}) "
              f"ttl={msg.ttl_s}s")
    if len(outbox) > 5:
        print(f"  … and {len(outbox) - 5} more")
    print(f"Inbox:  {len(inbox)} message(s)")
    for msg in inbox[:5]:
        print(f"  [{msg.id[:8]}…] ← {msg.source} ({msg.operation})")
    if len(inbox) > 5:
        print(f"  … and {len(inbox) - 5} more")

    return 0


# ── Relay modes (requires NadiNode) ─────────────────────────────────────────


def _handle_heartbeat(msg):
    log.info("heartbeat from %s (health=%.2f)", msg.source,
             msg.payload.get("health", 0))


def _handle_default(msg):
    log.info("received op=%s from %s", msg.operation, msg.source)


def _run_relay_cycle(node, args) -> int:
    if args.head_agent:
        try:
            import importlib as _il
            module_path, class_name = args.head_agent.rsplit(".", 1)
            mod = _il.import_module(module_path)
            cls = getattr(mod, class_name)
            head_instance = cls(node)
            head_instance.heartbeat()
        except Exception as exc:
            log.warning("HeadAgent failed: %s", exc)
            node.heartbeat(health=args.health)
    else:
        node.heartbeat(health=args.health)

    try:
        stats = node.sync()
        log.info("pulled=%d processed=%d pushed=%d expired=%d",
                 stats.get("pulled", 0), stats.get("processed", 0),
                 stats.get("pushed", 0), stats.get("expired", 0))
    except Exception as exc:
        log.error("sync failed: %s", exc)
        return 1
    return 0


def _execute_mode(args, node_loader=_load_nadi_node) -> int:
    # ── --once without --relay: read-only local diagnostic ──
    if args.once and not args.relay:
        return _do_local_diagnostic()

    # ── Relay modes require NadiNode ──
    if args.relay:
        node, exit_code = node_loader()
        if node is None:
            return exit_code

        node.on("heartbeat", _handle_heartbeat)
        node.on("*", _handle_default)

        print("\n  ⚠  REMOTE RELAY ENABLED\n"
              "  Hub: kimeisele/steward-federation\n")
        log.info("relay daemon started for %s", node.agent_id)

        if args.once:
            return _run_relay_cycle(node, args)

        cycle = 0
        import time
        while True:
            cycle += 1
            log.info("=== relay cycle %d ===", cycle)
            if _run_relay_cycle(node, args) != 0:
                log.warning("relay cycle %d had errors", cycle)
            time.sleep(args.interval)

    return 1


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv=None, *, node_loader=_load_nadi_node) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="NADI federation daemon")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--relay", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--health", type=float, default=1.0)
    parser.add_argument("--head-agent", type=str, default=None)
    if argv is None:
        argv = sys.argv[1:]
    args = parser.parse_args(argv)

    if not args.once and not args.relay:
        parser.print_help()
        return 1

    return _execute_mode(args, node_loader=node_loader)


if __name__ == "__main__":
    raise SystemExit(main())
