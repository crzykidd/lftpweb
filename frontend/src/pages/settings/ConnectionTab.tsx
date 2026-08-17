import { useEffect, useState } from 'react'
import { getHost, putHost, testHost } from '../../api/client'
import type { AuthMethod, HostOut, KnownHostsPolicy, TestConnectionResponse } from '../../api/types'
import { FieldHelp } from '../../components/FieldHelp'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'

interface FormState {
  name: string
  address: string
  port: string
  username: string
  auth_method: AuthMethod
  key_path: string
  password: string
  ssh_key: string
  known_hosts_policy: KnownHostsPolicy
}

const EMPTY_FORM: FormState = {
  name: '',
  address: '',
  port: '22',
  username: '',
  auth_method: 'key',
  key_path: '',
  password: '',
  ssh_key: '',
  known_hosts_policy: 'accept-and-pin',
}

/** DESIGN.md §9.2 Settings → Connection. The password and pasted-key fields are write-only by
 * design (§9.2): neither is ever pre-filled from a saved host, so leaving either blank on save
 * means "keep the stored value" (enforced server-side, see api/settings.py's put_host). A
 * pasted key (migration 014) is an *additional* way to satisfy key auth, alongside `key_path`
 * -- not a replacement -- and wins over `key_path` when both are set; `saved.active_key_source`
 * says which one is actually in use.
 */
