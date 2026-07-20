# DEFAULT_BRANCH_GOVERNANCE_VALIDATION.md

> Authoritative validation record — version 1.0.0

## Purpose

Every Federation node created from this template must have its default
branch protected against accidental or malicious bypass.  This
document records the design, tests, and live-proof results of the
`agent-federation-baseline-v1` mechanism.

## Baseline Ruleset

| Field | Value |
|---|---|
| **Name** | `agent-federation-baseline-v1` |
| **Target** | `branch` |
| **Enforcement** | `active` |
| **Default branch** | `~DEFAULT_BRANCH` |
| **Bypass actors** | `[]` |
| **Exclude** | `[]` |

### Canonical Rules

| Rule | Effect |
|---|---|
| `deletion` | Default branch cannot be deleted |
| `non_fast_forward` | Force pushes are blocked |
| `pull_request` | Changes must go through a pull request |

`required_approving_review_count` is `0` — pull requests are
required, but approvals are not (template users may be solo).

All three merge methods (`merge`, `squash`, `rebase`) are permitted.

## Unit-Test Evidence

The governance module is tested by 93 unit tests in
`tests/test_governance.py`.  All external GitHub API calls are mocked.

Key coverage areas:

- Repository detection from git remote
- Per-rule-type evaluation (CONFIRMED / MISSING / UNKNOWN)
- Aggregation from two API sources (rulesets + classic protection)
- Ruleset creation, idempotency, and conservative compatibility checks
- Auth, permission, and network error handling
- CLI mode enforcement (interactive, non-interactive, apply-governance, status)
- Conservative validation (target, conditions, exclude, bypass_actors, type safety)

## Live-Proof

A complete end-to-end validation was performed in:

**`kimeisele/agent-red-team`**

### Positive Path

- Node setup via `python scripts/setup_node.py --non-interactive`
- Governance applied via `python scripts/setup_node.py --apply-governance`
- Local changes committed on `setup-federation-node` branch
- PR merged to `main` through the normal GitHub merge flow
- `--status` returned exit 0, Compliance `CONFORMANT`
- All three baseline rules confirmed present

### Negative Paths

| Test | Command | Result |
|---|---|---|
| Direct push to `main` | `git push origin main` | ❌ Blocked: "Changes must be made through a pull request." |
| Force push to `main` | `git push --force-with-lease origin …:main` | ❌ Blocked by repository rule violations |
| Delete default branch | `git push origin --delete main` | ❌ Blocked: "refusing to delete the current branch" |

All three prohibited operations were rejected while the remote `main`
SHA remained unchanged.

## Known v1 Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| `BypassState.UNKNOWN` in normal inspection flow | `--status` cannot confirm absence of bypass actors | Full detail fetch only in ensure flow; documented limitation |
| No organisation-ruleset management | Only repository-level rulesets are created/verified | Organisational rulesets are recognised via `rules/branches` but not managed |
| No PUT update of existing rulesets | Divergent same-named rulesets are diagnosed, not repaired | User receives specific `UNSUPPORTED_CONFIG` diagnostic with manual guidance |
| `required_approving_review_count: 0` | No approval enforcement on PRs | Solo template users cannot require approvals; stricter nodes can increase this value |

## Criteria for v2

A `v2` baseline ruleset (`agent-federation-baseline-v2`) should be
considered when:

1. The Federation adopts mandatory CI checks with stable, documented
   check names.
2. Organisational-ruleset management is required for multi-repo
   consistency.
3. A safe, verifiable PUT-update strategy for existing rulesets is
   designed and tested.
4. Approval requirements become enforceable (e.g., minimum
   collaborators per node).
5. Bypass-actor inspection in the read-only flow becomes a hard
   requirement.

## References

- Specification: `kimeisele/agent-template#7`
- Implementation PR: `kimeisele/agent-template#8`
- Live proof repository: `kimeisele/agent-red-team`
