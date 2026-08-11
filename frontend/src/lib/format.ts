const UNITS = ['B', 'KB', 'MB', 'GB', 'TB']

export function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const exp = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), UNITS.length - 1)
  const value = bytes / 1024 ** exp
  return `${value.toFixed(exp === 0 ? 0 : 1)} ${UNITS[exp]}`
}

export function formatRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`
}
