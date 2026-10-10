/** The JSON API `ah view` serves (agent_history/view.py), and hooks to read it live. */
import { useEffect, useState, useSyncExternalStore } from "react"

export type Author = {
  key: string | null
  kind: "agent" | "outside" | "baseline"
  agent: string | null
  name: string // the agent's short name, "you" (outside any agent) or "·" (pre-existing)
  concurrent: boolean
  session: string | null
  turn: number | null // the turn the person asked in, even for a sub-agent's line
  turn_id: string | null
  at: number | null
}

export type BlameLine = { n: number; text: string | null; a: number | null; rewrites: number }

export type Blame = {
  path: string
  on_disk: boolean
  warnings: string[]
  authors: Author[]
  lines: BlameLine[]
  totals: Record<string, number>
}

export type Change = {
  path: string
  rel: string
  kind: "add" | "update" | "delete" | "move"
  observed?: boolean
  lines: string[]
  marked: number | null
  cut_before: number
  cut_after: number
}

export type Step = {
  id: string
  at: number | null
  ended_at: number | null
  label: string
  title: string
  status: string | null
  summary: string | null
  origin: boolean
  exit_code?: number | null
  output?: string
  output_cut?: number
  changes?: Change[]
}

export type Story = {
  path: string
  line: number
  text: string | null
  author?: Author
} & (
  | { kind: "none" }
  | { kind: "baseline" }
  | { kind: "outside"; from: number; to: number; changes: Change[] }
  | { kind: "unsynced"; tool: string | null; command: string | null; changes: Change[] }
  | {
      kind: "agent"
      cwd: string | null
      agent_version: string | null
      prompt: string | null
      handoffs: { thread: string; prompt: string | null }[]
      observed: boolean
      hidden_before: number
      steps: Step[]
      history: { author: Author | null; text: string }[]
    }
)

export type FileEntry = { path: string; root: string; changes: number; agents: string[]; last_changed: number | null }

export type SessionSummary = {
  id: string
  agent: string
  agent_version: string | null
  cwd: string | null
  started_at: number | null
  ended_at: number | null
  outcome: string | null
  turns: number
  first_input: string | null
}

export type LinkedChange = { path: string; rel: string; kind: string; line: number | null }
export type SessionStep = Omit<Step, "changes"> & { changes?: LinkedChange[] }

export type Turn = {
  id: string
  seq: number
  status: string
  thread: string
  started_at: number | null
  ended_at: number | null
  input: string | null
  delegated_from: string | null
  steps: SessionStep[]
}

export type Session = Omit<SessionSummary, "turns" | "first_input"> & { turns: Turn[] }

export type Status = {
  syncing: boolean
  syncs: number
  ingested: number
  ingested_at: number | null
  failed: string[]
  db: string
  version: number
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

const cache = new Map<string, Promise<unknown>>()

function load<T>(url: string, version: number): Promise<T> {
  const key = `${version}|${url}`
  let hit = cache.get(key)
  if (!hit) {
    hit = fetch(url).then(async (response) => {
      const body = await response.json().catch(() => ({}))
      if (!response.ok) throw new ApiError(body.error ?? `${response.status} ${response.statusText}`, response.status)
      return body
    })
    hit.catch(() => cache.delete(key))
    cache.set(key, hit)
    if (cache.size > 200) cache.delete(cache.keys().next().value!)
  }
  return hit as Promise<T>
}

/** Prefetch, e.g. a line's story on hover, so the click feels instant. */
export function prefetch(url: string) {
  load(url, status?.version ?? 0).catch(() => {})
}

export type Loaded<T> = { data: T | undefined; error: ApiError | undefined; loading: boolean; stale: boolean }

/** Data for `url`, refetched whenever the database changes. While a new url loads, the last
 * data stays (`stale`), so views never flash empty. */
export function useApi<T>(url: string | null): Loaded<T> {
  const version = useStatus()?.version ?? 0
  const [state, setState] = useState<{ url: string | null; data?: T; error?: ApiError; loading: boolean }>({
    url: null,
    loading: !!url,
  })
  useEffect(() => {
    if (!url) return
    let live = true
    setState((s) => ({ ...s, loading: true }))
    load<T>(url, version).then(
      (data) => live && setState({ url, data, loading: false }),
      (error) => live && setState({ url, error, loading: false }),
    )
    return () => {
      live = false
    }
  }, [url, version])
  const fresh = state.url === url
  return { data: state.data, error: fresh ? state.error : undefined, loading: state.loading || !fresh, stale: !fresh }
}

// -- live status: polled, so the page follows syncs and hooks as they write ---------------------

let status: Status | undefined
const watchers = new Set<() => void>()

async function poll() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" })
    if (response.ok) {
      const next: Status = await response.json()
      if (JSON.stringify(next) !== JSON.stringify(status)) {
        status = next
        watchers.forEach((w) => w())
      }
    }
  } catch {
    // the server stopped; keep showing what we have
  }
  setTimeout(poll, status?.syncing ? 500 : document.hidden ? 10_000 : 2_000)
}

if (typeof window !== "undefined") poll()

export function useStatus(): Status | undefined {
  return useSyncExternalStore(
    (w) => {
      watchers.add(w)
      return () => watchers.delete(w)
    },
    () => status,
  )
}

export const api = {
  files: "/api/files",
  sessions: "/api/sessions",
  blame: (path: string) => `/api/blame?path=${encodeURIComponent(path)}`,
  story: (path: string, line: number) => `/api/story?path=${encodeURIComponent(path)}&line=${line}`,
  session: (id: string) => `/api/sessions/${encodeURIComponent(id)}`,
}
