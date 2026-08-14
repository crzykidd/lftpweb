# docs/

Two different audiences share this directory. Don't confuse them.

## User documentation

**[`quick-start.md`](quick-start.md)** and **[`concepts.md`](concepts.md)** are for someone
running lftpweb — deploying it, connecting to a seedbox, and understanding the handful of
behaviours (the settle gate, auto-queue suppression, `copy` vs `move`, ...) that actually
confuse people in practice. They render in the app itself under **Docs** in the left nav
(`/docs/quick-start`, `/docs/concepts`) — `frontend/src/pages/docs/MarkdownDoc.tsx` reads these
same two files at build time (2026-08-14,
[`prompts/done/2026-08-14-docs-as-markdown-single-source.md`](../prompts/done/2026-08-14-docs-as-markdown-single-source.md)),
so this is the *only* copy of that prose — reading it here or reading it in the running app
shows identical text, because it's the same file.

Internal links inside them (`[Settings → Queues](/settings/queues)`) are app routes, not GitHub
paths — they only resolve to something useful inside a running instance.

## Engineering records

**[`decisions.md`](decisions.md)** — the project's decision log: approach changes, rejected
alternatives, workarounds. Newest entry at top.

**[`repo-setup.md`](repo-setup.md)** — a one-time runbook for taking this repo from "prepared
for GitHub" to actually on GitHub with branch protection enforced. Not relevant once that's
done.

Architecture lives in [`../DESIGN.md`](../DESIGN.md), not here.
