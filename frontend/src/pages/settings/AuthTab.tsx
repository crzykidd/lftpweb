import { useEffect, useState } from 'react'
import {
  changePassword,
  createApiKey,
  deleteApiKey,
  getAuthSettings,
  listApiKeys,
  putAuthSettings,
} from '../../api/client'
import type { ApiKeyOut, AuthMode, AuthSettingsOut } from '../../api/types'
import { useAuth } from '../../hooks/authContext'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'
const buttonClasses =
  'w-fit rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300'
const dangerButtonClasses =
  'w-fit rounded-md border border-red-300 px-2 py-1 text-xs font-medium text-red-700 hover:bg-red-50 disabled:opacity-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950'

const MODES: { value: AuthMode; label: string; description: string }[] = [
  {
    value: 'none',
    label: 'None (default)',
    description: 'No authentication at all -- exactly the behaviour before this phase.',
  },
  {
    value: 'password',
    label: 'Password',
    description:
      'Single local user, argon2id-hashed password, HTTP-only session cookie, CSRF-protected, rate-limited login.',
  },
  {
    value: 'proxy',
    label: 'Trusted proxy',
    description:
      'Trust an identity header (e.g. Remote-User) forwarded by a reverse proxy such as Authelia -- only from a configured trusted CIDR. Refuses to enable without one.',
  },
]

/** Settings → Auth (DESIGN.md §8, phase 8). Never click-tested (no browser in this build
 * environment) -- see docs/decisions.md. The mode/CIDR/user-creation invariants this form
 * enforces client-side (never store `proxy` without a CIDR, never store `password` without a
 * user who can log in) are also enforced server-side in `api/auth.py.put_auth_settings`, so a
 * direct API call can't bypass them either.
 */
