import type { ReactNode } from "react"
import { authorLabel, nameColor } from "../lib/colors"

/** An agent's name in its colour: the one visual identity an agent has across the viewer. */
export function AgentChip({ name, size = "sm", title }: { name: string; size?: "xs" | "sm" | "md"; title?: string }) {
  const color = nameColor(name)
  const sizes = { xs: "h-[18px] px-1.5 text-[10.5px] gap-1", sm: "h-5 px-2 text-[11px] gap-1.5", md: "h-6 px-2.5 text-xs gap-1.5" }
  return (
    <span
      className={`tint-strong inline-flex shrink-0 items-center rounded-full font-medium tracking-tight ${sizes[size]}`}
      style={{ "--c": color, color: `color-mix(in oklab, ${color} 78%, var(--ink))` } as React.CSSProperties}
      title={title}
    >
      <span className="size-1.5 rounded-full" style={{ background: color }} />
      {authorLabel(name)}
    </span>
  )
}

export function Dot({ name, className = "size-2" }: { name: string; className?: string }) {
  return <span className={`inline-block shrink-0 rounded-full ${className}`} style={{ background: nameColor(name) }} />
}

const TONES = {
  ok: "text-ok bg-[color-mix(in_oklab,var(--ok)_12%,transparent)]",
  bad: "text-bad bg-[color-mix(in_oklab,var(--bad)_12%,transparent)]",
  warn: "text-warn bg-[color-mix(in_oklab,var(--warn)_14%,transparent)]",
  plain: "text-ink-2 bg-sunken",
} as const

export function Pill({ tone = "plain", children, title }: { tone?: keyof typeof TONES; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex max-w-full items-center gap-1 truncate rounded-md px-1.5 py-px text-[11px] font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  )
}

export function statusTone(status: string | null | undefined): keyof typeof TONES {
  if (status === "completed") return "ok"
  if (status === "failed" || status === "declined") return "bad"
  if (status === "interrupted" || status === "incomplete") return "warn"
  return "plain"
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-[18px] min-w-[18px] items-center justify-center rounded border border-edge-strong bg-panel px-1 font-sans text-[10.5px] font-medium text-ink-2 shadow-[0_1px_0_var(--edge-strong)]">
      {children}
    </kbd>
  )
}

export function short(id: string | null | undefined, n = 8): string {
  return id ? id.slice(-n) : ""
}
