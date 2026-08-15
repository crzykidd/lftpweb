// One small, safe `localStorage` wrapper (2026-08-13, prompts/2026-08-13-lifecycle-icons.md)
// -- the Files page's collapse preference and sort preference both go through this, per the
// task's own instruction ("same storage helper, same failure handling. Do not write two").
//
// Both directions are wrapped: reading must not throw in private browsing (some browsers make
// `localStorage` throw on access, not just on write) or on corrupt/foreign JSON left over from
// an older version of this key; writing must not throw when storage is full or unavailable. A
// preference read/write failing must never break the page -- it just doesn't persist for that
// session.

const NAMESPACE = 'lftpweb'

/** `isValid` is a type guard so a corrupt or foreign value (an old schema, something another
 * tab wrote) is rejected rather than trusted -- the caller gets `null` exactly like "never
 * saved," and falls back to its own default the same way either way.
 */
export function readLocalStorage<T>(key: string, isValid: (value: unknown) => value is T): T | null {
  try {
    const raw = localStorage.getItem(`${NAMESPACE}.${key}`)
    if (raw == null) return null
    const parsed: unknown = JSON.parse(raw)
    return isValid(parsed) ? parsed : null
  } catch {
    return null
  }
}

export function writeLocalStorage(key: string, value: unknown): void {
  try {
    localStorage.setItem(`${NAMESPACE}.${key}`, JSON.stringify(value))
  } catch {
    // Quota exceeded, storage disabled, or otherwise unavailable -- the in-memory state for
    // this session still works, it just won't survive a reload.
  }
}
