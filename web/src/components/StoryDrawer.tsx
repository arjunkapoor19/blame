import type { ReactNode } from "react"
import { api, useApi, type Story } from "../api"
import { authorColor } from "../lib/colors"
import { href } from "../lib/router"
import { ago, exact } from "../lib/time"
import { AgentChip, Kbd, short } from "./Chip"
import { Diff } from "./Diff"
import { Icon } from "./Icon"
import { Prose, Timeline } from "./Timeline"

/** How a line came to be: who wrote it, what you asked, and every step around the change. */
export function StoryDrawer({ path, line, onClose }: { path: string; line: number; onClose: () => void }) {
  const { data, error, stale } = useApi<Story>(api.story(path, line))
  return (
    <aside
      className="flex h-full min-h-0 flex-col border-l border-edge bg-panel max-md:rounded-t-2xl max-md:border-t max-md:border-l-0 max-md:shadow-[var(--shadow-pop)]"
      aria-label={`Story of line ${line}`}
    >
      <div className="flex items-center gap-2 border-b border-edge px-4 py-2.5">
        <span className="mx-auto mb-1 h-1 w-9 rounded-full bg-edge-strong md:hidden" />
        <span className="font-mono text-[11.5px] text-ink-3 max-md:hidden">line {line}</span>
        <code className="min-w-0 flex-1 truncate font-mono text-[12px] text-ink max-md:hidden">{data?.text ?? ""}</code>
        <span className="flex items-center gap-1 text-[11px] text-ink-3 max-md:hidden">
          <Kbd>j</Kbd>
          <Kbd>k</Kbd>
        </span>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-1 text-ink-3 transition-colors hover:bg-sunken hover:text-ink"
          aria-label="Close"
          title="Close (Esc)"
        >
          <Icon name="close" size={15} />
        </button>
      </div>
      <div className={`min-h-0 flex-1 overflow-y-auto px-4 py-4 transition-opacity duration-150 ${stale ? "opacity-60" : ""}`}>
        {error ? (
          <Note icon="error" title="Couldn't load this line">{error.message}</Note>
        ) : !data ? (
          <Skeleton />
        ) : (
          <StoryBody story={data} />
        )}
      </div>
    </aside>
  )
}

