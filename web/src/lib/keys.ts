/** Keyboard shortcuts that stay out of the way while typing in a field. */
import { useEffect, useRef } from "react"

export type Keymap = Record<string, (event: KeyboardEvent) => void>

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName))
}

/** Keys are `j`, `Enter`, `mod+k` (⌘ or Ctrl), or two-key sequences like `g s`. */
export function useKeys(map: Keymap, enabled = true) {
  const latest = useRef(map)
  latest.current = map
  useEffect(() => {
    if (!enabled) return
    let pending: string | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    const onKey = (event: KeyboardEvent) => {
      const mod = event.metaKey || event.ctrlKey
      const key = (mod ? "mod+" : "") + (event.key.length === 1 ? event.key.toLowerCase() : event.key)
      if (!mod && (typing(event.target) || event.altKey)) return
      const handlers = latest.current
      const sequence = pending ? `${pending} ${key}` : null
      if (sequence && handlers[sequence]) {
        pending = null
        event.preventDefault()
        return handlers[sequence](event)
      }
      if (Object.keys(handlers).some((k) => k.startsWith(key + " "))) {
        pending = key
        clearTimeout(timer)
        timer = setTimeout(() => (pending = null), 800)
        return
      }
      pending = null
      const handler = handlers[key]
      if (handler) {
        event.preventDefault()
        handler(event)
      }
    }
    addEventListener("keydown", onKey)
    return () => {
      removeEventListener("keydown", onKey)
      clearTimeout(timer)
    }
  }, [enabled])
}
