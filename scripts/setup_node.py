#!/usr/bin/env python3
"""Interactive federation node setup wizard.

Two phases:
  Phase 1 — Identity: configure your node locally (charter, capabilities, descriptors)
  Phase 2 — Connect: choose your Agent City zone, discover peers, verify readiness

Branch governance is evaluated after the two phases.  Apply with
``--apply-governance`` or interactively.

Usage:
    python scripts/setup_node.py
    python scripts/setup_node.py --non-interactive --name "My Node" --role research
    python scripts/setup_node.py --status
    python scripts/setup_node.py --apply-governance
"""
from __future__ import annotations

import argparse
import enum
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Add scripts/ to path so governance can import federation_utils
_SCRIPTS = str(Path(__file__).resolve().parent)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from federation_utils import repo_from_git_remote  # noqa: E402
from governance._models import (  # noqa: E402
    BypassState,
    ComplianceStatus,
    Diagnostic,
    GovernanceCheck,
)
from governance._protection import ensure_governance_baseline, inspect_governance  # noqa: E402
from governance._repo import detect_repository  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


# ── Structured identity and result types ────────────────────────────────────


class IdentitySource(enum.Enum):
    """How the repository identity was determined."""

    REMOTE = "remote"       # Detected from git remote origin
    EXPLICIT = "explicit"   # From --repo CLI flag
    NONE = "none"           # Cannot be determined


class ReadmeResult(enum.Enum):
    """Outcome of the README identity block write."""

    INSERTED = "inserted"             # Block was added where none existed
    UPDATED = "updated"               # Existing block content was replaced
    UNCHANGED = "unchanged"           # Block exists and content is identical
    SKIPPED_MALFORMED = "skipped_malformed"  # Markers damaged, no write
    SKIPPED_NO_README = "skipped_no_readme"  # No README file (created new)


@dataclass
class SetupContext:
    """Resolved identity and safety flags for one setup invocation."""

    identity_source: IdentitySource
    allow_remote_writes: bool
    # When allow_remote_writes is False, topic and governance writes
    # are gated off regardless of CLI flags.


class TopicResult(enum.Enum):
    """Structured outcome of a topic registration attempt."""

    ALREADY_PRESENT = "already_present"
    ADDED = "added"
    SKIPPED_OFFLINE = "skipped_offline"
    SKIPPED_NO_GH = "skipped_no_gh"
    SKIPPED_NO_AUTH = "skipped_no_auth"
    SKIPPED_NO_PERMISSION = "skipped_no_permission"
    FAILED_READ = "failed_read"
    FAILED_WRITE = "failed_write"
    FAILED_POSTCONDITION = "failed_postcondition"


@dataclass
class TopicRegistration:
    """Complete result of a topic registration operation.

    *topics_after* is only set when a successful re-read was performed.
    When the re-read could not be executed, it is empty.
    """

    result: TopicResult
    repository: str
    topics_before: list[str]
    topics_after: list[str]
    message: str = ""
    remote_attempted: bool = False


@dataclass(frozen=True)
class SetupOutcome:
    """Aggregate result of one setup invocation.

    The *exit_code* is derived from the topic result and governance
    status according to the Gate-4 contract.
    """

    topic: TopicRegistration
    governance: ComplianceStatus
    local_materialization_complete: bool
    federation_registration_complete: bool
    exit_code: int


# ── Federation constants ──────────────────────────────────────────────────

AGENT_CITY_REPO = "kimeisele/agent-city"

CITY_ZONES = {
    "general": {"name": "General", "element": "Vayu (Air)", "description": "Communication & Networking"},
    "research": {"name": "Research", "element": "Jala (Water)", "description": "Knowledge & Philosophy"},
    "engineering": {"name": "Engineering", "element": "Prithvi (Earth)", "description": "Building & Tools"},
    "governance": {"name": "Governance", "element": "Agni (Fire)", "description": "Leadership & Policy"},
    "discovery": {"name": "Discovery", "element": "Akasha (Ether)", "description": "Abstract thought & Exploration"},
}

TIER_TO_ZONE = {
    "relay": "general",
    "contributor": "general",
    "research": "research",
    "service": "engineering",
    "governance": "governance",
}

# ── Tier definitions ──────────────────────────────────────────────────────

TIERS = {
    "relay": {
        "label": "Relay Node",
        "description": "Minimal presence — publish your charter, be discoverable, relay trust.",
        "produces": ["authority_document", "canonical_surface"],
        "consumes": [],
        "protocols": ["authority_feed_v1"],
        "capabilities": ["authority-publishing"],
    },
    "contributor": {
        "label": "Contributor Node",
        "description": "Active participant — publish documents, consume peer feeds, respond to inquiries.",
        "produces": ["authority_document", "canonical_surface", "public_summary"],
        "consumes": ["inquiry_request", "peer_review_challenge"],
        "protocols": ["authority_feed_v1", "open_inquiry_v1"],
        "capabilities": ["authority-publishing", "inquiry-response"],
    },
    "research": {
        "label": "Research Faculty",
        "description": "Knowledge producer — run research, publish findings, accept cross-domain inquiries.",
        "produces": ["authority_document", "research_synthesis", "cross_domain_report", "meta_analysis_report", "open_dataset"],
        "consumes": ["research_question", "raw_data_feed", "domain_observation", "inquiry_request", "peer_review_challenge"],
        "protocols": ["authority_feed_v1", "open_inquiry_v1", "peer_review_v1"],
        "capabilities": ["authority-publishing", "research-synthesis", "cross-domain-analysis", "open-inquiry"],
    },
    "service": {
        "label": "Service Node",
        "description": "Capability provider — offer tools, APIs, or agent services to the federation.",
        "produces": ["authority_document", "canonical_surface", "service_manifest"],
        "consumes": ["service_request", "capability_query"],
        "protocols": ["authority_feed_v1", "service_discovery_v1"],
        "capabilities": ["authority-publishing", "service-provider"],
    },
    "governance": {
        "label": "Governance Node",
        "description": "Policy and trust — participate in federation governance, propose policies, vote.",
        "produces": ["authority_document", "canonical_surface", "policy_proposal", "governance_record"],
        "consumes": ["policy_proposal", "vote_request", "governance_challenge"],
        "protocols": ["authority_feed_v1", "governance_v1"],
        "capabilities": ["authority-publishing", "governance-participation"],
    },
}

LAYER_MAP = {
    "relay": "node",
    "contributor": "node",
    "research": "node",
    "service": "node",
    "governance": "city",
}

# ── Domain catalog ────────────────────────────────────────────────────────

