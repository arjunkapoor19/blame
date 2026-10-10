import { useEffect, useState } from "react"
import { useStatus } from "../api"
import { href, type Route } from "../lib/router"
import { useTheme } from "../lib/theme"
import { Kbd } from "./Chip"
import { Icon } from "./Icon"

export function TopBar({ route, onSearch }: { route: Route; onSearch: () => void }) {
  const [theme, nextTheme] = useTheme()
  const tab = (name: string, label: string, active: boolean) => (
    <a
      href={href({ name } as Route)}
      className={`rounded-md px-2.5 py-1 text-[12.5px] font-medium transition-colors ${
        active ? "bg-sunken text-ink" : "text-ink-2 hover:bg-sunken/70 hover:text-ink"
      }`}
    >
      {label}
    </a>
  )
  return (
    <header className="sticky top-0 z-20 flex h-12 shrink-0 items-center gap-3 border-b border-edge bg-panel/80 px-4 backdrop-blur-md">
      <a href="#/" className="flex items-center gap-2" aria-label="ah home">
        <Logo />
        <span className="text-[13.5px] font-semibold tracking-tight max-sm:hidden">
          ah <span className="font-normal text-ink-3">agent history</span>
        </span>
      </a>
      <nav className="ml-2 flex gap-0.5">
        {tab("home", "Files", route.name === "home" || route.name === "file")}
        {tab("sessions", "Sessions", route.name === "sessions" || route.name === "session")}
      </nav>
      <button
        type="button"
        onClick={onSearch}
        className="ml-auto flex h-8 w-64 items-center gap-2 rounded-lg border border-edge bg-canvas px-2.5 text-[12.5px] text-ink-3 transition-colors hover:border-edge-strong hover:text-ink-2 max-md:w-8 max-md:justify-center max-md:px-0"
      >
        <Icon name="search" size={14} />
        <span className="flex-1 text-left max-md:hidden">Go to file…</span>
        <span className="flex gap-0.5 max-md:hidden">
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </span>
      </button>
      <SyncPill />
      <button
        type="button"
        onClick={nextTheme}
        className="flex size-8 items-center justify-center rounded-lg text-ink-2 transition-colors hover:bg-sunken hover:text-ink"
        title={`Theme: ${theme}`}
        aria-label={`Theme: ${theme}`}
      >
        <Icon name={theme === "system" ? "system" : theme === "light" ? "sun" : "moon"} size={15} />
      </button>
    </header>
  )
}

function Logo() {
  return (
    <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" className="fill-ink" />
      <rect x="8" y="8" width="4" height="16" rx="2" fill="var(--agent-0)" />
      <rect x="14" y="12" width="4" height="12" rx="2" fill="var(--agent-4)" />
      <rect x="20" y="6" width="4" height="18" rx="2" fill="var(--agent-7)" />
    </svg>
  )
}

/** Live, or syncing, or "3 new sessions": what the background sync is doing. */
function SyncPill() {
  const status = useStatus()
  const [fresh, setFresh] = useState(false)
  const at = status?.ingested_at
  useEffect(() => {
    if (!at || Date.now() - at > 10_000) return
    setFresh(true)
    const timer = setTimeout(() => setFresh(false), 4000)
    return () => clearTimeout(timer)
  }, [at])

  if (!status)
    return <span className="flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[11.5px] text-ink-3 max-sm:hidden">connecting…</span>
  const failed = status.failed.length
  const [dot, text, title] = status.syncing
    ? ["bg-focus animate-pulse", "Syncing", "Reading agent logs on this machine"]
    : fresh
      ? ["bg-ok", `+${status.ingested} session${status.ingested === 1 ? "" : "s"}`, "New agent sessions synced"]
      : failed
        ? ["bg-warn", `${failed} unreadable`, status.failed.join("\n")]
        : ["bg-ok", "Live", "Following agent logs and hooks as they write"]
  return (
    <span
      className={`flex h-7 items-center gap-1.5 rounded-full border border-edge px-2.5 text-[11.5px] font-medium text-ink-2 transition-all max-sm:hidden ${
        fresh ? "border-transparent bg-[color-mix(in_oklab,var(--ok)_14%,transparent)] text-ok" : ""
      }`}
      title={title}
    >
      <span className="relative flex size-2">
        {!status.syncing && !failed && <span className={`absolute inset-0 animate-ping rounded-full opacity-40 ${dot} [animation-duration:2.4s]`} />}
        <span className={`relative size-2 rounded-full ${dot}`} />
      </span>
      {text}
    </span>
  )
}
