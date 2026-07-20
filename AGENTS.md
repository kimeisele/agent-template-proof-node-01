# AGENTS.md

Instructions for AI coding agents working on this repository.

## Overview

This is a **federation node template** for the [agent-internet](https://github.com/kimeisele/agent-internet) mesh.
It publishes authority documents, exposes an agent card, and discovers peer nodes via GitHub API.

## Architecture

```
.well-known/
  agent-federation.json    ← federation descriptor (auto-generated)
  agent.json               ← agent card (auto-generated)
docs/authority/
  charter.md               ← canonical authority document (edit this)
  capabilities.json        ← capability manifest (edit this)
data/federation/
  authority-descriptor-seeds.json  ← known peer URLs
data/federation/nadi_outbox.json       ← Nadi transport outbox (plain [] array)
scripts/
  render_federation_descriptor.py  ← generates .well-known/agent-federation.json
  render_agent_card.py             ← generates .well-known/agent.json
  export_authority_feed.py         ← builds authority feed bundle
  discover_federation_peers.py     ← discovers peers via GitHub API (curl)
  fetch_peer_authority.py          ← fetches + SHA-256-verifies peer feeds
  nadi_send.py                     ← queue DeliveryEnvelope messages to outbox
tests/
  test_federation.py               ← federation smoke tests
  test_governance.py               ← governance baseline tests
  test_identity.py                 ← identity / drift / topic tests
  test_nadi.py                     ← nadi path / daemon / send tests
```

## Setup

```bash
# Interactive setup wizard (configures charter, capabilities, descriptors)
python scripts/setup_node.py

# Non-interactive (CI/scripting)
python scripts/setup_node.py --non-interactive --name "My Node" --role research --org myorg
```

Tiers: `relay`, `contributor`, `research`, `service`, `governance`.
Every tier includes the core federation kernel.

## Build & test

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
python -m pytest tests/ -q

# Lint
python -m ruff check .

# Regenerate descriptors
python scripts/render_federation_descriptor.py
python scripts/render_agent_card.py

# Export authority feed
python scripts/export_authority_feed.py

# Discover federation peers (requires GITHUB_TOKEN for rate limits)
python scripts/discover_federation_peers.py
```

## Conventions

- Python >= 3.11, standard library only (no runtime dependencies)
- All scripts use `argparse` and return `int` exit codes
- JSON output uses 2-space indent, sorted keys for canonical representation
- SHA-256 hashing for artifact integrity verification
- `curl` subprocess for all HTTP (no `requests` dependency)

## Key patterns

- Federation descriptor must include: `kind`, `version`, `repo_id`, `display_name`, `status`, `capabilities`, `layer`, `endpoints`
- Capability manifest follows the `agent_capability_manifest` schema with `federation_interfaces.produces` / `consumes` / `protocols`
- Discovery is dual-mode: seed-based (offline/unauthed) + GitHub topic search (`agent-federation-node`)
- Authority feeds are versioned by git commit SHA with SHA-256 content hashing
- Nadi transport: `data/federation/nadi_outbox.json` holds signed `NadiMessage` entries written by `nadi_send.py` via `NadiNode.emit()`; `nadi_daemon.py --once` gives a read-only local diagnostic; `--relay` enables hub push/pull

## Git workflow

- Feature branches, PRs to `main`
- Pushing to `main` triggers: descriptor sync, agent card sync, authority feed publish
- Bot commits use `[skip ci]` to prevent infinite workflow loops
- Federation discovery runs weekly (Monday 06:00 UTC)
- Bot commits use `federation-bot` / `bot@federation`

## What to customize

When using this template for a new node:
1. `docs/authority/charter.md` — replace with your charter
2. `docs/authority/capabilities.json` — declare your skills, produces/consumes
3. Add the `agent-federation-node` GitHub topic
4. Everything in `.well-known/` is auto-generated — don't edit directly
