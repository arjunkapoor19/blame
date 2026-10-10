import { useEffect, useRef } from "react"
import type { Author } from "../api"
import type { Block } from "../lib/blocks"
import { authorColor } from "../lib/colors"

/** The whole file at a glance, down the right edge: who wrote where, and where you are. */
export function Minimap({
  blocks,
  authors,
  total,
  scroller,
  selected,
}: {
  blocks: Block[]
  authors: Author[]
  total: number
  scroller: React.RefObject<HTMLDivElement | null>
  selected: number | null
}) {
  const view = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = scroller.current
    if (!el) return
    let frame = 0
    const update = () => {
      frame = 0
      const box = view.current
      if (!box) return
      const visible = el.clientHeight / Math.max(el.scrollHeight, 1)
      box.style.top = `${(el.scrollTop / Math.max(el.scrollHeight, 1)) * 100}%`
      box.style.height = `${Math.min(visible, 1) * 100}%`
      box.style.opacity = visible >= 1 ? "0" : "1"
    }
    const onScroll = () => (frame ||= requestAnimationFrame(update))
    update()
    el.addEventListener("scroll", onScroll, { passive: true })
    const resize = new ResizeObserver(onScroll)
    resize.observe(el)
    return () => {
      el.removeEventListener("scroll", onScroll)
      resize.disconnect()
      cancelAnimationFrame(frame)
    }
  }, [scroller, total])

  const jump = (event: React.MouseEvent<HTMLDivElement>) => {
    const el = scroller.current
    if (!el) return
    const rect = event.currentTarget.getBoundingClientRect()
    const at = (event.clientY - rect.top) / rect.height
    el.scrollTo({ top: at * el.scrollHeight - el.clientHeight / 2, behavior: "smooth" })
  }

  return (
    <div
      className="absolute top-0 right-0 bottom-0 w-3 cursor-pointer border-l border-edge/60 bg-canvas/80 backdrop-blur max-md:hidden"
      onClick={jump}
      aria-hidden="true"
    >
      {blocks.map((b) => {
        const author = b.a == null ? null : authors[b.a]
        if (!author || author.kind === "baseline") return null
        return (
          <span
            key={b.start}
            className="absolute inset-x-[3px] rounded-full"
            style={{
              top: `${(b.start / total) * 100}%`,
              height: `max(${((b.end - b.start) / total) * 100}%, 2px)`,
              background: authorColor(author),
            }}
          />
        )
      })}
      {selected != null && (
        <span className="absolute inset-x-0 h-[2px] bg-ink" style={{ top: `${((selected - 0.5) / total) * 100}%` }} />
      )}
      <div ref={view} className="pointer-events-none absolute inset-x-0 rounded-sm bg-ink/8 ring-1 ring-ink/15" />
    </div>
  )
}
