# AGENTS.md

Repo-level guidance for coding agents working in **garmin-coach**.

## Vision

Read [`VISION.md`](VISION.md) first. It's the north star: what this project is
(a personal Garmin data platform — collect once, use everywhere), the staged
ambition (describe → explain → coach), the guardrails (read-only to Garmin, no
medical claims), and the autonomous-vs-ask-first rule that drives the
`ready-for-agent` / `ready-for-human` triage split.

## Agent skills

### Issue tracker

Issues live in Linear — project **Garmin Coach** (team Niuluc, key `TASK`), via the Linear MCP tools / `orca linear` CLI. PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Canonical five labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), all present in Niuluc. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
