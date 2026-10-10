import { useMemo } from "react"
import { api, useApi, type Session, type SessionStep, type Turn } from "../api"
import { AgentChip, Pill, short, statusTone } from "../components/Chip"
import { Icon } from "../components/Icon"
import { Prose, shorten } from "../components/Timeline"
import { nameColor } from "../lib/colors"
import { href } from "../lib/router"
import { clock, duration, exact } from "../lib/time"
import { Empty } from "./FileView"

/** One session as `ah log` tells it: turns as cards, sub-agent turns nested under the turn that
 * delegated them, and every edit a link to the file at the line it wrote. */
export function SessionView({ id }: { id: string }) {
  const { data, error } = useApi<Session>(api.session(id))
  const nested = useMemo(() => {
    const children = new Map<string, Turn[]>()
    const top: Turn[] = []
    for (const turn of data?.turns ?? []) {
      if (turn.delegated_from) children.set(turn.delegated_from, [...(children.get(turn.delegated_from) ?? []), turn])
      else top.push(turn)
    }
    return { top, children }
  }, [data])

  if (error) return <Empty title="No such session">{error.message}</Empty>
  if (!data) return <div className="mx-auto max-w-3xl p-8"><div className="skeleton h-64 w-full rounded-xl" /></div>
  const name = data.agent.split("-")[0]
  const color = nameColor(name)
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-5 py-8 md:px-8">
        <header className="animate-rise mb-6">
          <div className="flex flex-wrap items-center gap-2">
            <AgentChip name={name} size="md" title={data.agent} />
            {data.agent_version && <span className="text-[12px] text-ink-3">{data.agent_version}</span>}
            {data.outcome && <Pill tone={statusTone(data.outcome)}>{data.outcome}</Pill>}
          </div>
          <h1 className="mt-2 font-mono text-[13px] break-all text-ink-2">{data.id}</h1>
          <div className="mt-1 flex flex-wrap gap-x-3 text-[12px] text-ink-3">
            <span className="font-mono">{data.cwd}</span>
            <span title={exact(data.started_at)}>{exact(data.started_at)}</span>
            {duration(data.started_at, data.ended_at) && <span>{duration(data.started_at, data.ended_at)}</span>}
            <span>{nested.top.length} {nested.top.length === 1 ? "turn" : "turns"}</span>
          </div>
        </header>
        <div className="space-y-4">
          {nested.top.map((turn, i) => (
            <div key={turn.id} className="animate-rise [animation-fill-mode:backwards]" style={{ animationDelay: `${i * 50}ms` }}>
              <TurnCard turn={turn} color={color} cwd={data.cwd} />
              {(nested.children.get(turn.id) ?? []).map((sub) => (
                <div key={sub.id} className="relative mt-2 ml-6 border-l-2 pl-4" style={{ borderColor: `color-mix(in oklab, ${color} 45%, transparent)` }}>
                  <TurnCard turn={sub} color={color} cwd={data.cwd} sub />
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function TurnCard({ turn, color, cwd, sub = false }: { turn: Turn; color: string; cwd: string | null; sub?: boolean }) {
  const steps = turn.steps.filter((s) => s.label !== "user")
  return (
    <article className="overflow-hidden rounded-xl border border-edge bg-panel">
      <div className="flex items-center gap-2 border-b border-edge bg-sunken/50 px-3.5 py-2">
        {sub ? (
          <span className="flex items-center gap-1.5 text-[12px] font-semibold">
            <span style={{ color }}><Icon name="subagent" size={13} /></span> sub-agent <span className="font-mono font-normal text-ink-2">{short(turn.thread)}</span>
          </span>
        ) : (
          <span className="text-[12px] font-semibold">Turn {turn.seq}</span>
        )}
        <Pill tone={statusTone(turn.status)}>{turn.status}</Pill>
        <span className="ml-auto font-mono text-[11px] text-ink-3" title={exact(turn.started_at)}>
          {clock(turn.started_at)}
          {duration(turn.started_at, turn.ended_at) && ` · ${duration(turn.started_at, turn.ended_at)}`}
        </span>
      </div>
      {turn.input && (
        <div className="border-b border-edge px-3.5 py-2.5">
          <div className="mb-0.5 text-[10.5px] font-semibold tracking-[0.08em] text-ink-3 uppercase">{sub ? "Asked to" : "You asked"}</div>
          <Prose text={turn.input.replace(/^[▎\s]+/, "")} clamp />
        </div>
      )}
      <ol className="px-2 py-1.5">
        {steps.map((step) => <StepRow key={step.id} step={step} cwd={cwd} />)}
        {steps.length === 0 && <li className="px-2 py-1.5 text-[12px] text-ink-3">No steps recorded.</li>}
      </ol>
    </article>
  )
}

function StepRow({ step, cwd }: { step: SessionStep; cwd: string | null }) {
  const failed = step.status === "failed" || step.status === "declined"
  const mono = step.label === "command" || step.label === "test"
  return (
    <li className="flex items-start gap-2.5 rounded-lg px-2 py-1 hover:bg-sunken/60">
      <span className="w-14 shrink-0 pt-px font-mono text-[10.5px] text-ink-3 tabular-nums">{clock(step.at)}</span>
      <span className={`pt-0.5 ${failed ? "text-bad" : "text-ink-3"}`}>
        <Icon name={step.label} size={13} />
      </span>
      <span className="min-w-0 flex-1">
        {step.label === "edit" && step.changes?.length ? (
          <span className="flex flex-wrap gap-1.5">
            {step.changes.map((c) => (
              <a
                key={c.path}
                href={href({ name: "file", path: c.path, line: c.line })}
                className="inline-flex items-center gap-1 rounded-md bg-sunken px-1.5 py-px font-mono text-[11.5px] text-ink hover:bg-edge"
              >
                <span className={c.kind === "add" ? "text-add" : c.kind === "delete" ? "text-del" : "text-ink-3"}>
                  {c.kind === "add" ? "+" : c.kind === "delete" ? "−" : "~"}
                </span>
                {c.rel}
                {c.line && <span className="text-ink-3">:{c.line}</span>}
              </a>
            ))}
            {step.status && step.status !== "completed" && <Pill tone={statusTone(step.status)}>{step.status}</Pill>}
          </span>
        ) : (
          <span className={`block truncate ${mono ? "font-mono text-[11.5px]" : "text-[12.5px]"} ${step.label === "answer" ? "font-medium" : ""}`}>
            {(mono ? shorten(step.title, cwd) : step.title).split("\n")[0]}
          </span>
        )}
        {step.summary && step.label !== "edit" && (
          <span className="mt-0.5 block">
            <Pill tone={step.label === "test" ? statusTone(step.status) : "plain"}>{step.summary}</Pill>
          </span>
        )}
      </span>
    </li>
  )
}
