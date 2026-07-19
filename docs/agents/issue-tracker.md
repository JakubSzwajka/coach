# Issue tracker: Linear

Issues and PRDs for this repo live in **Linear**, in the **Garmin Coach** project.
Use the Linear MCP tools (`mcp__linear_*`, exposed as `xd://` devices) for all
operations; the `orca linear` CLI is an alternative for the same actions.

## Identifiers

- **Team:** Niuluc — key `TASK` — id `52e0b010-ad49-4016-a15d-f4f669f01bcd`
- **Project:** Garmin Coach — id `07faa647-79f1-4f55-9553-c136e04547e1`
  — <https://linear.app/jakszw/project/garmin-coach-3b07d27fed3c>
- Issue identifiers look like `TASK-123`.

## Conventions

- **Create an issue**: `mcp__linear_save_issue` with `title`, `team: "Niuluc"`,
  `project: "Garmin Coach"`, `description` (markdown), and optionally `labels`,
  `assignee` (`"me"` or a name/email), `priority`, `parentId`.
- **Read an issue**: `mcp__linear_get_issue` with `id: "TASK-123"`
  (`includeRelations: true` to see blocking/related/duplicate links).
- **List issues**: `mcp__linear_list_issues` filtered by `team`, `project`,
  `assignee` (`"me"`/`"null"`), `state`, `label`, or `query`.
- **Comment**: `mcp__linear_save_comment` with `issueId` and `body`
  (or `parentId` to reply in a thread).
- **List comments**: `mcp__linear_list_comments` with `issueId`.
- **Apply / change labels**: `mcp__linear_save_issue` with `id` and `labels`.
  Linear replaces the label set — include existing labels you want to keep.
- **Change state / close**: `mcp__linear_save_issue` with `id` and `state`
  (Linear workflow states, e.g. `Todo`, `In Progress`, `Done`, `Canceled`).
  Prefer `Canceled` for `wontfix` and `Done` for completed work.

## Pull requests as a triage surface

**PRs as a request surface: no.** This repo has no public code-hosting remote,
so external PRs are not a request surface. `/triage` reads only Linear issues.

## When a skill says "publish to the issue tracker"

Create a Linear issue in the **Garmin Coach** project (team Niuluc).

## When a skill says "fetch the relevant ticket"

Run `mcp__linear_get_issue` with the `TASK-<n>` identifier
(add `includeRelations: true` when dependencies matter).

## Wayfinding operations

Used by `/wayfinder`. Map the concepts onto Linear:

- **Map**: a Linear issue labelled `wayfinder:map` (create it if the label does
  not exist) holding the Notes / Decisions-so-far / Fog body. A project document
  (`mcp__linear_save_document`) is an acceptable alternative for the map body.
- **Child ticket**: a Linear issue created with `parentId` set to the map issue,
  labelled `wayfinder:<type>` (`research` / `prototype` / `grilling` / `task`).
  Assign to the driving dev once claimed (`assignee`).
- **Blocking**: Linear native issue relations (`blocks` / `blocked by`), visible
  via `includeRelations: true`. A ticket is unblocked when every blocker is
  `Done`/`Canceled`.
- **Frontier query**: `mcp__linear_list_issues` for the map's children in an open
  state; drop any with an open blocker or an assignee; first in map order wins.
- **Claim**: set `assignee: "me"` on the child issue — the session's first write.
- **Resolve**: comment the answer (`mcp__linear_save_comment`), set the child to
  `Done`, then append a context pointer to the map's Decisions-so-far.
