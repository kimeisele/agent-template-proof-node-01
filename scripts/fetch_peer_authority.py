"""Fetch and validate a peer's authority feed manifest.

Given a peer's ``authority_feed_manifest_url`` (from the federation
descriptor), downloads the manifest and verifies SHA-256 hashes of
each artifact.

Usage:
    python scripts/fetch_peer_authority.py <manifest-url>
    python scripts/fetch_peer_authority.py --peers .federation/peers.json
"""
from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path

from federation_utils import curl_bytes, curl_json


def fetch_and_verify(manifest_url: str, output_dir: Path) -> dict:
    """Fetch the manifest and each artifact.  Returns a verification report."""
    manifest = curl_json(manifest_url)
    if not isinstance(manifest, dict):
        return {"manifest_url": manifest_url, "status": "unreachable"}

    base_url = manifest_url.rsplit("/", 1)[0]
    repo_id = manifest.get("source_repo_id", "unknown")
    source_sha = manifest.get("source_sha", "unknown")
    report: dict = {
        "manifest_url": manifest_url,
        "repo_id": repo_id,
        "source_sha": source_sha,
        "status": "ok",
        "artifacts": {},
    }

    dest = output_dir / repo_id / source_sha
    dest.mkdir(parents=True, exist_ok=True)

    # Save manifest
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    # Fetch and verify artifacts
    for rel_path, meta in (manifest.get("artifacts") or {}).items():
        artifact_url = f"{base_url}/{meta['path']}"
        raw = curl_bytes(artifact_url)
        if raw is None:
            report["artifacts"][rel_path] = {"status": "unreachable"}
            report["status"] = "partial"
            continue

        actual_sha = sha256(raw).hexdigest()
        expected_sha = meta.get("sha256", "")
        ok = actual_sha == expected_sha

        artifact_dest = dest / rel_path
        artifact_dest.parent.mkdir(parents=True, exist_ok=True)
        artifact_dest.write_bytes(raw)

        report["artifacts"][rel_path] = {
            "status": "verified" if ok else "sha256_mismatch",
            "expected_sha256": expected_sha,
            "actual_sha256": actual_sha,
        }
        if not ok:
            report["status"] = "integrity_error"

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch and verify peer authority feeds")
    parser.add_argument("manifest_url", nargs="?", help="Direct manifest URL to fetch")
    parser.add_argument("--peers", help="Path to peers.json; fetches all peer feeds")
    parser.add_argument("--output-dir", default=".federation/peer-feeds")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)

    urls: list[str] = []
    if args.manifest_url:
        urls.append(args.manifest_url)
    elif args.peers:
        peers_data = json.loads(Path(args.peers).read_text())
        for peer in peers_data.get("peers", []):
            desc = peer.get("federation_descriptor", {})
            url = desc.get("authority_feed_manifest_url")
            if url:
                urls.append(url)
    else:
        print("error: provide a manifest URL or --peers file", file=sys.stderr)
        return 1

    reports: list[dict] = []
    for url in urls:
        print(f"Fetching {url} …")
        report = fetch_and_verify(url, output_dir)
        reports.append(report)
        status = report["status"]
        marker = "ok" if status == "ok" else f"** {status} **"
        print(f"  → {report.get('repo_id', '?')} [{marker}]")

    summary_path = output_dir / "verification-report.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(reports, indent=2, sort_keys=True) + "\n")
    print(f"\nVerification report → {summary_path}")

    return 0 if all(r["status"] == "ok" for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
