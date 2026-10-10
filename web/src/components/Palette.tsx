import { useEffect, useMemo, useRef, useState } from "react"
import { api, useApi, type FileEntry, type SessionSummary } from "../api"
import { rank } from "../lib/fuzzy"
import { go } from "../lib/router"
import { ago } from "../lib/time"
import { AgentChip, Dot, Kbd } from "./Chip"
import { Icon } from "./Icon"

type Item =
  | { type: "file"; file: FileEntry; label: string; where: string }
  | { type: "session"; session: SessionSummary; label: string }

/** ⌘K: jump to any file or session ah knows, by a few letters of its name. */
export function Palette({ onClose }: { onClose: () => void }) {
  const files = useApi<FileEntry[]>(api.files).data ?? []
  const sessions = useApi<SessionSummary[]>(api.sessions).data ?? []
  const [query, setQuery] = useState("")
  const [active, setActive] = useState(0)
  const list = useRef<HTMLDivElement>(null)

  const items = useMemo<Item[]>(() => {
    const fileItems: Item[] = files.map((file) => {
      const root = file.root.split("/").pop() ?? ""
      const rel = file.path.startsWith(file.root + "/") ? file.path.slice(file.root.length + 1) : file.path
      return { type: "file", file, label: rel, where: root }
    })
    const sessionItems: Item[] = sessions.map((session) => ({
      type: "session",
      session,
      label: (session.first_input ?? session.id).replace(/\s+/g, " ").replace(/^▎\s*/, ""),
    }))
    const text = (item: Item) =>
      item.type === "file" ? `${item.where}/${item.label}` : `${item.session.agent} ${item.label} ${item.session.id}`
    return [...rank(query, fileItems, text, 40), ...rank(query, sessionItems, text, query ? 12 : 6)]
  }, [files, sessions, query])

  useEffect(() => setActive(0), [query])
  useEffect(() => {
    list.current?.querySelector(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" })
  }, [active])

  const open = (item: Item | undefined) => {
    if (!item) return
    onClose()
    if (item.type === "file") go({ name: "file", path: item.file.path, line: null })
    else go({ name: "session", id: item.session.id })
  }

  const onKey = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") setActive((a) => Math.min(a + 1, items.length - 1))
    else if (event.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0))
    else if (event.key === "Enter") open(items[active])
    else if (event.key === "Escape") onClose()
    else return
    event.preventDefault()
  }

  const firstSession = items.findIndex((i) => i.type === "session")
  return (
    <div className="animate-fade-in fixed inset-0 z-50 flex items-start justify-center bg-black/25 px-4 pt-[12vh] backdrop-blur-[2px]" onMouseDown={onClose}>
      <div
        className="animate-rise w-full max-w-[620px] overflow-hidden rounded-2xl border border-edge bg-raised shadow-[var(--shadow-pop)]"
        onMouseDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Go to file or session"
      >
        <div className="flex items-center gap-2.5 border-b border-edge px-4">
          <Icon name="search" size={16} className="text-ink-3" />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKey}
            placeholder="Go to a file or session…"
            className="h-12 flex-1 bg-transparent text-[14px] outline-none placeholder:text-ink-3"
            role="combobox"
            aria-expanded="true"
          />
          <Kbd>esc</Kbd>
        </div>
        <div ref={list} className="max-h-[52vh] overflow-y-auto p-1.5">
          {items.length === 0 && <div className="px-3 py-8 text-center text-[12.5px] text-ink-3">Nothing matches “{query}”.</div>}
          {items.map((item, i) => (
            <div key={item.type === "file" ? item.file.path : item.session.id}>
              {(i === 0 && item.type === "file") || i === firstSession ? (
                <div className="px-2.5 pt-2 pb-1 text-[10.5px] font-semibold tracking-[0.08em] text-ink-3 uppercase">
                  {item.type === "file" ? "Files" : "Sessions"}
                </div>
              ) : null}
              <button
                type="button"
                data-i={i}
                onMouseMove={() => setActive(i)}
                onClick={() => open(item)}
                className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left ${i === active ? "bg-sunken" : ""}`}
              >
                {item.type === "file" ? (
                  <>
                    <Icon name="file" size={14} className="text-ink-3" />
                    <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">
                      <span className="text-ink-3">{item.where}/</span>
                      {item.label}
                    </span>
                    <span className="flex gap-1">
                      {item.file.agents.map((a) => (
                        <Dot key={a} name={a.split("-")[0]} />
                      ))}
                    </span>
                    <span className="w-20 shrink-0 text-right text-[11px] text-ink-3">{ago(item.file.last_changed)}</span>
                  </>
                ) : (
                  <>
                    <AgentChip name={item.session.agent.split("-")[0]} size="xs" />
                    <span className="min-w-0 flex-1 truncate text-[12.5px]">{item.label}</span>
                    <span className="w-20 shrink-0 text-right text-[11px] text-ink-3">{ago(item.session.started_at)}</span>
                  </>
                )}
                {i === active && <Icon name="arrow" size={12} className="text-ink-3" />}
              </button>
            </div>
          ))}
        </div>
        <div className="flex items-center gap-3 border-t border-edge bg-sunken/50 px-4 py-2 text-[11px] text-ink-3">
          <span className="flex items-center gap-1"><Kbd>↑</Kbd><Kbd>↓</Kbd> move</span>
          <span className="flex items-center gap-1"><Kbd>↵</Kbd> open</span>
        </div>
      </div>
    </div>
  )
}

export function Shortcuts({ onClose }: { onClose: () => void }) {
  const groups: [string, [string[], string][]][] = [
    ["Anywhere", [[["⌘", "K"], "Go to a file or session"], [["g", "s"], "Sessions"], [["g", "f"], "Files"], [["?"], "These shortcuts"]]],
    ["In a file", [[["j"], "Next line"], [["k"], "Previous line"], [["]"], "Next block"], [["["], "Previous block"], [["↵"], "Open the first agent line"], [["esc"], "Close the story"]]],
  ]
  return (
    <div className="animate-fade-in fixed inset-0 z-50 flex items-center justify-center bg-black/25 p-4 backdrop-blur-[2px]" onMouseDown={onClose}>
      <div className="animate-rise w-full max-w-sm rounded-2xl border border-edge bg-raised p-5 shadow-[var(--shadow-pop)]" onMouseDown={(e) => e.stopPropagation()}>
        <h2 className="mb-3 text-[14px] font-semibold">Keyboard shortcuts</h2>
        {groups.map(([title, keys]) => (
          <div key={title} className="mb-3 last:mb-0">
            <div className="mb-1.5 text-[10.5px] font-semibold tracking-[0.08em] text-ink-3 uppercase">{title}</div>
            {keys.map(([combo, what]) => (
              <div key={what} className="flex items-center justify-between py-1 text-[12.5px]">
                <span className="text-ink-2">{what}</span>
                <span className="flex gap-1">{combo.map((k) => <Kbd key={k}>{k}</Kbd>)}</span>
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
