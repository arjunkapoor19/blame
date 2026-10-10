import { useMemo } from "react"
import { api, useApi, type FileEntry, type SessionSummary } from "../api"
import { AgentChip, Dot, Kbd, Pill, statusTone } from "../components/Chip"
import { Icon } from "../components/Icon"
import { href } from "../lib/router"
import { ago, exact } from "../lib/time"

/** Where to start: the files agents touched, by workspace, and the latest sessions. */
export function Home({ onSearch }: { onSearch: () => void }) {
  const files = useApi<FileEntry[]>(api.files)
  const sessions = useApi<SessionSummary[]>(api.sessions)
  const workspaces = useMemo(() => {
    const groups = new Map<string, FileEntry[]>()
    for (const file of files.data ?? []) groups.set(file.root, [...(groups.get(file.root) ?? []), file])
    return [...groups.entries()]
  }, [files.data])
  const agents = useMemo(() => {
    const count = new Map<string, number>()
    for (const s of sessions.data ?? []) count.set(s.agent.split("-")[0], (count.get(s.agent.split("-")[0]) ?? 0) + 1)
    return [...count.entries()].sort((a, b) => b[1] - a[1])
  }, [sessions.data])

  const empty = files.data && sessions.data && !files.data.length && !sessions.data.length
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-5 py-8 md:px-8">
        <section className="animate-rise mb-8">
          <h1 className="text-[22px] font-semibold tracking-tight">Every line an agent wrote, and why.</h1>
          <p className="mt-1 max-w-xl text-[13px] text-ink-2">
            Pick a file to see who wrote each line. Click any line for the story behind it: what you asked, what the agent
            did, and what the tests said.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={onSearch}
              className="flex h-9 items-center gap-2 rounded-lg bg-ink px-3.5 text-[12.5px] font-medium text-canvas shadow-sm transition-transform active:scale-[0.98]"
            >
              <Icon name="search" size={14} /> Go to a file <span className="ml-1 flex gap-0.5 opacity-70"><Kbd>⌘</Kbd><Kbd>K</Kbd></span>
            </button>
            {agents.map(([name, n]) => (
              <span key={name} className="flex items-center gap-1.5 rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-[12px]">
                <Dot name={name} />
                <span className="font-medium">{name}</span>
                <span className="text-ink-3 tabular-nums">{n} sessions</span>
              </span>
            ))}
          </div>
        </section>

        {empty ? (
          <div className="rounded-2xl border border-dashed border-edge-strong p-10 text-center">
            <h2 className="text-[15px] font-semibold">Nothing recorded yet</h2>
            <p className="mt-1 text-[12.5px] text-ink-2">
              Run <code className="rounded bg-sunken px-1 font-mono">ah setup</code>, then use Claude Code, Codex or opencode as
              usual. Their work shows up here as it happens.
            </p>
          </div>
        ) : (
          <div className="grid gap-8 lg:grid-cols-[1.4fr_1fr]">
            <section>
              <Heading icon="folder">Files</Heading>
              {!files.data && <ListSkeleton />}
              <div className="space-y-5">
                {workspaces.map(([root, list], i) => (
                  <div key={root} className="animate-rise [animation-fill-mode:backwards]" style={{ animationDelay: `${i * 40}ms` }}>
                    <div className="mb-1.5 flex items-baseline gap-2 px-1">
                      <span className="font-mono text-[12.5px] font-semibold">{root.split("/").pop()}</span>
                      <span className="truncate font-mono text-[11px] text-ink-3">{root}</span>
                    </div>
                    <div className="overflow-hidden rounded-xl border border-edge bg-panel">
                      {list.slice(0, 8).map((file) => (
                        <a
                          key={file.path}
                          href={href({ name: "file", path: file.path, line: null })}
                          className="group flex items-center gap-3 border-b border-edge px-3 py-2 last:border-b-0 hover:bg-sunken/70"
                        >
                          <Icon name="file" size={14} className="text-ink-3" />
                          <span className="min-w-0 flex-1 truncate font-mono text-[12.5px]">{file.path.slice(root.length + 1) || file.path}</span>
                          <span className="flex gap-1">{file.agents.map((a) => <Dot key={a} name={a.split("-")[0]} />)}</span>
                          <span className="w-20 text-right text-[11px] text-ink-3" title={exact(file.last_changed)}>{ago(file.last_changed)}</span>
                          <Icon name="chevron" size={13} className="text-ink-3 opacity-0 transition-opacity group-hover:opacity-100" />
                        </a>
                      ))}
                      {list.length > 8 && (
                        <button type="button" onClick={onSearch} className="w-full px-3 py-1.5 text-left text-[11.5px] text-ink-3 hover:bg-sunken/70 hover:text-ink">
                          {list.length - 8} more files…
                        </button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </section>
            <section>
              <Heading icon="sessions" more={{ to: href({ name: "sessions" }), label: "All sessions" }}>
                Recent sessions
              </Heading>
              {!sessions.data && <ListSkeleton />}
              <div className="overflow-hidden rounded-xl border border-edge bg-panel">
                {(sessions.data ?? []).slice(0, 10).map((s) => <SessionRow key={s.id} session={s} />)}
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  )
}

export function SessionRow({ session }: { session: SessionSummary }) {
  return (
    <a
      href={href({ name: "session", id: session.id })}
      className={`flex items-start gap-3 border-b border-edge px-3 py-2.5 last:border-b-0 hover:bg-sunken/70 ${session.turns ? "" : "opacity-55"}`}
    >
      <span className="w-[76px] shrink-0 pt-px">
        <AgentChip name={session.agent.split("-")[0]} size="xs" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="line-clamp-2 text-[12.5px] leading-[18px] text-ink">
          {(session.first_input ?? "(no prompt)").replace(/^▎\s*/, "")}
        </span>
        <span className="mt-0.5 flex items-center gap-2 text-[11px] whitespace-nowrap text-ink-3">
          <span className="min-w-0 truncate font-mono">{session.cwd?.split("/").pop()}</span>
          <span>·</span>
          <span className="shrink-0">{session.turns} {session.turns === 1 ? "turn" : "turns"}</span>
          {session.outcome && session.outcome !== "completed" && <Pill tone={statusTone(session.outcome)}>{session.outcome}</Pill>}
        </span>
      </span>
      <span className="shrink-0 text-[11px] text-ink-3" title={exact(session.started_at)}>{ago(session.started_at)}</span>
    </a>
  )
}

function Heading({ icon, children, more }: { icon: string; children: React.ReactNode; more?: { to: string; label: string } }) {
  return (
    <div className="mb-3 flex items-center gap-2 px-1">
      <Icon name={icon} size={14} className="text-ink-3" />
      <h2 className="text-[13px] font-semibold">{children}</h2>
      {more && (
        <a href={more.to} className="ml-auto flex items-center gap-1 text-[11.5px] text-ink-3 hover:text-ink">
          {more.label} <Icon name="arrow" size={11} />
        </a>
      )}
    </div>
  )
}

function ListSkeleton() {
  return (
    <div className="space-y-2">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="skeleton h-9 w-full rounded-lg" />
      ))}
    </div>
  )
}
