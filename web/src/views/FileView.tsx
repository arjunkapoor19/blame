import { useVirtualizer } from "@tanstack/react-virtual"
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { api, prefetch, useApi, type Author, type Blame, type BlameLine } from "../api"
import { AuthorBar } from "../components/AuthorBar"
import { AgentChip, short } from "../components/Chip"
import { Icon } from "../components/Icon"
import { Minimap } from "../components/Minimap"
import { StoryDrawer } from "../components/StoryDrawer"
import { blocks as toBlocks, nextBlock } from "../lib/blocks"
import { authorColor } from "../lib/colors"
import { tokenize, type Token } from "../lib/highlight"
import { useKeys } from "../lib/keys"
import { go } from "../lib/router"
import { ago, exact } from "../lib/time"

const VIRTUAL_FROM = 1500 // lines; below this every row is in the page, so ⌘F finds anything
const ROW = 22

export function FileView({ path, line }: { path: string; line: number | null }) {
  const { data, error } = useApi<Blame>(api.blame(path))
  const select = useCallback((n: number | null) => go({ name: "file", path, line: n }, true), [path])
  const [focus, setFocus] = useState<string | null>(null)
  const [pinned, setPinned] = useState<string | null>(null)

  if (error)
    return (
      <Empty title={error.status === 404 ? "No history for this file" : "Couldn't load this file"}>
        {error.message}
      </Empty>
    )
  if (!data) return <Loading path={path} />
  const current = line && line <= data.lines.length ? line : null
  return (
    <div className="flex h-full min-h-0">
      <section className="flex min-w-0 flex-1 flex-col">
        <FileHeader blame={data}>
          <AuthorBar totals={data.totals} focus={focus} pinned={pinned} onFocus={setFocus} onPin={setPinned} />
        </FileHeader>
        <Code key={data.path} blame={data} selected={current} onSelect={select} dim={focus ?? pinned} />
      </section>
      {current && (
        <>
          <div className="fixed inset-0 z-30 bg-black/30 backdrop-blur-[1px] md:hidden" onClick={() => select(null)} />
          <div className="animate-drawer max-md:fixed max-md:inset-x-0 max-md:bottom-0 max-md:z-40 max-md:h-[80vh] md:w-[min(540px,44vw)] md:shrink-0">
            <StoryDrawer path={data.path} line={current} onClose={() => select(null)} />
          </div>
        </>
      )}
    </div>
  )
}