export function ConnectionTab() {
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [saved, setSaved] = useState<HostOut | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestConnectionResponse | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getHost()
      .then((host) => {
        setSaved(host)
        if (host) {
          setForm({
            name: host.name,
            address: host.address,
            port: String(host.port),
            username: host.username,
            auth_method: host.auth_method,
            key_path: host.key_path ?? '',
            password: '',
            ssh_key: '',
            known_hosts_policy: host.known_hosts_policy,
          })
        }
      })
      .finally(() => setLoading(false))
  }, [])

  const update = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      const host = await putHost({
        name: form.name,
        address: form.address,
        port: Number(form.port) || 22,
        username: form.username,
        auth_method: form.auth_method,
        key_path: form.auth_method === 'key' ? form.key_path || null : null,
        password: form.password || null,
        ssh_key: form.auth_method === 'key' ? form.ssh_key || null : null,
        known_hosts_policy: form.known_hosts_policy,
      })
      setSaved(host)
      update('password', '')
      update('ssh_key', '')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await testHost({
        name: form.name || null,
        address: form.address || null,
        port: form.port ? Number(form.port) : null,
        username: form.username || null,
        auth_method: form.auth_method,
        key_path: form.auth_method === 'key' ? form.key_path || null : null,
        password: form.password || null,
        ssh_key: form.auth_method === 'key' ? form.ssh_key || null : null,
        known_hosts_policy: form.known_hosts_policy,
      })
      setTestResult(result)
    } catch (err) {
      setTestResult({ ok: false, error_class: 'UNKNOWN', message: err instanceof Error ? err.message : String(err) })
    } finally {
      setTesting(false)
    }
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex max-w-lg flex-col gap-4">
      {saved?.credentials_need_reentry && (
        <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
          Stored credentials could not be decrypted (DESIGN.md §8) — re-enter the password below
          and save.
        </div>
      )}

      <div className="grid grid-cols-2 gap-3">
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Name</span>
          <input className={inputClasses} value={form.name} onChange={(e) => update('name', e.target.value)} />
        </label>
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>Port</span>
          <input className={inputClasses} value={form.port} onChange={(e) => update('port', e.target.value)} />
        </label>
      </div>

      <label className="flex flex-col gap-1">
        <span className={labelClasses}>Address</span>
        <input className={inputClasses} value={form.address} onChange={(e) => update('address', e.target.value)} />
      </label>

      <label className="flex flex-col gap-1">
        <span className={labelClasses}>Username</span>
        <input className={inputClasses} value={form.username} onChange={(e) => update('username', e.target.value)} />
      </label>

      <label className="flex flex-col gap-1">
        <span className={labelClasses}>Auth method</span>
        <select
          className={inputClasses}
          value={form.auth_method}
          onChange={(e) => update('auth_method', e.target.value as AuthMethod)}
        >
          <option value="key">SSH key</option>
          <option value="agent">SSH agent</option>
          <option value="password">Password</option>
        </select>
      </label>

      {form.auth_method === 'key' && (
        <>
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>
              Private key{' '}
              {saved?.has_ssh_key && (
                <span className="text-zinc-400">(leave blank to keep the stored one)</span>
              )}
            </span>
            <textarea
              className={`${inputClasses} h-28 font-mono text-xs`}
              value={form.ssh_key}
              onChange={(e) => update('ssh_key', e.target.value)}
              placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;...&#10;-----END OPENSSH PRIVATE KEY-----"
              autoComplete="off"
              spellCheck={false}
            />
            <span className="text-xs text-zinc-500 dark:text-zinc-400">
              Encrypted at rest, decrypted only in memory. Passphrase-protected keys are
              rejected — strip the passphrase first, or use Key path below instead.
            </span>
          </label>

          {/* No Browse button here (GitHub issue #4, prompts/done/2026-08-16-path-browse-dialog.md)
           * -- deliberately: this field names a *file*, not a directory, and the pasted-key
           * alternative above is already the preferred path. */}
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>Key path</span>
            <input className={inputClasses} value={form.key_path} onChange={(e) => update('key_path', e.target.value)} />
            <span className="text-xs text-zinc-500 dark:text-zinc-400">
              A key file already mounted into the container. Ignored while a pasted key above is
              stored — a pasted key always takes priority.
            </span>
          </label>

          {saved?.active_key_source && (
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              Currently using:{' '}
              <span className="font-medium text-zinc-700 dark:text-zinc-300">
                {saved.active_key_source === 'pasted' ? 'the pasted key' : 'Key path'}
              </span>
            </p>
          )}
        </>
      )}

      {form.auth_method === 'password' && (
        <label className="flex flex-col gap-1">
          <span className={labelClasses}>
            Password {saved?.has_password && <span className="text-zinc-400">(leave blank to keep the stored one)</span>}
          </span>
          <input
            type="password"
            className={inputClasses}
            value={form.password}
            onChange={(e) => update('password', e.target.value)}
            autoComplete="new-password"
          />
        </label>
      )}

      <label className="flex flex-col gap-1">
        <span className={labelClasses}>
          Known-hosts policy
          {/* Second demonstration of `FieldHelp` (2026-08-13, prompts/2026-08-13-docs-section.md).
           * Picked because this control has three options, no inline explanation at all, and the
           * consequence of the wrong pick is either "it silently stops connecting one day" or
           * "it never checked in the first place" -- neither of which the option labels convey. */}
          <FieldHelp label="Known-hosts policy">
            <p>What happens when the seedbox presents its SSH host key.</p>
            <p>
              <strong>Accept and pin on first use</strong> (default) — trust whatever key the
              server shows the first time, remember it, and refuse to connect if it ever changes.
              This catches a swapped-out server later, but trusts the very first connection
              blindly.
            </p>
            <p>
              <strong>Strict</strong> — only ever accept a key that has already been pinned.
              Safest, but it will refuse to connect until something has pinned one, so it is not
              a good first setting on a new install.
            </p>
            <p>
              <strong>Insecure</strong> — never verify the host key. Only reasonable on a
              network you fully control.
            </p>
          </FieldHelp>
        </span>
        <select
          className={inputClasses}
          value={form.known_hosts_policy}
          onChange={(e) => update('known_hosts_policy', e.target.value as KnownHostsPolicy)}
        >
          <option value="accept-and-pin">Accept and pin on first use (default)</option>
          <option value="strict">Strict (only a previously pinned key)</option>
          <option value="insecure">Insecure (never verify)</option>
        </select>
      </label>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          type="button"
          onClick={handleTest}
          disabled={testing}
          className="rounded-md border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"
        >
          {testing ? 'Testing…' : 'Test connection'}
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      {testResult && (
        <div
          className={`rounded-md px-3 py-2 text-sm ${
            testResult.ok
              ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300'
              : 'bg-red-50 text-red-800 dark:bg-red-950/40 dark:text-red-300'
          }`}
        >
          {testResult.ok ? 'Connected.' : `Failed (${testResult.error_class}): ${testResult.message}`}
        </div>
      )}
    </div>
  )
}