DOMAINS = {
    "energy": {"id": "energy-sustainability", "name": "Energy & Sustainability"},
    "health": {"id": "health-medicine", "name": "Health & Medicine"},
    "physics": {"id": "physics-fundamental", "name": "Physics & Fundamental Science"},
    "computation": {"id": "computation-intelligence", "name": "Computation & Intelligence"},
    "biology": {"id": "biology-ecology", "name": "Biology & Ecology"},
    "philosophy": {"id": "philosophy-ethics", "name": "Philosophy & Ethics"},
    "art": {"id": "art-creativity", "name": "Art & Creative Expression"},
    "education": {"id": "education-learning", "name": "Education & Learning"},
    "engineering": {"id": "engineering-building", "name": "Engineering & Building"},
    "economics": {"id": "economics-trade", "name": "Economics & Trade"},
}

# ── Interactive prompts ───────────────────────────────────────────────────


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {CYAN}{prompt}{suffix}{RESET}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return answer or default


def _ask_choice(prompt: str, options: dict[str, str], default: str = "") -> str:
    print(f"\n  {CYAN}{prompt}{RESET}")
    keys = list(options.keys())
    for i, (key, desc) in enumerate(options.items(), 1):
        marker = f" {DIM}(default){RESET}" if key == default else ""
        print(f"    {BOLD}{i}{RESET}. {key:15s} — {desc}{marker}")
    while True:
        raw = _ask("Choose (number or name)", default)
        if raw in options:
            return raw
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
        except ValueError:
            pass
        print(f"    {YELLOW}Please enter a valid option.{RESET}")


def _ask_multi(prompt: str, options: dict[str, str]) -> list[str]:
    print(f"\n  {CYAN}{prompt}{RESET}")
    keys = list(options.keys())
    for i, (key, desc) in enumerate(options.items(), 1):
        print(f"    {BOLD}{i}{RESET}. {key:15s} — {desc}")
    print(f"    {DIM}Enter numbers separated by commas, or 'none'{RESET}")
    raw = _ask("Select", "none")
    if raw.lower() == "none":
        return []
    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part in options:
            selected.append(part)
        else:
            try:
                idx = int(part) - 1
                if 0 <= idx < len(keys):
                    selected.append(keys[idx])
            except ValueError:
                pass
    return selected


def _ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    raw = _ask(f"{prompt} ({suffix})", "")
    if not raw:
        return default
    return raw.lower().startswith("y")


# ── File generators ───────────────────────────────────────────────────────


_BLOCK_BEGIN = "<!-- BEGIN FEDERATION NODE IDENTITY -->"
_BLOCK_END = "<!-- END FEDERATION NODE IDENTITY -->"


def _write_readme_identity(config: dict) -> ReadmeResult:
    """Insert or update the federation node identity block in README.md.

    Only content between the ``BEGIN`` / ``END`` markers is touched;
    everything outside the block is preserved byte-for-byte.

    Returns a :class:`ReadmeResult` indicating what happened.
    The result is only ``INSERTED`` or ``UPDATED`` when the postcondition
    is actually verified by re-reading the file.
    """
    readme_path = REPO_ROOT / "README.md"
    tier = TIERS[config["tier"]]
    display_name = config["display_name"]
    github_repo = config["github_repo"]

    block_lines = [
        _BLOCK_BEGIN,
        f"> **Node:** {display_name}",
        f"> **Repository:** {github_repo}",
        f"> **Tier:** {tier['label']}",
        f"> **Role:** {tier['description']}",
        ">  ",
        "> ℹ️ The content above is managed by `scripts/setup_node.py`.",
        "> The rest of this README is the generic federation-node handbook.",
        _BLOCK_END,
    ]
    block_text = "\n".join(block_lines)

    if not readme_path.exists():
        readme_path.write_text(
            block_text + "\n\n_No README content yet — add your documentation here._\n"
        )
        return ReadmeResult.SKIPPED_NO_README

    original = readme_path.read_text()

    begin_count = original.count(_BLOCK_BEGIN)
    end_count = original.count(_BLOCK_END)

    if begin_count == 0 and end_count == 0:
        # No markers — insert the block.
        lines = original.splitlines(keepends=True)
        result_lines: list[str] = []
        inserted = False
        for line in lines:
            result_lines.append(line)
            if not inserted and line.startswith("# "):
                result_lines.append("\n")
                result_lines.append(block_text)
                result_lines.append("\n")
                inserted = True
        if not inserted:
            # No H1 heading — prepend block at the top.
            result_lines = [block_text, "\n\n"] + result_lines
        readme_path.write_text("".join(result_lines))
        # Postcondition: re-read and verify the block was actually inserted.
        if _readme_identity_block_is_valid(
            readme_path.read_text(),
            display_name=display_name,
            github_repo=github_repo,
        ):
            return ReadmeResult.INSERTED
        # If postcondition fails, the file is in an unexpected state.
        # Do not claim success.
        return ReadmeResult.SKIPPED_MALFORMED

    if begin_count != 1 or end_count != 1:
        return ReadmeResult.SKIPPED_MALFORMED

    begin_idx = original.index(_BLOCK_BEGIN)
    end_idx = original.index(_BLOCK_END)
    if end_idx < begin_idx:
        return ReadmeResult.SKIPPED_MALFORMED

    # Replace existing block content
    before_block = original[:begin_idx]
    after_block = original[end_idx + len(_BLOCK_END):]
    new_content = before_block + block_text + after_block
    if new_content == original:
        return ReadmeResult.UNCHANGED
    readme_path.write_text(new_content)
    # Postcondition: re-read and verify.
    if _readme_identity_block_is_valid(
        readme_path.read_text(),
        display_name=display_name,
        github_repo=github_repo,
    ):
        return ReadmeResult.UPDATED
    return ReadmeResult.SKIPPED_MALFORMED


def _readme_identity_block_is_valid(
    content: str,
    *,
    display_name: str,
    github_repo: str,
) -> bool:
    """Verify that *content* contains exactly one valid identity block.

    Checks marker counts, ordering, and expected identity values.
    """
    begin_count = content.count(_BLOCK_BEGIN)
    end_count = content.count(_BLOCK_END)
    if begin_count != 1 or end_count != 1:
        return False
    begin_idx = content.index(_BLOCK_BEGIN)
    end_idx = content.index(_BLOCK_END)
    if end_idx < begin_idx:
        return False
    block_content = content[begin_idx:end_idx + len(_BLOCK_END)]
    if display_name not in block_content:
        return False
    if github_repo not in block_content:
        return False
    return True