function FileHeader({ blame, children }: { blame: Blame; children: React.ReactNode }) {
  const parts = blame.path.split("/").filter(Boolean)
  const name = parts.pop()
  const dirs = parts.slice(-3)
  const agents = Object.keys(blame.totals).filter((n) => !["·", "you", "untracked"].includes(n))
  return (
    <header className="border-b border-edge bg-panel/60 px-5 pt-4 pb-3 backdrop-blur">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="flex min-w-0 items-baseline gap-1 font-mono text-[13px]">
          <span className="truncate text-ink-3">
            {parts.length > 3 ? "…/" : "/"}
            {dirs.join("/")}/
          </span>
          <span className="text-[15px] font-semibold tracking-tight text-ink">{name}</span>
        </h1>
        <span className="text-[12px] text-ink-3">
          {blame.lines.length} lines · {agents.length} {agents.length === 1 ? "agent" : "agents"}
        </span>
        {!blame.on_disk && (
          <span className="rounded-md bg-[color-mix(in_oklab,var(--warn)_14%,transparent)] px-1.5 py-px text-[11px] text-warn">
            not on disk: rebuilt from recorded changes
          </span>
        )}
      </div>
      {blame.warnings.length > 0 && (
        <details className="mt-1 text-[11.5px] text-warn">
          <summary className="cursor-pointer">{blame.warnings.length} warnings</summary>
          <ul className="mt-1 list-disc pl-5">{blame.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
        </details>
      )}
      <div className="mt-3">{children}</div>
    </header>
  )
}

type CodeProps = { blame: Blame; selected: number | null; onSelect: (n: number | null) => void; dim: string | null }

/** The file, line by line, with who wrote each block in the gutter. `dim` fades every line not by that author. */
function Code({ blame, selected, onSelect, dim }: CodeProps) {
  const scroller = useRef<HTMLDivElement>(null)
  const glow = useRef<HTMLStyleElement>(null)
  const scrollTo = useRef<(index: number, center: boolean) => void>(() => {})
  const [tokens, setTokens] = useState<Token[][] | null>(null)
  const blocks = useMemo(() => toBlocks(blame.lines), [blame.lines])
  const starts = useMemo(() => new Set(blocks.map((b) => b.start)), [blocks])
  const width = String(blame.lines.length).length

  useEffect(() => {
    let live = true
    const code = blame.lines.map((l) => l.text ?? "").join("\n")
    const timer = setTimeout(() => {
      tokenize(code, blame.path, blame.lines.length).then((t) => live && setTokens(t), () => {})
    }, 0)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [blame])

  // hovering a line lights up everything its turn wrote; focusing an author dims the rest
  const hovered = useRef<string | null>(null)
  const paint = useCallback(() => {
    const rules: string[] = []
    const turn = hovered.current
    if (turn) {
      rules.push(`.rows [data-t="${CSS.escape(turn)}"]{background:color-mix(in oklab,var(--c) 11%,transparent)}`)
      rules.push(`.rows [data-t="${CSS.escape(turn)}"] .ribbon{width:5px}`)
    }
    if (dim) rules.push(`.rows .row:not([data-name="${CSS.escape(dim)}"]) .code{opacity:.28}`)
    if (glow.current) glow.current.textContent = rules.join("\n")
  }, [dim])
  useEffect(paint, [paint])

  const hoverTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const onMove = (event: React.MouseEvent) => {
    const row = (event.target as HTMLElement).closest<HTMLElement>("[data-n]")
    const turn = row?.dataset.t ?? null
    if (turn !== hovered.current) {
      hovered.current = turn
      paint()
    }
    clearTimeout(hoverTimer.current)
    if (row) {
      const n = Number(row.dataset.n)
      hoverTimer.current = setTimeout(() => prefetch(api.story(blame.path, n)), 140)
    }
  }
  const onLeave = () => {
    hovered.current = null
    clearTimeout(hoverTimer.current)
    paint()
  }

  const first = useRef(true)
  useLayoutEffect(() => {
    if (selected == null) return
    scrollTo.current(selected - 1, first.current)
    first.current = false
  }, [selected])

  const total = blame.lines.length
  const firstAttributed = () => blame.lines.find((l) => l.a != null && blame.authors[l.a].kind !== "baseline")?.n ?? 1
  useKeys({
    j: () => onSelect(selected ? Math.min(selected + 1, total) : firstAttributed()),
    ArrowDown: () => onSelect(selected ? Math.min(selected + 1, total) : firstAttributed()),
    k: () => onSelect(selected ? Math.max(selected - 1, 1) : firstAttributed()),
    ArrowUp: () => onSelect(selected ? Math.max(selected - 1, 1) : firstAttributed()),
    "]": () => {
      const next = nextBlock(blocks, (selected ?? 0) - 1, 1)
      if (next != null) onSelect(next + 1)
    },
    "[": () => {
      const prev = nextBlock(blocks, (selected ?? 1) - 1, -1)
      if (prev != null) onSelect(prev + 1)
    },
    Enter: () => !selected && onSelect(firstAttributed()),
    Escape: () => onSelect(null),
  })

  const rowProps = { blame, starts, selected, width, tokens, onSelect }
  return (
    <>
      <style ref={glow} />
      <div className="relative min-h-0 flex-1">
        <div ref={scroller} className="rows absolute inset-0 overflow-auto pr-3 max-md:pr-0" onMouseMove={onMove} onMouseLeave={onLeave}>
          {total >= VIRTUAL_FROM ? (
            <VirtualRows {...rowProps} scroller={scroller} scrollTo={scrollTo} />
          ) : (
            <PlainRows {...rowProps} scroller={scroller} scrollTo={scrollTo} />
          )}
        </div>
        <Minimap blocks={blocks} authors={blame.authors} total={total} scroller={scroller} selected={selected} />
      </div>
    </>
  )
}

type RowsProps = {
  blame: Blame
  starts: Set<number>
  selected: number | null
  width: number
  tokens: Token[][] | null
  onSelect: (n: number | null) => void
  scroller: React.RefObject<HTMLDivElement | null>
  scrollTo: React.RefObject<(index: number, center: boolean) => void>
}

function PlainRows({ blame, starts, selected, width, tokens, onSelect, scroller, scrollTo }: RowsProps) {
  scrollTo.current = (index, center) => {
    const el = scroller.current?.querySelector<HTMLElement>(`[data-n="${index + 1}"]`)
    el?.scrollIntoView({ block: center ? "center" : "nearest" })
  }
  return (
    <div className="min-w-max py-2">
      {blame.lines.map((line, i) => (
        <Row
          key={line.n}
          line={line}
          author={line.a == null ? null : blame.authors[line.a]}
          start={starts.has(i)}
          selected={selected === line.n}
          width={width}
          tokens={tokens?.[i]}
          onSelect={onSelect}
        />
      ))}
    </div>
  )
}

function VirtualRows({ blame, starts, selected, width, tokens, onSelect, scroller, scrollTo }: RowsProps) {
  const virtual = useVirtualizer({
    count: blame.lines.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW,
    overscan: 40,
    paddingStart: 8,
    paddingEnd: 8,
  })
  scrollTo.current = (index, center) => virtual.scrollToIndex(index, { align: center ? "center" : "auto" })
  return (
    <div className="relative min-w-max" style={{ height: virtual.getTotalSize() }}>
      {virtual.getVirtualItems().map((item) => {
        const line = blame.lines[item.index]
        return (
          <div key={item.key} className="absolute inset-x-0" style={{ transform: `translateY(${item.start}px)` }}>
            <Row
              line={line}
              author={line.a == null ? null : blame.authors[line.a]}
              start={starts.has(item.index) || item.index === virtual.range?.startIndex}
              selected={selected === line.n}
              width={width}
              tokens={tokens?.[item.index]}
              onSelect={onSelect}
            />
          </div>
        )
      })}
    </div>
  )
}

type RowProps = {
  line: BlameLine
  author: Author | null
  start: boolean
  selected: boolean
  width: number
  tokens: Token[] | undefined
  onSelect: (n: number | null) => void
}

const Row = memo(function Row({ line, author, start, selected, width, tokens, onSelect }: RowProps) {
  const color = authorColor(author)
  const kind = author?.kind
  const tracked = author && kind !== "baseline"
  return (
    <div
      data-n={line.n}
      data-t={author?.turn_id ?? author?.key ?? undefined}
      data-name={author?.name ?? "untracked"}
      onClick={() => onSelect(selected ? null : line.n)}
      className={`row group relative flex h-[22px] cursor-pointer items-center transition-colors duration-75 ${
        selected ? "tint-strong" : "hover:bg-sunken/80"
      } ${start && line.n > 1 ? "shadow-[inset_0_1px_0_var(--edge)]" : ""}`}
      style={{ "--c": color } as React.CSSProperties}
    >
      <div className="gutter sticky left-0 z-10 flex h-full w-[238px] shrink-0 items-center gap-2 bg-canvas pr-2 pl-3 max-md:w-[52px] max-md:pl-2.5 [.row:hover_&]:bg-[color-mix(in_oklab,var(--sunken)_80%,var(--canvas))]">
        <span
          className="ribbon absolute inset-y-0 left-0 transition-[width] duration-150"
          style={{ background: author ? color : "transparent", width: selected ? 5 : 3, opacity: kind === "baseline" ? 0.45 : 1 }}
        />
        {start && tracked && (
          <>
            <span className="max-md:hidden">
              <AgentChip name={author.name} size="xs" title={author.agent ?? undefined} />
            </span>
            <span className="font-mono text-[10.5px] text-ink-3 max-md:hidden" title={author.session ?? undefined}>
              {short(author.session, 6)}
              {author.turn != null && <span className="text-ink-2"> t{author.turn}</span>}
            </span>
            <span className="ml-auto truncate text-[10.5px] text-ink-3 max-md:hidden" title={exact(author.at)}>
              {ago(author.at)}
            </span>
            <span className="size-2 rounded-full md:hidden" style={{ background: color }} />
          </>
        )}
        {start && kind === "baseline" && <span className="text-[10.5px] text-ink-3/80 max-md:hidden">pre-existing</span>}
        {author?.concurrent && <span className="absolute right-1 text-[11px] text-warn" title="another tool call ran at the same time">≈</span>}
      </div>
      <div
        className={`shrink-0 pr-4 text-right font-mono text-[11.5px] select-none ${selected ? "font-semibold text-ink" : "text-ink-3/80"}`}
        style={{ width: `calc(${width}ch + 28px)` }}
      >
        {line.n}
      </div>
      <div className="code pr-8 font-mono text-[12.5px] leading-[22px] whitespace-pre transition-opacity duration-200">
        {line.text == null ? (
          <span className="text-ink-3 italic">… content not recorded</span>
        ) : tokens ? (
          tokens.map((t, i) => (
            <span key={i} className="tk" style={t.style as React.CSSProperties}>
              {t.content}
            </span>
          ))
        ) : (
          <span className={kind === "baseline" || !author ? "text-ink-2" : ""}>{line.text}</span>
        )}
      </div>
    </div>
  )
})

function Loading({ path }: { path: string }) {
  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-edge px-5 pt-4 pb-3">
        <div className="font-mono text-[13px] text-ink-3">{path}</div>
        <div className="skeleton mt-3 h-2 w-full rounded-full" />
        <div className="mt-2 flex gap-2">
          <div className="skeleton h-4 w-20" />
          <div className="skeleton h-4 w-16" />
        </div>
      </header>
      <div className="space-y-2 p-4">
        {Array.from({ length: 14 }, (_, i) => (
          <div key={i} className="flex gap-4">
            <div className="skeleton h-3.5 w-48" />
            <div className="skeleton h-3.5" style={{ width: `${30 + ((i * 37) % 50)}%` }} />
          </div>
        ))}
      </div>
    </div>
  )
}

export function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="animate-rise max-w-md text-center">
        <div className="mx-auto mb-3 flex size-10 items-center justify-center rounded-full bg-sunken text-ink-2 ring-1 ring-edge">
          <Icon name="file" size={18} />
        </div>
        <h2 className="text-[15px] font-semibold">{title}</h2>
        <p className="mt-1 text-[12.5px] text-ink-2">{children}</p>
        <a href="#/" className="mt-4 inline-flex items-center gap-1 text-[12.5px] font-medium text-focus hover:underline">
          Browse files <Icon name="arrow" size={12} />
        </a>
      </div>
    </div>
  )
}
