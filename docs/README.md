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

**[`audit-v0.1.0.md`](audit-v0.1.0.md)** — the codebase audit run against the first release.
Historical.

### Feature specs

**These are *proposal* documents by convention, and they describe more than exists.** Each one
records the reasoning, the decisions it reverses (in place, with their cause — preserve that
convention when editing), the open questions, and a staged build order. **`DESIGN.md` is what
describes reality**; where a spec and `DESIGN.md` disagree about whether something is built,
`DESIGN.md` wins.

**[`transfers-redesign-spec.md`](transfers-redesign-spec.md)** — where the
Transfers/Files/History surfaces went: Transfers as the main section with Queue and Files tabs,
one globally-ordered ungrouped queue list, History becoming Events, and the Preflight box.
**Phase 1 is built and shipped in `v0.3.0`.** Its §4 (the advisory download-client integration)
is the sketch the framework spec below supersedes.

**[`arr-integration-spec.md`](arr-integration-spec.md)** — the Sonarr/Radarr integration: data
model, association lifecycle, matching rules, the poller, the fully-done gate. **Built**;
`DESIGN.md` §16 is its architectural summary.

**[`download-client-framework-spec.md`](download-client-framework-spec.md)** — the pluggable
download-client connector layer (SABnzbd, rTorrent, and whatever follows): the two vocabularies,
the tri-state capability declaration, deletion-over-SSH, the disk review scan, and a six-stage
build order. **Stages 0–4 are built and unreleased on `dev`; stage 5, the delete pipeline, is
not.** `DESIGN.md` §17 is the architectural summary and the description of what exists. **§13.4
and §13.6 are its two correction lists** — every SABnzbd and rTorrent guess authored from vendor
docs and never confirmed against a live instance, risk-ranked. The test suite cannot falsify
them; the fixtures encode the same assumptions.

**[`download-client-api-survey.md`](download-client-api-survey.md)** — what rTorrent/ruTorrent,
qBittorrent, Transmission, Deluge and SABnzbd can each actually report, researched from vendor
docs 2026-08-22. **Re-confirm anything against a real instance before relying on it.**

**[`torrent-manager-spec.md`](torrent-manager-spec.md)** — issue #21: seeding overview, per-site
stop-seeding rules, space reclamation. **Not started**, and it depends on the connector framework
above.

Architecture lives in [`../DESIGN.md`](../DESIGN.md), not here.
