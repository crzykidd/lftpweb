// Pure presentation logic for the nav's bottom-left version readout (DESIGN.md §9.1),
// extracted the same way as lib/resetWarning.ts and friends so it's testable without an
// effect-driven fetch or a DOM. `VersionLink.tsx` is the only caller.
//
// 2026-08-16 (docs/decisions.md, prompts/done/2026-08-16-dev-build-version-badge.md): a
// `:dev` image now bakes `build_sha`/`build_channel` (config.Settings, /api/health) so a test
// instance is never mistaken for a release. Every path that never baked them -- local
// `uv run`, `docker-compose.dev.yml`, a manual `docker build` with no `--build-arg`, or simply
// `health` not having arrived yet -- degrades to exactly today's rendering. That degradation is
// the point: `build_channel` is `null` (not `'release'`) on all of those paths, so `!== 'dev'`
// is the one branch this function needs to fall back on, never a second "is this actually a
// release" check.

import type { HealthResponse } from '../api/types'

export interface VersionBadge {
  /** What to show: `v0.1.1`, or `DEV: v0.1.1 · <sha>` (dev builds; the sha suffix is only
   * dropped in the defensive case of a dev channel baked without a sha). */
  label: string
  /** Drives the amber badge styling -- true only for a confirmed dev-channel build. */
  dev: boolean
  /** Link target, or `null` to render plain text (no dead link). The commit on GitHub when
   * both `build_sha` and `repo_url` are present on a dev build; the release tag otherwise,
   * whenever `repo_url` is present; `null` when it isn't. */
  href: string | null
}

/** `null` while health hasn't loaded yet -- the caller keeps its own "v…" placeholder rather
 * than this module inventing one, so there is exactly one "not loaded" rendering in the app.
 */
export function versionBadge(health: HealthResponse | null): VersionBadge | null {
  if (!health) return null

  const label = `v${health.version}`
  const releaseHref = health.repo_url ? `${health.repo_url}/releases/tag/${label}` : null

  if (health.build_channel !== 'dev') {
    return { label, dev: false, href: releaseHref }
  }

  const sha = health.build_sha
  const devLabel = sha ? `DEV: ${label} · ${sha}` : `DEV: ${label}`
  const href = sha && health.repo_url ? `${health.repo_url}/commit/${sha}` : releaseHref

  return { label: devLabel, dev: true, href }
}
