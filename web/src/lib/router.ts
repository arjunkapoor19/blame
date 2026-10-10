/** Hash routes, so `ah view` can open the page at any file and line, and links can be shared. */
import { useSyncExternalStore } from "react"
import { flushSync } from "react-dom"

export type Route =
  | { name: "home" }
  | { name: "file"; path: string; line: number | null }
  | { name: "sessions" }
  | { name: "session"; id: string }

export function parse(hash: string): Route {
  const [where, query = ""] = hash.replace(/^#/, "").split("?")
  const parts = where.split("/").filter(Boolean)
  const params = new URLSearchParams(query)
  if (parts[0] === "file" && parts[1]) {
    const line = Number(params.get("line"))
    return { name: "file", path: decodeURIComponent(parts.slice(1).join("/")), line: line > 0 ? line : null }
  }
  if (parts[0] === "sessions") return { name: "sessions" }
  if (parts[0] === "session" && parts[1]) return { name: "session", id: decodeURIComponent(parts[1]) }
  return { name: "home" }
}

export function href(route: Route): string {
  switch (route.name) {
    case "file":
      return `#/file/${encodeURIComponent(route.path)}${route.line ? `?line=${route.line}` : ""}`
    case "sessions":
      return "#/sessions"
    case "session":
      return `#/session/${encodeURIComponent(route.id)}`
    default:
      return "#/"
  }
}

const listeners = new Set<() => void>()
let current: Route = typeof location === "undefined" ? { name: "home" } : parse(location.hash)

function page(route: Route): string {
  return route.name === "file" ? `file:${route.path}` : route.name === "session" ? `session:${route.id}` : route.name
}

if (typeof window !== "undefined") {
  addEventListener("hashchange", () => {
    const next = parse(location.hash)
    const moved = page(next) !== page(current)
    const notify = () => {
      current = next
      listeners.forEach((l) => l())
    }
    // a cross-fade between pages; moving between lines of one file stays instant
    const smooth = moved && "startViewTransition" in document && !matchMedia("(prefers-reduced-motion: reduce)").matches
    if (smooth) document.startViewTransition(() => flushSync(notify))
    else notify()
  })
}

export function useRoute(): Route {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => current,
  )
}

export function go(route: Route, replace = false) {
  const target = href(route)
  if (replace) {
    history.replaceState(null, "", target)
    dispatchEvent(new HashChangeEvent("hashchange"))
  } else location.hash = target
}
