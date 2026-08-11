import { useTheme, type Theme } from '../hooks/useTheme'

const OPTIONS: { value: Theme; label: string }[] = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
]

export function ThemeToggle() {
  const [theme, setTheme] = useTheme()

  return (
    <div className="flex overflow-hidden rounded-md border border-zinc-300 text-xs dark:border-zinc-700">
      {OPTIONS.map(({ value, label }) => (
        <button
          key={value}
          type="button"
          onClick={() => setTheme(value)}
          aria-pressed={theme === value}
          className={`px-2 py-1 transition-colors ${
            theme === value
              ? 'bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900'
              : 'bg-transparent text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  )
}
