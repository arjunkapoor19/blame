import { authorLabel, nameColor } from "../lib/colors"

/** Who wrote the file, as one proportional bar with a legend. Hovering a name lights up its lines;
 * clicking keeps the others dimmed until clicked again. */
export function AuthorBar({
  totals,
  focus,
  pinned,
  onFocus,
  onPin,
}: {
  totals: Record<string, number>
  focus: string | null
  pinned: string | null
  onFocus: (name: string | null) => void
  onPin: (name: string | null) => void
}) {
  const order = (name: string) => (name === "untracked" ? 3 : name === "·" ? 2 : name === "you" ? 1 : 0)
  const entries = Object.entries(totals).sort((a, b) => order(a[0]) - order(b[0]) || b[1] - a[1])
  const total = entries.reduce((sum, [, n]) => sum + n, 0) || 1
  const active = focus ?? pinned
  return (
    <div className="space-y-2" onMouseLeave={() => onFocus(null)}>
      <div className="flex h-2 gap-[2px] overflow-hidden rounded-full">
        {entries.map(([name, n]) => (
          <button
            key={name}
            type="button"
            aria-label={`${authorLabel(name)}: ${n} lines`}
            onMouseEnter={() => onFocus(name)}
            onClick={() => onPin(pinned === name ? null : name)}
            className="h-full transition-[opacity,filter] duration-200 first:rounded-l-full last:rounded-r-full"
            style={{
              width: `${(n / total) * 100}%`,
              background: name === "untracked" ? "var(--edge)" : nameColor(name),
              opacity: active && active !== name ? 0.25 : 1,
            }}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-1 gap-y-1">
        {entries.map(([name, n]) => (
          <button
            key={name}
            type="button"
            onMouseEnter={() => onFocus(name)}
            onClick={() => onPin(pinned === name ? null : name)}
            className={`inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-[11.5px] transition-all ${
              pinned === name ? "bg-sunken ring-1 ring-edge-strong" : "hover:bg-sunken"
            } ${active && active !== name ? "opacity-45" : ""}`}
          >
            <span
              className="size-2 rounded-full"
              style={{ background: name === "untracked" ? "var(--edge-strong)" : nameColor(name) }}
            />
            <span className="font-medium text-ink">{name === "untracked" ? "not attributed" : authorLabel(name)}</span>
            <span className="text-ink-3 tabular-nums">{n}</span>
          </button>
        ))}
      </div>
    </div>
  )
}
