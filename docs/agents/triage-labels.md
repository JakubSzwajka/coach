# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those
roles to the actual Linear label strings used in this repo's tracker (team
Niuluc). All five labels exist in Linear.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the
corresponding label string from this table.

Notes for Linear:

- Apply labels with `mcp__linear_save_issue` (`labels` replaces the set — include
  any existing labels you want to keep).
- `wontfix` pairs naturally with the Linear `Canceled` workflow state; set both
  when declining an issue.

Edit the right-hand column to match whatever vocabulary you actually use.
