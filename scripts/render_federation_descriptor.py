from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from federation_utils import resolve_human_display_name, resolve_repo_identity


def _load_capabilities(repo_root: Path) -> list[str]:
    caps_path = repo_root / "docs" / "authority" / "capabilities.json"
    if caps_path.exists():
        try:
            data = json.loads(caps_path.read_text())
        except (json.JSONDecodeError, OSError):
            return ["authority-publishing"]
        if isinstance(data, dict):
            return [s["id"] for s in data.get("skills", [])]
    return ["authority-publishing"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=".well-known/agent-federation.json")
    parser.add_argument("--repo", default=None, help="Explicit owner/repo override (test/offline only)")
    parser.add_argument("--status", default="active")
    parser.add_argument("--layer", default="node")
    parser.add_argument("--intent", nargs="*", default=["public_authority_page"])
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    try:
        repo = resolve_repo_identity(repo_root, explicit_repo=args.repo)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    repo_owner, repo_name = repo.split("/", 1)
    payload = {
        "kind": "agent_federation_descriptor",
        "version": 1,
        "repo_id": repo_name,
        "display_name": resolve_human_display_name(repo_root, repo_name),
        "authority_feed_manifest_url": f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/authority-feed/latest-authority-manifest.json",
        "projection_intents": list(dict.fromkeys(args.intent)),
        "status": args.status,
        "capabilities": _load_capabilities(repo_root),
        "layer": args.layer,
        "endpoints": {
            "federation_descriptor": ".well-known/agent-federation.json",
            "authority_descriptor_seeds": "data/federation/authority-descriptor-seeds.json",
        },
        "owner_boundary": f"{repo_name.replace('-', '_')}_surface",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