export function AuthTab() {
  const { refresh: refreshSession } = useAuth()

  const [settings, setSettings] = useState<AuthSettingsOut | null>(null)
  const [mode, setMode] = useState<AuthMode>('none')
  const [proxyHeader, setProxyHeader] = useState('Remote-User')
  const [cidrsText, setCidrsText] = useState('')
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')
  const [changePasswordValue, setChangePasswordValue] = useState('')

  const [apiKeys, setApiKeys] = useState<ApiKeyOut[]>([])
  const [newKeyName, setNewKeyName] = useState('')
  const [createdKey, setCreatedKey] = useState<string | null>(null)

  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const loadAll = async () => {
    setError(null)
    try {
      const [s, keys] = await Promise.all([getAuthSettings(), listApiKeys()])
      setSettings(s)
      setMode(s.mode)
      setProxyHeader(s.proxy_header)
      setCidrsText(s.proxy_trusted_cidrs.join('\n'))
      setApiKeys(keys)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void loadAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSaveMode = async () => {
    setError(null)
    setNotice(null)
    setSaving(true)
    try {
      const cidrs = cidrsText
        .split(/[\n,]/)
        .map((c) => c.trim())
        .filter(Boolean)
      const saved = await putAuthSettings({
        mode,
        proxy_header: proxyHeader.trim() || 'Remote-User',
        proxy_trusted_cidrs: cidrs,
        // Only sent when switching into password mode for the first time, or deliberately
        // changing the username -- see AuthSettingsIn's own comment. A bare "save mode"
        // click for an already-configured password user must not require re-entering it.
        username: mode === 'password' && !settings?.has_user ? newUsername : undefined,
        new_password: mode === 'password' && !settings?.has_user ? newPassword : undefined,
      })
      setSettings(saved)
      setNewUsername('')
      setNewPassword('')
      setNotice('Saved.')
      await refreshSession()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleChangePassword = async () => {
    setError(null)
    setNotice(null)
    setSaving(true)
    try {
      await changePassword({ current_password: currentPassword, new_password: changePasswordValue })
      setCurrentPassword('')
      setChangePasswordValue('')
      setNotice('Password changed. You will need to sign in again.')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleCreateKey = async () => {
    setError(null)
    setCreatedKey(null)
    if (!newKeyName.trim()) return
    setSaving(true)
    try {
      const created = await createApiKey({ name: newKeyName.trim() })
      setCreatedKey(created.key)
      setNewKeyName('')
      const keys = await listApiKeys()
      setApiKeys(keys)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleDeleteKey = async (id: number) => {
    setError(null)
    try {
      await deleteApiKey(id)
      setApiKeys((prev) => prev.filter((k) => k.id !== id))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        DESIGN.md §8. Defaults to <code>none</code> -- an existing install behaves identically
        until this page is used to turn something on. Locked out? See README.md's "Locked out?"
        section: set <code>LFTPWEB_AUTH_MODE=none</code> and restart, or delete the local user
        row directly in the database.
      </p>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      {notice && <p className="text-sm text-emerald-600 dark:text-emerald-400">{notice}</p>}

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Mode</h3>
        <div className="flex flex-col gap-2">
          {MODES.map((m) => (
            <label key={m.value} className="flex items-start gap-2">
              <input
                type="radio"
                name="auth-mode"
                className="mt-1"
                checked={mode === m.value}
                onChange={() => setMode(m.value)}
              />
              <span>
                <span className="block text-sm font-medium text-zinc-900 dark:text-zinc-100">
                  {m.label}
                </span>
                <span className="block text-xs text-zinc-500 dark:text-zinc-400">
                  {m.description}
                </span>
              </span>
            </label>
          ))}
        </div>

        {mode === 'password' && !settings?.has_user && (
          <div className="flex flex-col gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              No local user exists yet -- create one to enable password mode.
            </p>
            <label className="flex flex-col gap-1">
              <span className={labelClasses}>Username</span>
              <input
                className={inputClasses}
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                autoComplete="username"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className={labelClasses}>Password</span>
              <input
                type="password"
                className={inputClasses}
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
              />
            </label>
          </div>
        )}

        {mode === 'proxy' && (
          <div className="flex flex-col gap-2 border-t border-zinc-200 pt-3 dark:border-zinc-800">
            <label className="flex flex-col gap-1">
              <span className={labelClasses}>Identity header</span>
              <input
                className={`${inputClasses} max-w-64`}
                value={proxyHeader}
                onChange={(e) => setProxyHeader(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className={labelClasses}>Trusted CIDRs (one per line)</span>
              <textarea
                className={`${inputClasses} h-24 font-mono`}
                value={cidrsText}
                onChange={(e) => setCidrsText(e.target.value)}
                placeholder="10.0.0.5/32&#10;192.168.1.0/24"
              />
              <span className="text-xs text-zinc-500 dark:text-zinc-400">
                Required -- DESIGN.md §8 is explicit that without a trusted CIDR, proxy mode is
                a bypass. Saving is refused with none configured.
              </span>
            </label>
          </div>
        )}

        <button type="button" disabled={saving} onClick={handleSaveMode} className={buttonClasses}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>

      {settings?.has_user && (
        <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
          <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            Change password ({settings.username})
          </h3>
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>Current password</span>
            <input
              type="password"
              className={inputClasses}
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              autoComplete="current-password"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>New password</span>
            <input
              type="password"
              className={inputClasses}
              value={changePasswordValue}
              onChange={(e) => setChangePasswordValue(e.target.value)}
              autoComplete="new-password"
            />
          </label>
          <button
            type="button"
            disabled={saving || !currentPassword || !changePasswordValue}
            onClick={handleChangePassword}
            className={buttonClasses}
          >
            Change password
          </button>
        </div>
      )}

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">API keys</h3>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          <code>X-API-Key</code>, accepted independently of the mode above -- for scripts. Shown
          in full exactly once, right after creation.
        </p>

        {createdKey && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
            <p className="mb-1 font-medium">
              Copy this now -- it will not be shown again:
            </p>
            <code className="block break-all">{createdKey}</code>
          </div>
        )}

        <div className="flex gap-2">
          <input
            className={inputClasses}
            placeholder="key name (e.g. sonarr)"
            value={newKeyName}
            onChange={(e) => setNewKeyName(e.target.value)}
          />
          <button
            type="button"
            disabled={saving || !newKeyName.trim()}
            onClick={handleCreateKey}
            className={buttonClasses}
          >
            Create
          </button>
        </div>

        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-zinc-500 dark:text-zinc-400">
              <th className="py-1 pr-4 font-medium">Name</th>
              <th className="py-1 pr-4 font-medium">Created</th>
              <th className="py-1 pr-4 font-medium">Last used</th>
              <th className="py-1 font-medium" />
            </tr>
          </thead>
          <tbody>
            {apiKeys.map((k) => (
              <tr key={k.id} className="border-t border-zinc-100 dark:border-zinc-800">
                <td className="py-1 pr-4 text-zinc-800 dark:text-zinc-200">{k.name}</td>
                <td className="py-1 pr-4 text-zinc-600 dark:text-zinc-400">
                  {new Date(k.created_at).toLocaleString()}
                </td>
                <td className="py-1 pr-4 text-zinc-600 dark:text-zinc-400">
                  {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'never'}
                </td>
                <td className="py-1">
                  <button
                    type="button"
                    onClick={() => void handleDeleteKey(k.id)}
                    className={dangerButtonClasses}
                  >
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
            {apiKeys.length === 0 && (
              <tr>
                <td colSpan={4} className="py-2 text-zinc-500 dark:text-zinc-400">
                  No API keys yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
