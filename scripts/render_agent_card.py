"""Generate .well-known/agent.json (A2A-inspired agent card).

Reads the federation descriptor and capability manifest to produce
a machine-readable agent card for capability discovery.

Usage:
    python scripts/render_agent_card.py [--output .well-known/agent.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from federation_utils import resolve_human_display_name, resolve_repo_identity


def _load_descriptor(repo_root: Path) -> dict:
    desc_path = repo_root / ".well-known" / "agent-federation.json"
    if desc_path.exists():
        return json.loads(desc_path.read_text())
    return {}


def _load_capability_manifest(repo_root: Path) -> dict:
    caps_path = repo_root / "docs" / "authority" / "capabilities.json"
    if caps_path.exists():
        try:
            data = json.loads(caps_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, dict):
            return data
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Render agent card")
    parser.add_argument("--output", default=".well-known/agent.json")
    parser.add_argument("--repo", default=None, help="Explicit owner/repo override (test/offline only)")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    try:
        repo = resolve_repo_identity(repo_root, explicit_repo=args.repo)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    repo_owner, repo_name = repo.split("/", 1)
    descriptor = _load_descriptor(repo_root)
    manifest = _load_capability_manifest(repo_root)
    name = resolve_human_display_name(repo_root, repo_name)
    description = manifest.get("description") or f"{name} — a federation node in the agent-internet."

    card = {
        "name": name,
        "description": description,
        "url": f"https://github.com/{repo_owner}/{repo_name}",
        "version": "1.0.0",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "skills": manifest.get("skills", []),
        "provider": {
            "organization": repo_owner,
        },
        "federation": {
            "node_topic": "agent-federation-node",
            "node_role": manifest.get("node_role", "federation_node"),
            "descriptor_path": ".well-known/agent-federation.json",
            "authority_feed_branch": "authority-feed",
            "interfaces": manifest.get("federation_interfaces", {}),
            "peer_discovery": True,
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(card, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
