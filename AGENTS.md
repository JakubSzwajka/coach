# AGENTS.md

Repo-level guidance for coding agents working in **garmin-coach**.

## ⚠️ This repo is PUBLIC

`github.com/JakubSzwajka/coach` is a **public** repository. Never commit
secrets or personal data:

- **No credentials/tokens** — Garmin login comes from `GARMIN_EMAIL` /
  `GARMIN_PASSWORD` env vars (see `.env.example`). `.env` and `~/.garminconnect`
  are gitignored; keep it that way.
- **No collected health data** — everything under `data/` (raw + derived) is
  gitignored and must stay out of git.
- **No personal identifiers** — real names, emails, Garmin profile ids, absolute
  home paths (`/Users/<you>/...`), device serials. Use placeholders in docs and
  examples.
- Before staging, sanity-check the diff for the above. When in doubt, leave it
  out.

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
