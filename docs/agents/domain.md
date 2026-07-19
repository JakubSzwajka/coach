# Domain Docs

How the engineering skills should consume this repo's domain documentation when
exploring the codebase. **Layout: single-context.**

## Before exploring, read these

- **`CONTEXT.md`** at the repo root (the domain glossary / ubiquitous language).
- **`docs/adr/`** — read ADRs that touch the area you're about to work in.

This repo also keeps durable project knowledge under **`docs/knowledge/`**
(e.g. `docs/knowledge/integrations/`). Read the relevant domain folder there when
it covers your topic.

If any of these files don't exist, **proceed silently**. Don't flag their
absence; don't suggest creating them upfront. The `/domain-modeling` skill
(reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates
them lazily when terms or decisions actually get resolved.

## File structure

Single-context repo:

```
/
├── CONTEXT.md
├── docs/
│   ├── adr/
│   │   ├── 0001-....md
│   │   └── 0002-....md
│   ├── agents/          ← this config (issue tracker, labels, domain)
│   └── knowledge/       ← durable project knowledge by domain
└── collector/  viz/  ...
```

## Use the glossary's vocabulary

When your output names a domain concept (an issue title, a refactor proposal, a
hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to
synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal — either
you're inventing language the project doesn't use (reconsider) or there's a real
gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than
silently overriding:

> _Contradicts ADR-0002 (…) — but worth reopening because…_
