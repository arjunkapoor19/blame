import { useMemo, useState } from "react"
import { api, useApi, type SessionSummary } from "../api"
import { Dot } from "../components/Chip"
import { Icon } from "../components/Icon"
import { rank } from "../lib/fuzzy"
import { day } from "../lib/time"
import { SessionRow } from "./Home"

/** Every session, newest first, grouped by day; filter by agent or by words from the prompt. */
export function Sessions() {
  const { data } = useApi<SessionSummary[]>(api.sessions)
  const [query, setQuery] = useState("")
  const [agent, setAgent] = useState<string | null>(null)
  const agents = useMemo(() => {
    const count = new Map<string, number>()
    for (const s of data ?? []) count.set(s.agent.split("-")[0], (count.get(s.agent.split("-")[0]) ?? 0) + 1)
    return [...count.entries()].sort((a, b) => b[1] - a[1])
  }, [data])
  const groups = useMemo(() => {
    const filtered = (data ?? []).filter((s) => !agent || s.agent.split("-")[0] === agent)
    const shown = query ? rank(query, filtered, (s) => `${s.first_input ?? ""} ${s.cwd ?? ""} ${s.id}`, 200) : filtered
    const out = new Map<string, SessionSummary[]>()
    for (const s of shown) {
      const key = query ? "Best matches" : s.started_at ? day(s.started_at) : "Undated"
      out.set(key, [...(out.get(key) ?? []), s])
    }
    return [...out.entries()]
  }, [data, query, agent])

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-5 py-8 md:px-8">
        <h1 className="mb-4 text-[20px] font-semibold tracking-tight">Sessions</h1>
        <div className="sticky top-0 z-10 -mx-2 mb-4 flex flex-wrap items-center gap-2 bg-canvas/90 px-2 py-2 backdrop-blur">
          <label className="flex h-9 min-w-56 flex-1 items-center gap-2 rounded-lg border border-edge bg-panel px-3 focus-within:border-focus">
            <Icon name="search" size={14} className="text-ink-3" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search prompts, folders, ids…"
              className="flex-1 bg-transparent text-[13px] outline-none placeholder:text-ink-3"
            />
          </label>
          <div className="flex gap-1">
            {agents.map(([name, n]) => (
              <button
                key={name}
                type="button"
                onClick={() => setAgent(agent === name ? null : name)}
                className={`flex h-9 items-center gap-1.5 rounded-lg border px-2.5 text-[12px] transition-colors ${
                  agent === name ? "border-edge-strong bg-sunken" : "border-edge bg-panel hover:bg-sunken/70"
                } ${agent && agent !== name ? "opacity-50" : ""}`}
              >
                <Dot name={name} /> {name} <span className="text-ink-3 tabular-nums">{n}</span>
              </button>
            ))}
          </div>
        </div>
        {!data && <div className="skeleton h-40 w-full rounded-xl" />}
        {data && groups.length === 0 && <p className="py-10 text-center text-[12.5px] text-ink-3">No sessions match.</p>}
        <div className="space-y-6">
          {groups.map(([label, list]) => (
            <section key={label} className="animate-rise">
              <h2 className="mb-1.5 px-1 text-[11px] font-semibold tracking-[0.08em] text-ink-3 uppercase">{label}</h2>
              <div className="overflow-hidden rounded-xl border border-edge bg-panel">
                {list.map((s) => <SessionRow key={s.id} session={s} />)}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