def _write_charter(config: dict) -> None:
    charter_path = REPO_ROOT / "docs" / "authority" / "charter.md"
    name = config["display_name"]
    description = config["description"]
    tier = TIERS[config["tier"]]
    zone = CITY_ZONES.get(config.get("city_zone", ""), {})

    lines = [
        f"# {name} Charter",
        "",
        f"> {description}",
        "",
        "## Role",
        "",
        f"This node operates as a **{tier['label']}** in the agent-internet federation.",
        "",
    ]

    if zone:
        lines.extend([
            "## City Zone",
            "",
            f"Registered in the **{zone['name']}** zone ({zone['element']}) — {zone['description']}.",
            "",
        ])

    if config.get("domains"):
        lines.extend(["## Domains", ""])
        for d in config["domains"]:
            lines.append(f"- **{DOMAINS[d]['name']}**")
        lines.append("")

    if config.get("values"):
        lines.extend(["## Values", "", config["values"], ""])

    lines.extend([
        "## Federation Commitment",
        "",
        "This node commits to the federation's core principles:",
        "- Publish truthful, verifiable authority documents",
        "- Respect boundary separation (substrate / world / city / membrane)",
        "- Participate in peer review and trust verification",
        "",
    ])

    charter_path.write_text("\n".join(lines))


def _write_capabilities(config: dict) -> None:
    caps_path = REPO_ROOT / "docs" / "authority" / "capabilities.json"
    tier = TIERS[config["tier"]]

    skills = [{"id": cap, "name": cap.replace("-", " ").title(), "description": f"{cap.replace('-', ' ').title()} capability."} for cap in tier["capabilities"]]

    for skill in config.get("custom_skills", []):
        skills.append({"id": skill.lower().replace(" ", "-"), "name": skill, "description": f"{skill} capability."})

    manifest: dict = {
        "kind": "agent_capability_manifest",
        "version": 1,
        "node_id": config["repo_name"],
        "node_role": config.get("role_id", config["tier"]),
        "display_name": config["display_name"],
        "description": config["description"],
        "skills": skills,
        "capabilities": {},
        "federation_interfaces": {
            "produces": tier["produces"],
            "consumes": tier["consumes"],
            "protocols": tier["protocols"],
        },
        "protocols": [
            {"name": "agent-federation", "version": 1, "descriptor": ".well-known/agent-federation.json"},
            {"name": "a2a-agent-card", "version": "1.0.0", "descriptor": ".well-known/agent.json"},
        ],
    }

    if config.get("city_zone"):
        manifest["city"] = {
            "zone": config["city_zone"],
            "element": CITY_ZONES[config["city_zone"]]["element"],
        }

    if config.get("domains"):
        manifest["faculties"] = [DOMAINS[d] for d in config["domains"]]

    caps_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _write_peer_json(config: dict) -> None:
    """Write NADI peer descriptor with the configured node identity."""
    peer_dir = REPO_ROOT / "data" / "federation"
    peer_dir.mkdir(parents=True, exist_ok=True)
    peer_path = peer_dir / "peer.json"

    repo = config.get("github_repo", f"kimeisele/{config['repo_name']}")
    node_id = config["repo_name"]
    tier = TIERS[config["tier"]]

    peer_data = {
        "identity": {
            "city_id": node_id,
            "slug": node_id,
            "repo": repo,
            "public_key": "",
        },
        "endpoint": {
            "city_id": node_id,
            "transport": "filesystem",
            "location": "data/federation",
        },
        "capabilities": tier["capabilities"],
        "nadi": {
            "outbox": "data/federation/nadi_outbox.json",
            "inbox": "data/federation/nadi_inbox.json",
            "reports": "data/federation/reports/",
            "directives": "data/federation/directives/",
        },
    }

    # Preserve existing identity fields that were set externally (e.g. public_key)
    existing_identity: dict = {}
    if peer_path.exists():
        try:
            existing = json.loads(peer_path.read_text())
            if isinstance(existing, dict) and "identity" in existing:
                existing_identity = existing.get("identity", {})
        except json.JSONDecodeError:
            # Corrupt peer.json — warn but do not overwrite.
            print(
                f"    {YELLOW}warning: {peer_path} is not valid JSON. "
                f"Skipping peer.json update to preserve existing data.{RESET}"
            )
            return

    # Merge: keep existing public_key, update config-derived fields
    if existing_identity and existing_identity.get("public_key"):
        peer_data["identity"]["public_key"] = existing_identity["public_key"]

    peer_path.write_text(json.dumps(peer_data, indent=2) + "\n")

    # Ensure inbox/outbox exist without corrupting existing data
    for fname in ("nadi_inbox.json", "nadi_outbox.json"):
        fpath = peer_dir / fname
        if not fpath.exists():
            fpath.write_text("[]\n")
        elif fpath.read_text().strip() == "":
            fpath.write_text("[]\n")
        else:
            # File exists with content — validate it is a JSON array
            try:
                data = json.loads(fpath.read_text())
                if not isinstance(data, list):
                    print(
                        f"    {YELLOW}warning: {fpath} is not a JSON array. "
                        f"Not modified.{RESET}"
                    )
            except json.JSONDecodeError:
                print(
                    f"    {YELLOW}warning: {fpath} is not valid JSON. "
                    f"Not modified — please repair manually.{RESET}"
                )

    # Ensure subdirectories exist
    (peer_dir / "reports").mkdir(exist_ok=True)
    (peer_dir / "directives").mkdir(exist_ok=True)


def _regenerate(config: dict) -> None:
    repo = config.get("github_repo", f"kimeisele/{config['repo_name']}")
    layer = LAYER_MAP.get(config["tier"], "node")
    for script, extra_args in [
        ("render_federation_descriptor.py", ["--repo", repo, "--layer", layer]),
        ("render_agent_card.py", ["--repo", repo]),
    ]:
        result = subprocess.run(
            [sys.executable, f"scripts/{script}", *extra_args],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"    {YELLOW}warning: {script} failed: {result.stderr.strip()[:80]}{RESET}")


