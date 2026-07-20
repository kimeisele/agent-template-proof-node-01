"""Discover federation peers via the GitHub API (curl-only).

Searches GitHub for repositories with the ``agent-federation-node`` topic,
fetches each peer's ``.well-known/agent-federation.json``, and writes a
local peer registry.

Requires either ``GITHUB_TOKEN`` env-var or unauthenticated access.

Usage:
    python scripts/discover_federation_peers.py [--output .federation/peers.json]
    python scripts/discover_federation_peers.py --org kimeisele
    python scripts/discover_federation_peers.py --seeds-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from federation_utils import curl_json

TOPIC = "agent-federation-node"
SEARCH_API = "https://api.github.com/search/repositories"
RAW_BASE = "https://raw.githubusercontent.com"


def _fetch_descriptor(full_name: str, default_branch: str) -> dict | None:
    url = f"{RAW_BASE}/{full_name}/{default_branch}/.well-known/agent-federation.json"
    data = curl_json(url)
    return data if isinstance(data, dict) else None


def _fetch_agent_card(full_name: str, default_branch: str) -> dict | None:
    url = f"{RAW_BASE}/{full_name}/{default_branch}/.well-known/agent.json"
    data = curl_json(url)
    return data if isinstance(data, dict) else None


def discover(
    *,
    org: str | None = None,
    exclude_self: str | None = None,
) -> list[dict]:
    """Return a list of peer records discovered from GitHub topic search."""
    query = f"topic:{TOPIC}"
    if org:
        query += f" org:{org}"
    url = f"{SEARCH_API}?q={query}&per_page=100"
    data = curl_json(url)
    if not isinstance(data, dict) or "items" not in data:
        print("warning: GitHub search returned no results", file=sys.stderr)
        return []

    peers: list[dict] = []
    for repo in data["items"]:
        full_name = repo["full_name"]
        if exclude_self and full_name == exclude_self:
            continue
        default_branch = repo.get("default_branch", "main")

        descriptor = _fetch_descriptor(full_name, default_branch)
        agent_card = _fetch_agent_card(full_name, default_branch)

        peer: dict = {
            "full_name": full_name,
            "html_url": repo["html_url"],
            "default_branch": default_branch,
            "description": repo.get("description") or "",
            "topics": repo.get("topics", []),
        }
        if descriptor:
            peer["federation_descriptor"] = descriptor
        if agent_card:
            peer["agent_card"] = agent_card

        peers.append(peer)

    return peers


def _load_seeds(repo_root: Path | None = None) -> list[str]:
    """Load descriptor URLs from authority-descriptor-seeds.json."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[1]
    seeds_path = repo_root / "data" / "federation" / "authority-descriptor-seeds.json"
    if not seeds_path.exists():
        return []
    data = json.loads(seeds_path.read_text())
    return data.get("descriptor_urls", [])


def discover_from_seeds(*, repo_root: Path | None = None) -> list[dict]:
    """Discover peers by fetching known descriptor seed URLs directly."""
    urls = _load_seeds(repo_root)
    peers: list[dict] = []
    for url in urls:
        descriptor = curl_json(url)
        if not isinstance(descriptor, dict):
            continue
        # Derive repo info from the URL pattern
        parts = url.split("/")
        if len(parts) >= 5 and "githubusercontent" in parts[2]:
            full_name = f"{parts[3]}/{parts[4]}"
        else:
            full_name = descriptor.get("repo_id", "unknown")
        peers.append({
            "full_name": full_name,
            "html_url": f"https://github.com/{full_name}",
            "default_branch": "main",
            "description": descriptor.get("display_name", ""),
            "topics": [],
            "federation_descriptor": descriptor,
            "discovery_method": "seed",
        })
    return peers


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover federation peers via GitHub API")
    parser.add_argument("--output", default=".federation/peers.json")
    parser.add_argument("--org", help="Limit discovery to a specific GitHub org")
    parser.add_argument("--seeds-only", action="store_true", help="Only use descriptor seeds (no GitHub search)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args()

    # Merge seed-based and topic-based discovery
    seen: set[str] = set()
    peers: list[dict] = []

    # Always try seeds first (works without authentication)
    seed_peers = discover_from_seeds()
    for p in seed_peers:
        if p["full_name"] not in seen:
            seen.add(p["full_name"])
            peers.append(p)
    if seed_peers:
        print(f"Seeds: {len(seed_peers)} peer(s) from authority-descriptor-seeds.json")

    # Then augment with GitHub topic search (unless --seeds-only)
    if not args.seeds_only:
        topic_peers = discover(org=args.org, exclude_self=args.repo or None)
        for p in topic_peers:
            if p["full_name"] not in seen:
                seen.add(p["full_name"])
                p["discovery_method"] = "topic_search"
                peers.append(p)
        if topic_peers:
            print(f"Topic search: {len(topic_peers)} peer(s) from GitHub")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    registry = {
        "kind": "federation_peer_registry",
        "version": 1,
        "peer_count": len(peers),
        "peers": peers,
    }
    output.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    print(f"Total: {len(peers)} unique peer(s) → {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
