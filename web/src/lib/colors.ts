import type { Author } from "../api"

const SLOTS = 8

/** A stable colour slot per agent name (FNV-1a), so an agent looks the same everywhere. */
export function slot(name: string): number {
  let hash = 0x811c9dc5
  for (const char of name) {
    hash ^= char.charCodeAt(0)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash % SLOTS
}

export function nameColor(name: string): string {
  if (name === "you") return "var(--you)"
  if (name === "·" || name === "untracked" || !name) return "var(--nobody)"
  return `var(--agent-${slot(name)})`
}

export function authorColor(author: Author | null | undefined): string {
  return nameColor(author?.name ?? "")
}

export function authorLabel(name: string): string {
  return name === "·" ? "pre-existing" : name === "you" ? "outside any agent" : name
}
