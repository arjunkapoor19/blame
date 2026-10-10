const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

export function ago(ms: number | null | undefined, now = Date.now()): string {
  if (ms == null) return ""
  const d = now - ms
  if (d < MINUTE) return "just now"
  if (d < HOUR) return `${Math.floor(d / MINUTE)} min ago`
  if (d < DAY) return `${Math.floor(d / HOUR)} h ago`
  if (d < 2 * DAY) return "yesterday"
  if (d < 7 * DAY) return `${Math.floor(d / DAY)} days ago`
  const date = new Date(ms)
  const sameYear = date.getFullYear() === new Date(now).getFullYear()
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: sameYear ? undefined : "numeric" })
}

export function exact(ms: number | null | undefined): string {
  if (ms == null) return ""
  return new Date(ms).toLocaleString(undefined, {
    weekday: "short", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  })
}

export function clock(ms: number | null | undefined): string {
  if (ms == null) return "--:--:--"
  return new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
}

export function duration(start: number | null | undefined, end: number | null | undefined): string {
  if (start == null || end == null || end < start) return ""
  const s = (end - start) / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${Math.round(s % 60)}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

/** "Today", "Yesterday", or the date: for grouping lists by day. */
export function day(ms: number, now = Date.now()): string {
  const date = new Date(ms)
  const today = new Date(now)
  const midnight = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()
  if (ms >= midnight) return "Today"
  if (ms >= midnight - DAY) return "Yesterday"
  return date.toLocaleDateString(undefined, {
    weekday: "long", month: "long", day: "numeric",
    year: date.getFullYear() === today.getFullYear() ? undefined : "numeric",
  })
}