def _read_repository_topics(repo_full_name: str) -> tuple[list[str] | None, str | None]:
    """Read the current topic list for *repo_full_name* via ``gh`` CLI.

    Returns ``(topics, None)`` on success, ``(None, error_class)`` on
    failure where *error_class* is one of ``"no_gh"``, ``"auth"``,
    ``"timeout"``, ``"failed"``.
    """
    try:
        result = subprocess.run(
            ["gh", "repo", "view", repo_full_name,
             "--json", "repositoryTopics"],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        return None, "no_gh"
    except subprocess.TimeoutExpired:
        return None, "timeout"

    if result.returncode != 0:
        stderr_lower = (result.stderr or "").lower()
        if any(w in stderr_lower for w in
               ("authentication", "not logged in", "401", "unauthorized")):
            return None, "auth"
        if any(w in stderr_lower for w in
               ("403", "forbidden", "permission", "resource not accessible")):
            return None, "auth"
        return None, "failed"

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "failed"
    topics = data.get("repositoryTopics", [])
    if not isinstance(topics, list):
        return None, "failed"
    return (
        sorted({t["name"] for t in topics if isinstance(t, dict) and "name" in t}),
        None,
    )


def _classify_write_error(
    exc: Exception | None, stderr: str
) -> "TopicResult":
    """Classify a topic write failure into a TopicResult."""
    if isinstance(exc, FileNotFoundError):
        return TopicResult.SKIPPED_NO_GH
    if isinstance(exc, subprocess.TimeoutExpired):
        return TopicResult.FAILED_WRITE
    stderr_lower = (stderr or "").lower()
    if any(w in stderr_lower for w in
           ("authentication", "not logged in", "401", "unauthorized")):
        return TopicResult.SKIPPED_NO_AUTH
    if any(w in stderr_lower for w in
           ("403", "forbidden", "permission", "resource not accessible")):
        return TopicResult.SKIPPED_NO_PERMISSION
    return TopicResult.FAILED_WRITE


def _register_federation_topic(
    repo_full_name: str,
    *,
    allow_remote_writes: bool,
) -> TopicRegistration:
    """Ensure ``agent-federation-node`` is present among the repository topics.

    Reads existing topics, adds the federation topic if missing, and
    verifies the result via re-read.  Existing topics are never removed.

    When *allow_remote_writes* is ``False``, no remote operations are
    attempted and ``SKIPPED_OFFLINE`` is returned.
    """
    TOPIC = "agent-federation-node"

    if not allow_remote_writes:
        return TopicRegistration(
            result=TopicResult.SKIPPED_OFFLINE,
            repository=repo_full_name,
            topics_before=[],
            topics_after=[],
            message="Topic registration skipped: LOCAL/OFFLINE MODE",
            remote_attempted=False,
        )

    # 1. Read existing topics
    topics_before, read_error = _read_repository_topics(repo_full_name)
    if topics_before is None:
        err_map = {
            "no_gh": (TopicResult.SKIPPED_NO_GH,
                       "gh CLI not found. Install GitHub CLI and run: "
                       "gh auth login"),
            "auth": (TopicResult.SKIPPED_NO_AUTH,
                      "Not authenticated. Run: gh auth login"),
            "timeout": (TopicResult.FAILED_READ,
                         "Timed out reading repository topics."),
            "failed": (TopicResult.FAILED_READ,
                        "Could not read repository topics."),
        }
        result_type, msg = err_map.get(
            read_error, (TopicResult.FAILED_READ, "Could not read topics."))
        return TopicRegistration(
            result=result_type,
            repository=repo_full_name,
            topics_before=[],
            topics_after=[],
            message=msg,
            remote_attempted=(read_error != "no_gh"),
        )

    # 2. Already present → no write needed
    if TOPIC in topics_before:
        return TopicRegistration(
            result=TopicResult.ALREADY_PRESENT,
            repository=repo_full_name,
            topics_before=list(topics_before),
            topics_after=list(topics_before),
            message=f"Topic '{TOPIC}' already present",
            remote_attempted=False,
        )

    # 3. Add the topic via gh --add-topic (safe, preserves existing)
    write_exc: Exception | None = None
    try:
        result = subprocess.run(
            ["gh", "repo", "edit", repo_full_name, "--add-topic", TOPIC],
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError as exc:
        write_exc = exc
        result = None
    except subprocess.TimeoutExpired as exc:
        write_exc = exc
        result = None

    if write_exc is not None or (result is not None and result.returncode != 0):
        stderr_text = ""
        if result is not None:
            stderr_text = (result.stderr or "").strip()[:120]
        classified = _classify_write_error(write_exc, stderr_text)
        manual_cmd = (
            f"gh repo edit {repo_full_name} --add-topic {TOPIC}"
        )
        return TopicRegistration(
            result=classified,
            repository=repo_full_name,
            topics_before=list(topics_before),
            topics_after=list(topics_before),
            message=(
                f"Failed to add topic '{TOPIC}'. "
                f"Manually run: {manual_cmd}"
                + (f" ({stderr_text})" if stderr_text else "")
            ),
            remote_attempted=True,
        )

    # 4. Re-read postcondition
    topics_after, _ = _read_repository_topics(repo_full_name)
    if topics_after is None:
        return TopicRegistration(
            result=TopicResult.FAILED_POSTCONDITION,
            repository=repo_full_name,
            topics_before=list(topics_before),
            topics_after=[],
            message=(
                f"Topic '{TOPIC}' was written but re-read failed. "
                f"Check: gh repo view {repo_full_name} --json repositoryTopics"
            ),
            remote_attempted=True,
        )

    # Full preservation check: all before-topics must survive
    lost = sorted(set(topics_before) - set(topics_after))
    if lost:
        return TopicRegistration(
            result=TopicResult.FAILED_POSTCONDITION,
            repository=repo_full_name,
            topics_before=list(topics_before),
            topics_after=list(topics_after),
            message=(
                f"Topic write postcondition failed; "
                f"existing topics disappeared: {', '.join(lost)}"
            ),
            remote_attempted=True,
        )

    if TOPIC in topics_after:
        return TopicRegistration(
            result=TopicResult.ADDED,
            repository=repo_full_name,
            topics_before=list(topics_before),
            topics_after=list(topics_after),
            message=f"Topic '{TOPIC}' added successfully",
            remote_attempted=True,
        )

    return TopicRegistration(
        result=TopicResult.FAILED_POSTCONDITION,
        repository=repo_full_name,
        topics_before=list(topics_before),
        topics_after=list(topics_after),
        message=(
            f"Write succeeded but re-read did not confirm '{TOPIC}'. "
            f"Check: gh repo view {repo_full_name} --json repositoryTopics"
        ),
        remote_attempted=True,
    )


# ── Main wizard ───────────────────────────────────────────────────────────


def interactive_setup() -> tuple[dict | None, SetupContext]:
    """Run the interactive setup wizard.

    Returns ``(config, ctx)`` where *config* is ``None`` if the user
    aborted or identity could not be resolved.
    """
    print(f"\n{BOLD}{'═' * 60}{RESET}")
    print(f"{BOLD}  Federation Node Setup Wizard{RESET}")
    print(f"{BOLD}{'═' * 60}{RESET}")
    print(f"\n  {DIM}Two phases: Identity → Connect to Federation{RESET}")
    print(f"  {DIM}The core kernel is always included.{RESET}\n")

    # ── Phase 1: Identity ──
    print(f"{BOLD}═══ Phase 1: Identity ═══{RESET}\n")

    display_name = _ask("Node name", "My Federation Node")

    # Resolve repository identity from the actual git remote.
    detected_repo = repo_from_git_remote(REPO_ROOT)
    if detected_repo:
        print(f"\n  {DIM}Detected repository: {BOLD}{detected_repo}{RESET}")
        if not _ask_yn(f"Use detected repository {detected_repo}?", default=True):
            print(f"\n  {YELLOW}Setup aborted. Re-run with --repo for offline mode.{RESET}")
            return None, SetupContext(
                identity_source=IdentitySource.REMOTE,
                allow_remote_writes=True,
            )
        github_repo = detected_repo
        repo_name = detected_repo.split("/", 1)[1]
        ctx = SetupContext(
            identity_source=IdentitySource.REMOTE,
            allow_remote_writes=True,
        )
    else:
        print(f"\n  {YELLOW}No git remote found.{RESET}")
        print(f"  {DIM}Interactive setup requires a git remote.{RESET}")
        print(f"  {DIM}Use --repo with --non-interactive for offline mode.{RESET}")
        return None, SetupContext(
            identity_source=IdentitySource.NONE,
            allow_remote_writes=False,
        )

    description = _ask("One-line description", f"{display_name} — a federation node")

    tier = _ask_choice(
        "What kind of node do you want to run?",
        {k: v["description"] for k, v in TIERS.items()},
        default="relay",
    )

    domains: list[str] = []
    if tier in ("research", "contributor"):
        domains = _ask_multi(
            "Which domains does your node cover?",
            {k: v["name"] for k, v in DOMAINS.items()},
        )

    custom_skills: list[str] = []
    if _ask_yn("Add custom capabilities beyond the defaults?", default=False):
        raw = _ask("List capabilities (comma-separated)", "")
        custom_skills = [s.strip() for s in raw.split(",") if s.strip()]

    values = ""
    if _ask_yn("Add a values statement to your charter?", default=False):
        values = _ask("Your values (one paragraph)", "")

    role_id = _ask("Node role identifier", f"{repo_name.replace('-', '_')}_{tier}")

    # ── Phase 2: Federation connection ──
    print(f"\n{BOLD}═══ Phase 2: Connect to Federation ═══{RESET}\n")

    default_zone = TIER_TO_ZONE.get(tier, "general")
    city_zone = _ask_choice(
        "Which Agent City zone fits your node?",
        {k: f"{v['element']} — {v['description']}" for k, v in CITY_ZONES.items()},
        default=default_zone,
    )

    return {
        "display_name": display_name,
        "repo_name": repo_name,
        "github_repo": github_repo,
        "description": description,
        "tier": tier,
        "domains": domains,
        "custom_skills": custom_skills,
        "values": values,
        "role_id": role_id,
        "city_zone": city_zone,
    }, ctx


def apply_config(
    config: dict,
    *,
    ctx: SetupContext,
    interactive: bool,
    apply_governance: bool,
) -> SetupOutcome:
    tier = TIERS[config["tier"]]
    zone = CITY_ZONES.get(config.get("city_zone", ""), {})

    # ── Show mode banner ──
    if not ctx.allow_remote_writes:
        print(
            f"\n{BOLD}── {YELLOW}LOCAL / OFFLINE MODE{RESET}{BOLD} ——{RESET}"
        )
        print(
            f"  {DIM}Remote writes (topic, governance) are DISABLED."
            f"  Local files only.{RESET}"
        )

    print(f"\n{BOLD}── Phase 1: Writing Local Config ──{RESET}\n")
    print(f"  Node:     {GREEN}{config['display_name']}{RESET}")
    print(f"  Repo:     {config['github_repo']}")
    print(f"  Tier:     {tier['label']}")
    print(f"  Layer:    {LAYER_MAP.get(config['tier'], 'node')}")
    if zone:
        print(f"  Zone:     {zone['name']} ({zone['element']})")
    print(f"  Produces: {', '.join(tier['produces'])}")
    print(f"  Consumes: {', '.join(tier['consumes']) or '(none yet)'}")
    if config.get("domains"):
        print(f"  Domains:  {', '.join(DOMAINS[d]['name'] for d in config['domains'])}")
    print()

    _write_charter(config)
    print(f"    {GREEN}✓{RESET} docs/authority/charter.md")

    _write_capabilities(config)
    print(f"    {GREEN}✓{RESET} docs/authority/capabilities.json")

    readme_result = _write_readme_identity(config)
    _print_readme_result(readme_result)

    _regenerate(config)
    print(f"    {GREEN}✓{RESET} .well-known/agent-federation.json")
    print(f"    {GREEN}✓{RESET} .well-known/agent.json")

    # ── Phase 2: Federation connection ──
    print(f"\n{BOLD}── Phase 2: Connecting to Federation ──{RESET}\n")

    # NADI peer descriptor + inbox/outbox
    _write_peer_json(config)
    print(f"    {GREEN}✓{RESET} data/federation/peer.json (NADI identity)")
    print(f"    {GREEN}✓{RESET} data/federation/nadi_inbox.json")
    print(f"    {GREEN}✓{RESET} data/federation/nadi_outbox.json")

    # Peer discovery
    result = subprocess.run(
        [sys.executable, "scripts/discover_federation_peers.py", "--seeds-only",
         "--output", ".federation/peers.json"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    if result.returncode == 0:
        peers_path = REPO_ROOT / ".federation" / "peers.json"
        if peers_path.exists():
            peers = json.loads(peers_path.read_text())
            count = peers.get("peer_count", 0)
            print(f"    {GREEN}✓{RESET} Discovered {count} federation peer(s)")
            for peer in peers.get("peers", [])[:5]:
                desc = peer.get("federation_descriptor", {})
                name = desc.get("display_name", peer.get("full_name", "?"))
                print(f"      · {name}")
            if count > 5:
                print(f"      … and {count - 5} more")
    else:
        print(f"    {YELLOW}Could not reach peers (offline?){RESET}")

    # Save config
    config_path = REPO_ROOT / ".federation-setup.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    print(f"    {GREEN}✓{RESET} .federation-setup.json (re-run anytime)")

    # Agent City registration guidance
    print(f"\n{BOLD}── Agent City Registration ──{RESET}\n")
    if zone:
        print(f"  Your node belongs in the {GREEN}{zone['name']}{RESET} zone ({zone['element']}).")
    print("  Register manually when ready:")
    print(f"    {CYAN}https://github.com/{AGENT_CITY_REPO}/issues/new?template=agent-registration.yml{RESET}")

    # Federation topic (gated)
    topic_reg = _register_federation_topic(
        config["github_repo"],
        allow_remote_writes=ctx.allow_remote_writes,
    )
    _print_topic_result(topic_reg)

    # ── Governance: branch protection baseline ──
    governance_exit = _run_governance_step(
        interactive=interactive,
        apply_governance=apply_governance and ctx.allow_remote_writes,
    )

    # Completion status
    federation_complete = (
        topic_reg.result in (TopicResult.ALREADY_PRESENT, TopicResult.ADDED)
        and ctx.allow_remote_writes
        and governance_exit == ComplianceStatus.CONFORMANT
    )

    # Next steps
    print(f"\n{BOLD}── Next Steps ──{RESET}\n")
    print(f"  1. Review your charter:  {CYAN}docs/authority/charter.md{RESET}")
    print(f"  2. Create setup branch:  {CYAN}git checkout -b setup-federation-node{RESET}")
    print(f"  3. Commit files:         {CYAN}git add -A && git commit -m 'Initialize federation node'{RESET}")
    print(f"  4. Push branch:          {CYAN}git push -u origin setup-federation-node{RESET}")
    print(f"  5. Open Pull Request:    {CYAN}(PR from setup-federation-node → main){RESET}")
    if topic_reg.result == TopicResult.ALREADY_PRESENT:
        print(f"  6. Topic:                {GREEN}agent-federation-node ✓ (already present){RESET}")
    elif topic_reg.result == TopicResult.ADDED:
        print(f"  6. Topic:                {GREEN}agent-federation-node ✓ (added){RESET}")
    elif topic_reg.result == TopicResult.SKIPPED_OFFLINE:
        print(f"  6. Add the topic:        {CYAN}gh repo edit --add-topic agent-federation-node{RESET}  {DIM}(once pushed){RESET}")
    else:
        print(f"  6. Add the topic:        {CYAN}gh repo edit --add-topic agent-federation-node{RESET}")
    print(f"  7. Register with city:   {CYAN}(link above){RESET}")
    print("  8. Review + merge PR")
    print(f"  9. Start NADI daemon:    {CYAN}python scripts/nadi_daemon.py --once{RESET}")
    print(f" 10. Send a message:       {CYAN}python scripts/nadi_send.py --to agent-internet --op heartbeat{RESET}")
    print(f"\n  Re-run: {CYAN}python scripts/setup_node.py{RESET}  |  Status: {CYAN}python scripts/setup_node.py --status{RESET}")
    print(f"  Apply governance: {CYAN}python scripts/setup_node.py --apply-governance{RESET}")

    # Compute exit code from topic + governance
    topic_ok = topic_reg.result in (TopicResult.ALREADY_PRESENT, TopicResult.ADDED)
    topic_failed = ctx.allow_remote_writes and not topic_ok and \
        topic_reg.result != TopicResult.SKIPPED_OFFLINE

    exit_code = 0
    if topic_failed:
        exit_code = 1
    elif apply_governance and governance_exit != ComplianceStatus.CONFORMANT:
        exit_code = 1

    outcome = SetupOutcome(
        topic=topic_reg,
        governance=governance_exit,
        local_materialization_complete=True,  # local files always written
        federation_registration_complete=federation_complete,
        exit_code=exit_code,
    )

    # Final status banner
    print()
    if federation_complete:
        print(f"{BOLD}{'─' * 40}{RESET}")
        print(f"{GREEN}{BOLD}  FEDERATION REGISTRATION COMPLETE{RESET}")
        print(f"{BOLD}{'─' * 40}{RESET}")
    elif not ctx.allow_remote_writes:
        print(f"{BOLD}{'─' * 40}{RESET}")
        print(f"{BOLD}  LOCAL MATERIALIZATION COMPLETE{RESET}")
        print(f"{DIM}  (remote registration requires a pushed repository){RESET}")
        print(f"{BOLD}{'─' * 40}{RESET}")
    else:
        print(f"{BOLD}{'─' * 40}{RESET}")
        print(f"{YELLOW}{BOLD}  LOCAL MATERIALIZATION COMPLETE{RESET}")
        print(f"{DIM}  (remote registration incomplete — see topic status above){RESET}")
        print(f"{BOLD}{'─' * 40}{RESET}")
    print()
    return outcome


def _print_topic_result(reg: TopicRegistration) -> None:
    """Print the topic registration result with appropriate status."""
    print()
    if reg.result == TopicResult.ALREADY_PRESENT:
        print(f"  {GREEN}✓{RESET} Topic agent-federation-node already present")
    elif reg.result == TopicResult.ADDED:
        print(f"  {GREEN}✓{RESET} Topic agent-federation-node added")
        print(f"  {DIM}Topics before: {', '.join(reg.topics_before) or '(none)'}{RESET}")
        print(f"  {DIM}Topics after:  {', '.join(reg.topics_after)}{RESET}")
    elif reg.result == TopicResult.SKIPPED_OFFLINE:
        print(f"  {DIM}── Topic registration skipped (local/offline mode) ──{RESET}")
    elif reg.result == TopicResult.SKIPPED_NO_GH:
        print(f"  {YELLOW}!{RESET} Topic registration skipped — {reg.message}")
    elif reg.result == TopicResult.SKIPPED_NO_AUTH:
        print(f"  {YELLOW}!{RESET} Topic registration skipped — {reg.message}")
    elif reg.result == TopicResult.SKIPPED_NO_PERMISSION:
        print(f"  {YELLOW}!{RESET} {reg.message}")
    elif reg.result in (TopicResult.FAILED_READ, TopicResult.FAILED_WRITE,
                        TopicResult.FAILED_POSTCONDITION):
        print(f"  {YELLOW}!{RESET} {reg.message}")


def _print_readme_result(result: ReadmeResult) -> None:
    """Print the appropriate status line for a ReadmeResult."""
    if result == ReadmeResult.INSERTED:
        print(f"    {GREEN}✓{RESET} README.md (identity block inserted)")
    elif result == ReadmeResult.UPDATED:
        print(f"    {GREEN}✓{RESET} README.md (identity block updated)")
    elif result == ReadmeResult.UNCHANGED:
        print(f"    {GREEN}✓{RESET} README.md (identity block unchanged)")
    elif result == ReadmeResult.SKIPPED_NO_README:
        print(f"    {GREEN}✓{RESET} README.md (created with identity block)")
    elif result == ReadmeResult.SKIPPED_MALFORMED:
        print(
            f"    {YELLOW}✗{RESET} README.md ({YELLOW}skipped{RESET} — "
            f"identity block markers are malformed; fix manually)"
        )


def _run_governance_step(*, interactive: bool, apply_governance: bool) -> ComplianceStatus:
    """Run the governance inspection and optionally apply the baseline.

    *interactive* controls whether the user may be prompted (``_ask_yn``).
    When ``False`` no stdin read occurs; the step is strictly non-blocking.

    *apply_governance* controls whether a write (POST) may be issued.
    When ``False`` the step is strictly read-only.

    Remote writes are ONLY allowed when:
      - ``apply_governance`` is ``True`` explicitly, OR
      - ``interactive`` is ``True`` AND the user confirms via ``_ask_yn``.

    Returns the final ComplianceStatus for exit-code decisions.
    """
    print(f"\n{BOLD}── Governance: Branch Protection Baseline ──{RESET}\n")

    repo, diag = detect_repository(REPO_ROOT)
    if repo is None:
        _print_governance_diag(diag)
        return ComplianceStatus.UNKNOWN

    print(f"  Repository:     {repo.full_name}")
    print(f"  Default Branch: {repo.default_branch}")

    check = inspect_governance(repo)
    _print_governance_check(check)

    if check.compliance == ComplianceStatus.CONFORMANT:
        return ComplianceStatus.CONFORMANT

    # Determine whether we are allowed to write
    may_write = apply_governance

    if not may_write and interactive and check.compliance == ComplianceStatus.NON_CONFORMANT:
        # Interactive mode: ask user before writing
        print("\n  The federation-baseline ruleset is not yet active on this repository.")
        if _ask_yn("  Create the 'agent-federation-baseline-v1' ruleset now?", default=True):
            may_write = True
        else:
            print(f"\n  {YELLOW}Skipped. Run with --apply-governance to set up later.{RESET}")

    if not may_write:
        if check.compliance == ComplianceStatus.NON_CONFORMANT and not interactive:
            print(f"\n  {YELLOW}Run with --apply-governance to set up branch protection.{RESET}")
        return check.compliance

    # Apply governance (only reached if may_write is True)
    print("\n  Applying federation-baseline ruleset...")
    result = ensure_governance_baseline(repo, check)
    print(f"  Action: {GREEN}{result.action or 'none'}{RESET}")

    # Display apply-step diagnostics
    for d in result.diagnostics:
        print(f"  {YELLOW}Diagnostic: {d.value}{RESET}")
    for detail in result.details:
        print(f"  {DIM}{detail}{RESET}")

    if result.final_check is not None:
        print()
        _print_governance_check(result.final_check)
        return result.final_check.compliance

    # No final_check → action failed completely
    print(f"\n  {YELLOW}Could not apply baseline.{RESET}")
    return ComplianceStatus.UNKNOWN


def _print_governance_check(check: GovernanceCheck) -> None:
    """Display a GovernanceCheck result to the user."""
    status_map = {
        ComplianceStatus.CONFORMANT: f"{GREEN}conformant{RESET}",
        ComplianceStatus.NON_CONFORMANT: f"{YELLOW}non-conformant{RESET}",
        ComplianceStatus.UNKNOWN: f"{YELLOW}unknown{RESET}",
    }
    print(f"  Compliance:      {status_map[check.compliance]}")

    if check.present_rules:
        print(f"  Present rules:   {', '.join(check.present_rules)}")
    if check.missing_rules:
        print(f"  Missing rules:   {YELLOW}{', '.join(check.missing_rules)}{RESET}")
    if check.unknown_rules:
        print(f"  Unknown rules:   {YELLOW}{', '.join(check.unknown_rules)}{RESET}")

    bypass_map = {
        BypassState.NONE_CONFIRMED: f"{GREEN}none confirmed{RESET}",
        BypassState.PRESENT: f"{YELLOW}present{RESET}",
        BypassState.UNKNOWN: f"{YELLOW}unknown{RESET}",
    }
    print(f"  Bypass actors:   {bypass_map[check.bypass_state]}")

    for detail in check.details:
        print(f"  {DIM}{detail}{RESET}")

    for d in check.diagnostics:
        print(f"  {YELLOW}Diagnostic: {d.value}{RESET}")


def _print_governance_diag(diag: Diagnostic) -> None:
    """Display a repository-detection diagnostic."""
    messages = {
        Diagnostic.REPO_NOT_FOUND: "No GitHub repository detected from git remote.",
        Diagnostic.AUTH_MISSING: "GitHub authentication missing. Set GITHUB_TOKEN, GH_TOKEN, or run 'gh auth login'.",
        Diagnostic.GITHUB_UNREACHABLE: "Could not reach GitHub API. Check network connectivity.",
    }
    msg = messages.get(diag, f"Could not evaluate governance: {diag.value}")
    print(f"  {YELLOW}{msg}{RESET}")


def show_status() -> ComplianceStatus | None:
    """Show federation status from saved config.

    Returns the governance ComplianceStatus for exit-code decisions,
    or ``None`` if no setup config exists.
    """
    config_path = REPO_ROOT / ".federation-setup.json"
    if not config_path.exists():
        print(f"  {YELLOW}No setup config found. Run: python scripts/setup_node.py{RESET}")
        return None

    config = json.loads(config_path.read_text())

    print(f"\n{BOLD}── Federation Status ──{RESET}\n")
    print(f"  Node:  {GREEN}{config.get('display_name', '?')}{RESET}")
    print(f"  Tier:  {TIERS.get(config.get('tier', ''), {}).get('label', '?')}")

    zone = CITY_ZONES.get(config.get("city_zone", ""), {})
    if zone:
        print(f"  Zone:  {zone['name']} ({zone['element']})")

    # Peer check
    _ = subprocess.run(
        [sys.executable, "scripts/discover_federation_peers.py", "--seeds-only",
         "--output", ".federation/peers.json"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    peers_path = REPO_ROOT / ".federation" / "peers.json"
    if peers_path.exists():
        peers = json.loads(peers_path.read_text())
        count = peers.get("peer_count", 0)
        print(f"  Peers: {GREEN}{count} reachable{RESET}")

    # Governance status
    print(f"\n{BOLD}── Governance: Branch Protection ──{RESET}\n")
    repo, diag = detect_repository(REPO_ROOT)
    if repo is None:
        _print_governance_diag(diag)
        print()
        return ComplianceStatus.UNKNOWN

    print(f"  Repository:     {repo.full_name}")
    print(f"  Default Branch: {repo.default_branch}")
    check = inspect_governance(repo)
    _print_governance_check(check)

    print()
    return check.compliance


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive federation node setup")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--status", action="store_true", help="Show federation and governance status")
    parser.add_argument("--apply-governance", action="store_true",
                        help="Apply federation branch-protection baseline (can be used alone or with --non-interactive)")
    parser.add_argument("--name", default="My Federation Node")
    parser.add_argument("--role", default="relay", choices=list(TIERS.keys()))
    parser.add_argument("--org", default="kimeisele")
    parser.add_argument("--repo", default=None,
                        help="Explicit owner/repo override (offline/test only). "
                             "When omitted, the git remote is authoritative.")
    parser.add_argument("--zone", default="", choices=[""] + list(CITY_ZONES.keys()))
    parser.add_argument("--description", default="")
    args = parser.parse_args()

    # ── --status: read-only inspection ──
    if args.status:
        status = show_status()
        if status is None:
            return 2  # no config
        if status == ComplianceStatus.CONFORMANT:
            return 0
        if status == ComplianceStatus.NON_CONFORMANT:
            return 1
        return 2  # UNKNOWN

    # ── --apply-governance alone: targeted governance run ──
    if args.apply_governance and not args.non_interactive and not any([
        args.name != "My Federation Node", args.role != "relay",
        args.org != "kimeisele", args.zone != "", args.description != "",
    ]):
        # Standalone governance run — load repo info from saved config
        return _run_governance_standalone()

    # ── Normal setup flow ──
    ctx: SetupContext
    if args.non_interactive:
        ctx, config = _resolve_non_interactive(args)
        if config is None:
            # Identity resolution failed — ctx carries the error.
            return 1
    else:
        config, ctx = interactive_setup()
        if config is None:
            # User aborted or identity not resolvable.
            return 1

    outcome = apply_config(
        config,
        ctx=ctx,
        interactive=not args.non_interactive,
        apply_governance=args.apply_governance,
    )
    return outcome.exit_code


def _resolve_non_interactive(args) -> tuple[SetupContext, dict | None]:
    """Resolve identity for the non-interactive path.

    Returns ``(ctx, config)`` where *config* is ``None`` on failure.
    """
    if args.repo:
        github_repo = args.repo
        repo_name = args.repo.split("/", 1)[1]

        # Check whether --repo matches the actual git remote
        detected = repo_from_git_remote(REPO_ROOT)
        if detected and detected != github_repo:
            print(
                f"error: --repo {github_repo} conflicts with detected git "
                f"remote {detected}. Refusing to proceed.",
                file=sys.stderr,
            )
            return (
                SetupContext(identity_source=IdentitySource.EXPLICIT,
                             allow_remote_writes=False),
                None,
            )

        # --repo without git remote → local/offline mode
        allow_remote = detected is not None and detected == github_repo
        ctx = SetupContext(
            identity_source=IdentitySource.EXPLICIT,
            allow_remote_writes=allow_remote,
        )
        if not allow_remote:
            print(
                f"\n  {YELLOW}── LOCAL / OFFLINE MODE ──{RESET}"
            )
            print(
                f"  {DIM}--repo {github_repo} cannot be verified against "
                f"a git remote. Local files will be generated, but remote "
                f"writes (topic, governance) are disabled.{RESET}"
            )
    else:
        detected = repo_from_git_remote(REPO_ROOT)
        if not detected:
            print(
                "error: cannot determine repository identity. "
                "No git remote found and --repo not specified. "
                "Run from a git checkout with a GitHub remote, "
                "or use --repo for offline/local materialization.",
                file=sys.stderr,
            )
            return (
                SetupContext(identity_source=IdentitySource.NONE,
                             allow_remote_writes=False),
                None,
            )
        github_repo = detected
        repo_name = detected.split("/", 1)[1]
        ctx = SetupContext(
            identity_source=IdentitySource.REMOTE,
            allow_remote_writes=True,
        )

    config = {
        "display_name": args.name,
        "repo_name": repo_name,
        "github_repo": github_repo,
        "description": args.description or f"{args.name} — a federation node",
        "tier": args.role,
        "domains": [],
        "custom_skills": [],
        "values": "",
        "role_id": f"{repo_name.replace('-', '_')}_{args.role}",
        "city_zone": args.zone or TIER_TO_ZONE.get(args.role, "general"),
    }
    return ctx, config


def _run_governance_standalone() -> int:
    """Run only the governance step (--apply-governance without setup).

    Reads repository information from .federation-setup.json.
    """
    config_path = REPO_ROOT / ".federation-setup.json"
    if not config_path.exists():
        print(f"  {YELLOW}No setup config found. Run: python scripts/setup_node.py{RESET}")
        return 2
    _ = json.loads(config_path.read_text())
    repo, diag = detect_repository(REPO_ROOT)
    if repo is None:
        _print_governance_diag(diag)
        return 2
    print(f"\n{BOLD}── Governance: Branch Protection Baseline ──{RESET}\n")
    print(f"  Repository:     {repo.full_name}")
    print(f"  Default Branch: {repo.default_branch}")
    check = inspect_governance(repo)
    _print_governance_check(check)
    if check.compliance == ComplianceStatus.CONFORMANT:
        print(f"\n  {GREEN}Already conformant — nothing to do.{RESET}")
        return 0
    print("\n  Applying federation-baseline ruleset...")
    result = ensure_governance_baseline(repo, check)
    print(f"  Action: {GREEN}{result.action or 'none'}{RESET}")

    # Display apply-step diagnostics
    for d in result.diagnostics:
        print(f"  {YELLOW}Diagnostic: {d.value}{RESET}")
    for detail in result.details:
        print(f"  {DIM}{detail}{RESET}")

    if result.final_check is not None:
        print()
        _print_governance_check(result.final_check)
        if result.final_check.compliance == ComplianceStatus.CONFORMANT:
            print(f"\n  {GREEN}Governance baseline applied successfully.{RESET}")
            return 0
        if result.final_check.compliance == ComplianceStatus.NON_CONFORMANT:
            print(f"\n  {YELLOW}Re-read did not confirm compliance.{RESET}")
            return 1
        print(f"\n  {YELLOW}Re-read returned unknown compliance.{RESET}")
        return 2

    # No final_check → action failed completely
    print(f"\n  {YELLOW}Could not apply baseline.{RESET}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
