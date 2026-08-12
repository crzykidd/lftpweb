import { useEffect, useState } from 'react'
import { backupDownloadUrl, backupNow, getBackupSettings, listBackups, putBackupSettings } from '../../api/client'
import type { BackupInfoOut, BackupSettingsOut } from '../../api/types'
import { formatBytes } from '../../lib/format'

const inputClasses =
  'w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100'
const labelClasses = 'text-sm font-medium text-zinc-700 dark:text-zinc-300'
const buttonClasses =
  'w-fit rounded-md bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-zinc-700 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300'

const EMPTY_SETTINGS: BackupSettingsOut = { interval_days: 1, keep_count: 7 }

/** Settings → Backup (DESIGN.md §10.2): manual "Backup now," the list of what's on disk with
 * download links, and the daily/keep-7 schedule (both configurable). The backup that actually
 * matters most -- the one taken automatically right before a schema migration -- has no
 * control here at all; it isn't optional and isn't something this page can turn off (see
 * core/backup.py's module docstring and docs/decisions.md).
 */
export function BackupTab() {
  const [settings, setSettings] = useState<BackupSettingsOut>(EMPTY_SETTINGS)
  const [backups, setBackups] = useState<BackupInfoOut[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [runningBackup, setRunningBackup] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadAll = async () => {
    setError(null)
    try {
      const [s, b] = await Promise.all([getBackupSettings(), listBackups()])
      setSettings(s)
      setBackups(b.backups)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleSaveSettings = async () => {
    setError(null)
    setSaving(true)
    try {
      const saved = await putBackupSettings(settings)
      setSettings(saved)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const handleBackupNow = async () => {
    setError(null)
    setRunningBackup(true)
    try {
      await backupNow()
      const b = await listBackups()
      setBackups(b.backups)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setRunningBackup(false)
    }
  }

  if (loading) return <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</p>

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        <code>VACUUM INTO</code>, never a file copy -- atomic and WAL-safe, so a backup taken
        mid-transfer can't capture a torn database. The encryption secret is never included
        (DESIGN.md §8): a backup here is safe to download and store anywhere, but restoring it
        to a fresh install needs the seedbox password re-entered.
      </p>

      <div className="flex flex-col gap-3 rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Schedule</h3>
        <div className="flex flex-wrap gap-4">
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>Every (days)</span>
            <input
              type="number"
              min={0.01}
              step={0.5}
              className={`${inputClasses} max-w-32`}
              value={settings.interval_days}
              onChange={(e) =>
                setSettings((prev) => ({
                  ...prev,
                  interval_days: Math.max(0.01, Number(e.target.value) || 1),
                }))
              }
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className={labelClasses}>Keep</span>
            <input
              type="number"
              min={1}
              className={`${inputClasses} max-w-32`}
              value={settings.keep_count}
              onChange={(e) =>
                setSettings((prev) => ({
                  ...prev,
                  keep_count: Math.max(1, Math.round(Number(e.target.value) || 1)),
                }))
              }
            />
          </label>
        </div>
        <button type="button" disabled={saving} onClick={handleSaveSettings} className={buttonClasses}>
          {saving ? 'Saving…' : 'Save schedule'}
        </button>
      </div>

      <div>
        <button type="button" disabled={runningBackup} onClick={handleBackupNow} className={buttonClasses}>
          {runningBackup ? 'Backing up…' : 'Backup now'}
        </button>
      </div>

      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Backups on disk</h3>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-zinc-500 dark:text-zinc-400">
              <th className="py-1 pr-4 font-medium">File</th>
              <th className="py-1 pr-4 font-medium">Created</th>
              <th className="py-1 pr-4 font-medium">Size</th>
              <th className="py-1 font-medium">Download</th>
            </tr>
          </thead>
          <tbody>
            {backups.map((b) => (
              <tr key={b.filename} className="border-t border-zinc-100 dark:border-zinc-800">
                <td className="py-1 pr-4 text-zinc-800 dark:text-zinc-200">{b.filename}</td>
                <td className="py-1 pr-4 text-zinc-600 dark:text-zinc-400">
                  {new Date(b.created_at).toLocaleString()}
                </td>
                <td className="py-1 pr-4 text-zinc-600 dark:text-zinc-400">
                  {formatBytes(b.size_bytes)}
                </td>
                <td className="py-1">
                  <a
                    className="text-zinc-700 underline hover:text-zinc-900 dark:text-zinc-300 dark:hover:text-zinc-100"
                    href={backupDownloadUrl(b.filename)}
                    download
                  >
                    Download
                  </a>
                </td>
              </tr>
            ))}
            {backups.length === 0 && (
              <tr>
                <td colSpan={4} className="py-2 text-zinc-500 dark:text-zinc-400">
                  No backups yet -- click "Backup now" or wait for the schedule.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
