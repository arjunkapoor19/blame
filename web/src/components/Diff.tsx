import { useEffect, useMemo, useRef, useState } from "react"
import type { Change } from "../api"
import { numbered, stats } from "../lib/diff"
import { Icon } from "./Icon"

const PREVIEW = 10 // lines shown of a diff that isn't the one that wrote the line
const WINDOW = 8 // lines either side of the marked line

type Props = { change: Change; color?: string; open?: boolean; showPath?: boolean }

/** A file change as a diff with line numbers. The change that wrote the blamed line opens around
 * that line, marked and pulsing once; others start as a short preview. */
export function Diff({ change, color, open = false, showPath = true }: Props) {
  const rows = useMemo(
    () => (change.kind === "add" && !change.lines.some((l) => l.startsWith("@@")) ? numbered(change.lines, 1, 1) : numbered(change.lines)),
    [change],
  )
  const markedRow = change.marked == null ? -1 : rows.findIndex((r) => r.at === change.marked)
  const { added, removed } = stats(change.lines)
  const [range, setRange] = useState<[number, number]>(() => {
    if (markedRow >= 0) return [Math.max(0, markedRow - WINDOW), Math.min(rows.length, markedRow + WINDOW + 1)]
    return [0, open ? rows.length : Math.min(rows.length, PREVIEW)]
  })
  const markRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    markRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" })
  }, [])

  const [from, to] = range
  const above = from + change.cut_before
  const below = rows.length - to + change.cut_after
  const width = String(Math.max(...rows.map((r) => r.new ?? r.old ?? 0), 1)).length

  return (
    <div className="overflow-hidden rounded-lg border border-edge bg-panel font-mono text-[11.5px] leading-[19px]">
      {showPath && (
        <div className="flex items-center gap-2 border-b border-edge bg-sunken/60 px-2.5 py-1 font-sans text-[11px] text-ink-2">
          <Icon name="file" size={12} />
          <span className="truncate font-medium text-ink">{change.rel}</span>
          {change.kind !== "update" && <span className="text-ink-3">{change.kind === "add" ? "new file" : change.kind}</span>}
          <span className="ml-auto flex shrink-0 gap-2 tabular-nums">
            {added > 0 && <span className="text-add">+{added}</span>}
            {removed > 0 && <span className="text-del">−{removed}</span>}
          </span>
        </div>
      )}
      {change.kind === "delete" ? (
        <div className="px-3 py-2 font-sans text-del">File deleted</div>
      ) : (
        <div className="overflow-x-auto">
          {above > 0 && (
            <Expand onClick={() => setRange([Math.max(0, from - 20), to])} disabled={from === 0}>
              {from === 0 ? `${above} lines not sent` : `${above} more lines above`}
            </Expand>
          )}
          <div className="min-w-max">
            {rows.slice(from, to).map((row, i) => {
              const marked = from + i === markedRow
              if (row.tag === "@")
                return (
                  <div key={row.at} className="bg-sunken/70 px-3 text-ink-3 select-none">
                    {row.text}
                  </div>
                )
              const tone = row.tag === "+" ? "var(--add)" : row.tag === "-" ? "var(--del)" : null
              return (
                <div
                  key={row.at}
                  ref={marked ? markRef : undefined}
                  className={`group relative flex ${marked ? "animate-pulse-line" : ""}`}
                  style={{
                    background: marked
                      ? `color-mix(in oklab, ${color ?? "var(--focus)"} 20%, transparent)`
                      : tone ? `color-mix(in oklab, ${tone} 9%, transparent)` : undefined,
                    "--c": color,
                  } as React.CSSProperties}
                >
                  {marked && <span className="absolute inset-y-0 left-0 w-[3px]" style={{ background: color ?? "var(--focus)" }} />}
                  <span className="w-[calc(var(--w)*1ch+12px)] shrink-0 pr-1.5 text-right text-ink-3/70 select-none" style={{ "--w": width } as React.CSSProperties}>
                    {row.old ?? ""}
                  </span>
                  <span className="w-[calc(var(--w)*1ch+12px)] shrink-0 pr-1.5 text-right text-ink-3/70 select-none" style={{ "--w": width } as React.CSSProperties}>
                    {row.new ?? ""}
                  </span>
                  <span className="w-4 shrink-0 text-center select-none" style={{ color: tone ?? undefined }}>
                    {row.tag === " " ? "" : row.tag === "-" ? "−" : "+"}
                  </span>
                  <span className="pr-4 whitespace-pre">{row.text || " "}</span>
                  {marked && (
                    <span
                      className="sticky right-0 ml-auto flex items-center gap-1 pr-2 pl-3 font-sans text-[10.5px] font-semibold whitespace-nowrap"
                      style={{ color: color ?? "var(--focus)" }}
                    >
                      ← this line
                    </span>
                  )}
                </div>
              )
            })}
          </div>
          {below > 0 && (
            <Expand onClick={() => setRange([from, Math.min(rows.length, to + 40)])} disabled={to === rows.length}>
              {to === rows.length ? `${below} lines not sent` : `${rows.length - to} more lines`}
            </Expand>
          )}
        </div>
      )}
    </div>
  )
}

function Expand({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled: boolean }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="flex w-full items-center gap-1.5 bg-sunken/50 px-3 py-0.5 text-left font-sans text-[11px] text-ink-3 transition-colors enabled:hover:bg-sunken enabled:hover:text-ink-2"
    >
      {!disabled && <Icon name="down" size={11} />}
      {children}
    </button>
  )
}
