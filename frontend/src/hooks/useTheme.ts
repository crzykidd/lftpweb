import { useCallback, useEffect, useState } from 'react'

export type Theme = 'dark' | 'light' | 'system'

const STORAGE_KEY = 'lftpweb-theme'

function applyTheme(theme: Theme) {
  const dark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
}

function readStoredTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY)
  return stored === 'dark' || stored === 'light' ? stored : 'system'
}

/** Dark / light / system, defaulting to system (DESIGN.md §9.1). Persisted to
 * localStorage; when set to `system`, actually follows OS changes live.
 */
export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setThemeState] = useState<Theme>(readStoredTheme)

  useEffect(() => {
    applyTheme(theme)

    if (theme !== 'system') return

    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => applyTheme('system')
    media.addEventListener('change', onChange)
    return () => media.removeEventListener('change', onChange)
  }, [theme])

  const setTheme = useCallback((next: Theme) => {
    if (next === 'system') {
      localStorage.removeItem(STORAGE_KEY)
    } else {
      localStorage.setItem(STORAGE_KEY, next)
    }
    setThemeState(next)
  }, [])

  return [theme, setTheme]
}
