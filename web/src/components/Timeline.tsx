import { useState } from "react"
import type { Step } from "../api"
import { clock, duration, exact } from "../lib/time"
import { Pill, statusTone } from "./Chip"
import { Diff } from "./Diff"
import { Icon } from "./Icon"

const LABELS: Record<string, string> = {
  read: "Read",
  edit: "Edit",
  command: "Ran",
  test: "Test",
  agent: "Said",
  answer: "Answer",
  subagent: "Sub-agent",
  error: "Error",
  thinking: "Thought",
  user: "You",
  tool: "Tool",
  approval: "Approval",
}

/** A turn's steps on a vertical rail. The step that wrote the line stands out and opens its diff. */
export function Timeline({ steps, color, cwd }: { steps: Step[]; color: string; cwd?: string | null }) {
  return (
    <ol className="relative">
      <span className="absolute top-3 bottom-3 left-[13px] w-px bg-edge" aria-hidden="true" />
      {steps.map((step, i) => (
        <StepItem key={step.id} step={step} color={color} cwd={cwd} delay={i} />
      ))}
    </ol>
  )
}

function StepItem({ step, color, cwd, delay }: { step: Step; color: string; cwd?: string | null; delay: number }) {
  const [open, setOpen] = useState(false)
  const failed = step.status === "failed" || step.status === "declined"
  const isMessage = step.label === "agent" || step.label === "answer" || step.label === "thinking"
  const hasMore = !!(step.output || (step.changes?.length && !step.origin) || (isMessage && step.title.length > 220))
  const title = step.label === "command" || step.label === "test" ? shorten(step.title, cwd) : step.title
  const took = duration(step.at, step.ended_at)

  return (
    <li
      className="animate-rise relative flex gap-3 pb-3 [animation-fill-mode:backwards]"
      style={{ animationDelay: `${Math.min(delay, 12) * 22}ms` }}
    >
      <span
        className={`relative z-10 mt-0.5 flex size-[27px] shrink-0 items-center justify-center rounded-full border ${
          step.origin ? "border-transparent text-white shadow-[0_0_0_4px_var(--panel)]" : "border-edge bg-panel text-ink-2"
        }`}
        style={step.origin ? { background: color } : failed ? { color: "var(--bad)" } : undefined}
      >
        <Icon name={step.label} size={13} />
      </span>
      <div
        className={`min-w-0 flex-1 ${step.origin ? "tint -mx-1 rounded-xl px-3 py-2.5 ring-1" : "pt-1"}`}
        style={step.origin ? ({ "--c": color, "--tw-ring-color": `color-mix(in oklab, ${color} 35%, transparent)` } as React.CSSProperties) : undefined}
      >
        <div className="flex items-baseline gap-2">
          <span className="shrink-0 text-[11px] font-semibold tracking-wide text-ink-3 uppercase">{LABELS[step.label] ?? step.label}</span>
          {step.origin && (
            <span className="shrink-0 rounded-full px-1.5 py-px text-[10.5px] font-semibold text-white" style={{ background: color }}>
              wrote this line
            </span>
          )}
          <time className="ml-auto shrink-0 font-mono text-[10.5px] text-ink-3 tabular-nums" title={exact(step.at)}>
            {clock(step.at)}
            {took && took !== "0.0s" ? <span className="text-ink-3/70"> · {took}</span> : null}
          </time>
        </div>
        {isMessage ? (
          <Prose text={step.title} clamp={!open} />
        ) : (
          <div className={`mt-0.5 break-words ${step.label === "command" || step.label === "test" ? "font-mono text-[12px]" : "text-[12.5px]"} text-ink`}>
            {title}
          </div>
        )}
        {(step.summary || (step.status && step.status !== "completed")) && (
          <div className="mt-1 flex flex-wrap gap-1.5">
            {step.status && step.status !== "completed" && <Pill tone={statusTone(step.status)}>{step.status}</Pill>}
            {step.summary && (
              <Pill tone={step.label === "test" ? statusTone(step.status) : "plain"} title={step.summary}>
                {step.label === "test" && <Icon name={step.status === "completed" ? "check" : "x"} size={11} />}
                {step.summary}
              </Pill>
            )}
          </div>
        )}
        {step.origin &&
          step.changes?.map((change) => (
            <div key={change.path} className="mt-2">
              <Diff change={change} color={color} />
            </div>
          ))}
        {open && !step.origin && (
          <div className="animate-fade-in mt-2 space-y-2">
            {step.changes?.map((change) => <Diff key={change.path} change={change} />)}
            {step.output && <Output text={step.output} cut={step.output_cut ?? 0} />}
          </div>
        )}
        {hasMore && !step.origin && (
          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium text-ink-3 transition-colors hover:text-ink"
          >
            <Icon name={open ? "down" : "chevron"} size={11} />
            {open ? "Less" : step.output ? "Output" : step.changes?.length ? `Diff${step.changes.length > 1 ? "s" : ""}` : "More"}
          </button>
        )}
        {step.origin && step.output && (
          <details className="mt-2">
            <summary className="cursor-pointer text-[11px] font-medium text-ink-3 hover:text-ink">Output</summary>
            <Output text={step.output} cut={step.output_cut ?? 0} />
          </details>
        )}
      </div>
    </li>
  )
}

function Output({ text, cut }: { text: string; cut: number }) {
  return (
    <pre className="mt-1 max-h-72 overflow-auto rounded-lg border border-edge bg-sunken px-3 py-2 font-mono text-[11px] leading-[17px] whitespace-pre-wrap text-ink-2">
      {cut > 0 && <span className="text-ink-3">… {cut.toLocaleString()} characters earlier{"\n"}</span>}
      {text}
    </pre>
  )
}

/** An agent's message: paragraphs kept, `code` set in mono, long ones clamped until opened. */
export function Prose({ text, clamp }: { text: string; clamp: boolean }) {
  const shown = clamp && text.length > 220 ? text.slice(0, 220).trimEnd() + "…" : text
  return (
    <div className="mt-0.5 text-[12.5px] leading-[19px] whitespace-pre-wrap text-ink">
      {shown.split(/(`[^`\n]+`)/g).map((part, i) =>
        part.startsWith("`") && part.endsWith("`") && part.length > 2 ? (
          <code key={i} className="rounded bg-sunken px-1 py-px font-mono text-[11.5px]">
            {part.slice(1, -1)}
          </code>
        ) : (
          part
        ),
      )}
    </div>
  )
}

/** A command with the session's folder written as `.`, so what it did stays readable. */
export function shorten(command: string, cwd?: string | null): string {
  const flat = command.replace(/\s+/g, " ").trim()
  if (!cwd) return flat
  return flat.split(cwd + "/").join("").split(cwd).join(".")
}