function StoryBody({ story }: { story: Story }) {
  const color = authorColor(story.author)
  switch (story.kind) {
    case "none":
      return (
        <Note icon="history" title="Not attributed">
          This line predates what ah has recorded or observed, or changed while nothing was watching. Run{" "}
          <code className="rounded bg-sunken px-1 font-mono">ah setup</code> so every change from now on is seen.
        </Note>
      )
    case "baseline":
      return (
        <Note icon="history" title="Pre-existing">
          Already here when ah started observing this workspace{story.author?.at ? <> ({exact(story.author.at)})</> : null}.
          No agent has changed it since.
        </Note>
      )
    case "outside":
      return (
        <div className="space-y-3">
          <Header story={story} />
          <Note icon="user" title="Edited outside any agent">
            By you or another program, between {exact(story.from)} and {exact(story.to)}.
          </Note>
          {story.changes.map((c) => <Diff key={c.path} change={c} color={color} />)}
        </div>
      )
    case "unsynced":
      return (
        <div className="space-y-3">
          <Header story={story} />
          <Note icon="clock" title="Transcript not synced yet">
            ah saw {story.tool ?? "a tool call"} change this file; the session's transcript will appear once it syncs.
          </Note>
          {story.command && <pre className="rounded-lg bg-sunken px-3 py-2 font-mono text-[11.5px] whitespace-pre-wrap">{story.command}</pre>}
          {story.changes.map((c) => <Diff key={c.path} change={c} color={color} />)}
        </div>
      )
    case "agent":
      return (
        <div className="space-y-4">
          <Header story={story} />
          {story.prompt && (
            <div className="animate-rise">
              <Label>You asked</Label>
              <div className="rounded-xl rounded-tl-sm border border-edge bg-sunken/70 px-3 py-2">
                <Prose text={story.prompt.replace(/^[▎\s]+/, "")} clamp={false} />
              </div>
              {story.handoffs.map((h) => (
                <div key={h.thread} className="relative mt-2 ml-4 border-l-2 pl-3" style={{ borderColor: color }}>
                  <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-ink-2">
                    <Icon name="subagent" size={12} />
                    handed to sub-agent <span className="font-mono">{short(h.thread)}</span>
                  </div>
                  {h.prompt && (
                    <div className="tint rounded-lg px-3 py-1.5" style={{ "--c": color } as React.CSSProperties}>
                      <Prose text={h.prompt} clamp />
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
          <div>
            <Label>What happened</Label>
            {story.hidden_before > 0 && story.author?.session && (
              <a
                href={href({ name: "session", id: story.author.session })}
                className="mb-2 ml-9 inline-flex items-center gap-1 text-[11px] text-ink-3 hover:text-ink"
              >
                … {story.hidden_before} earlier steps in this turn <Icon name="arrow" size={11} />
              </a>
            )}
            <Timeline steps={story.steps} color={color} cwd={story.cwd} />
          </div>
          {story.history.length > 1 && (
            <div>
              <Label>Line history</Label>
              <ol className="space-y-1">
                {story.history.map((h, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <span className="w-16 shrink-0">{h.author && <AgentChip name={h.author.name} size="xs" />}</span>
                    <code className={`truncate font-mono text-[11.5px] ${i === story.history.length - 1 ? "text-ink" : "text-ink-3 line-through decoration-ink-3/40"}`}>
                      {h.text}
                    </code>
                  </li>
                ))}
              </ol>
            </div>
          )}
          <p className="border-t border-edge pt-3 text-[11px] text-ink-3">Steps are shown in order; order is not proof of cause.</p>
        </div>
      )
  }
}

function Header({ story }: { story: Story }) {
  const author = story.author
  if (!author) return null
  const agentStory = story.kind === "agent" ? story : null
  return (
    <div className="animate-rise flex flex-wrap items-center gap-x-2 gap-y-1">
      <AgentChip name={author.name} size="md" title={author.agent ?? undefined} />
      {agentStory?.agent_version && <span className="text-[11px] text-ink-3">{agentStory.agent_version}</span>}
      {author.session && (
        <a
          href={href({ name: "session", id: author.session })}
          className="font-mono text-[11.5px] text-ink-2 underline decoration-edge-strong underline-offset-2 hover:text-ink"
          title={author.session}
        >
          {short(author.session, 10)}
        </a>
      )}
      {author.turn != null && <span className="text-[11.5px] text-ink-2">turn {author.turn}</span>}
      {agentStory?.handoffs.map((h) => (
        <span key={h.thread} className="flex items-center gap-1 text-[11.5px] text-ink-2">
          <Icon name="chevron" size={11} className="text-ink-3" />
          sub-agent <span className="font-mono">{short(h.thread)}</span>
        </span>
      ))}
      <span className="ml-auto text-[11.5px] text-ink-3" title={exact(author.at)}>
        {ago(author.at)}
      </span>
      {(agentStory?.observed || author.concurrent) && (
        <div className="flex w-full gap-1.5">
          {agentStory?.observed && (
            <span className="inline-flex items-center gap-1 rounded-md bg-sunken px-1.5 py-px text-[10.5px] text-ink-2" title="ah saw the file change around this tool call">
              <Icon name="eye" size={11} /> observed by ah
            </span>
          )}
          {author.concurrent && (
            <span className="inline-flex items-center gap-1 rounded-md bg-[color-mix(in_oklab,var(--warn)_14%,transparent)] px-1.5 py-px text-[10.5px] text-warn">
              ≈ another tool call ran at the same time; the line may be from either
            </span>
          )}
        </div>
      )}
    </div>
  )
}

function Label({ children }: { children: ReactNode }) {
  return <div className="mb-1.5 text-[10.5px] font-semibold tracking-[0.08em] text-ink-3 uppercase">{children}</div>
}

function Note({ icon, title, children }: { icon: string; title: string; children: ReactNode }) {
  return (
    <div className="animate-rise flex gap-3 rounded-xl border border-edge bg-sunken/50 p-3">
      <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-panel text-ink-2 ring-1 ring-edge">
        <Icon name={icon} size={14} />
      </span>
      <div className="min-w-0 text-[12.5px] leading-[19px] text-ink-2">
        <div className="mb-0.5 font-semibold text-ink">{title}</div>
        {children}
      </div>
    </div>
  )
}

function Skeleton() {
  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <div className="skeleton h-6 w-20 rounded-full" />
        <div className="skeleton h-6 w-32" />
      </div>
      <div className="skeleton h-16 w-full rounded-xl" />
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="flex gap-3">
          <div className="skeleton size-7 rounded-full" />
          <div className="flex-1 space-y-1.5">
            <div className="skeleton h-3 w-24" />
            <div className="skeleton h-3.5 w-full" />
          </div>
        </div>
      ))}
    </div>
  )
}
