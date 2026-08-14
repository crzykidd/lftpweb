# docs/

Two different audiences share this directory. Don't confuse them.

## User documentation

**[`quick-start.md`](quick-start.md)**, **[`how-it-works.md`](how-it-works.md)** and
**[`concepts.md`](concepts.md)** are for someone running lftpweb — deploying it, connecting to a
seedbox, understanding why the thing is shaped the way it is, and understanding the handful of
behaviours (the settle gate, auto-queue suppression, `copy` vs `move`, ...) that actually
confuse people in practice. They render in the app itself under **Docs** in the left nav
(`/docs/quick-start`, `/docs/how-it-works`, `/docs/concepts`) —
`frontend/src/pages/docs/MarkdownDoc.tsx` reads these same files at build time (2026-08-14,
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

**[`screenshot-plan.md`](screenshot-plan.md)** — the shooting order for screenshots: which two
go in `README.md`, which go in the gallery, what state each screen needs to be staged in, and
what to check before publishing. Not yet acted on; a human has to take them, since no browser
exists where agents run.

**[`screenshots.md`](screenshots.md)** — the gallery itself, with captions already written
against placeholder image paths. GitHub-only by design: it is not wired to an in-app route,
because screenshots of the app are useless inside the app. Images live in
[`images/`](images/) — see that directory's own README for the exact filenames expected.

Architecture lives in [`../DESIGN.md`](../DESIGN.md), not here.
