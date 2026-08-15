// Settings -> Transfer's "effective lftp settings" readout (2026-08-14,
// prompts/2026-08-14-show-effective-lftp-settings.md) -- collision detection between the
// free-text "Extra lftp settings" box and the settings `GET /api/settings/transfer/
// effective-lftp` reports lftpweb already writes into every job's rc file.
//
// A pure function, not the backend, on purpose: the prompt's own instruction is that the
// backend response must be generated from `core/lftp.py`, never hand-maintained, and the
// collision check is presentation logic over that response plus whatever the user has typed
// into a textarea that hasn't been saved yet -- there is no reason to round-trip an unsaved
// draft to the server just to compare two lists of strings.
//
// **Which side wins is verified, not guessed.** lftp's own `set` command is last-write-wins
// for a given key within one sourced script (verified directly against a real lftp 4.9.2
// binary: `lftp -c "set K v1; set K v2; set -a"` prints only `v2` --
// `tests/test_lftp_settings_accepted.py`'s
// `test_extra_lftp_settings_override_a_colliding_lftpweb_default` reproduces this through the
// exact rc `core/lftp.py.build_rc_text` generates). `build_rc_text` always appends the extra
// lftp settings box's contents *after* every built-in tuning line and before the credential-
// bearing `open` command, so a user's line for a key lftpweb also sets always comes later in
// the sourced script and therefore always wins.

export interface EffectiveLftpSetting {
  key: string
  value: string
  why: string
  configurable: boolean
}

export interface EffectiveLftpJobKind {
  kind: 'mirror' | 'pget'
  argv: string
  argv_why: string
  rc_settings: EffectiveLftpSetting[]
}

export interface ParsedSetLine {
  key: string
  value: string
  raw: string
}

/** Extracts `set <key> <value...>` lines the same way lftp's own tokenizer would recognise
 * them for the purpose of *which key is being set* -- a leading `set`, one key token, and
 * everything else on the line as the value (trailing `;` and surrounding whitespace trimmed).
 * Not a full lftp-syntax parser: quoting inside the value is left as-is, since only the key is
 * ever compared. Blank lines, comments, and any non-`set` command are ignored -- the box also
 * accepts other lftp commands, but only `set` lines can collide with a `set`-only rc.
 */
export function parseExtraLftpSettings(text: string): ParsedSetLine[] {
  const lines: ParsedSetLine[] = []
  for (const rawLine of text.split('\n')) {
    const trimmed = rawLine.trim()
    if (!trimmed.toLowerCase().startsWith('set ')) continue
    const rest = trimmed.slice(4).trim()
    const spaceIndex = rest.search(/\s/)
    if (spaceIndex === -1) continue // `set key` alone -- unsets the key, no value to compare
    const key = rest.slice(0, spaceIndex)
    const value = rest.slice(spaceIndex + 1).trim().replace(/;\s*$/, '').trim()
    if (!key || !value) continue
    lines.push({ key, value, raw: trimmed })
  }
  return lines
}

export interface LftpSettingCollision {
  key: string
  userValue: string
  userLine: string
  /** Every occurrence of this key lftpweb itself would write, one per job kind that sets it
   * (the value can legitimately differ between `mirror` and `pget` -- see
   * `effectiveLftpSettings.ts`'s own note on `mirror:use-pget-n` / `pget:default-n`).
   */
  lftpwebOccurrences: { kind: EffectiveLftpJobKind['kind']; value: string }[]
}

/** Every key the "Extra lftp settings" box sets that lftpweb also sets somewhere in the rc,
 * across either job kind. Returns one entry per colliding *user* line (a box that repeats the
 * same key twice yields two collisions, mirroring the box's own last-line-wins reality).
 */
export function findLftpSettingCollisions(
  extraLftpSettings: string,
  kinds: EffectiveLftpJobKind[],
): LftpSettingCollision[] {
  const byKey = new Map<string, { kind: EffectiveLftpJobKind['kind']; value: string }[]>()
  for (const k of kinds) {
    for (const setting of k.rc_settings) {
      const existing = byKey.get(setting.key) ?? []
      existing.push({ kind: k.kind, value: setting.value })
      byKey.set(setting.key, existing)
    }
  }

  const collisions: LftpSettingCollision[] = []
  for (const line of parseExtraLftpSettings(extraLftpSettings)) {
    const occurrences = byKey.get(line.key)
    if (!occurrences) continue
    collisions.push({
      key: line.key,
      userValue: line.value,
      userLine: line.raw,
      lftpwebOccurrences: occurrences,
    })
  }
  return collisions
}
