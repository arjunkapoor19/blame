import { describe, expect, test } from "vitest"
import type { BlameLine } from "../api"
import { blocks, nextBlock } from "./blocks"
import { nameColor, slot } from "./colors"
import { numbered, stats } from "./diff"
import { rank, score } from "./fuzzy"
import { href, parse } from "./router"
import { ago, duration } from "./time"

const line = (n: number, a: number | null): BlameLine => ({ n, text: `l${n}`, a, rewrites: 0 })

describe("blocks", () => {
  const lines = [line(1, null), line(2, 0), line(3, 0), line(4, 1), line(5, 0)]
  const all = blocks(lines)
  test("groups consecutive lines by author", () => {
    expect(all).toEqual([
      { start: 0, end: 1, a: null },
      { start: 1, end: 3, a: 0 },
      { start: 3, end: 4, a: 1 },
      { start: 4, end: 5, a: 0 },
    ])
  })
  test("moves between blocks", () => {
    expect(nextBlock(all, 1, 1)).toBe(3)
    expect(nextBlock(all, 2, -1)).toBe(1) // inside a block: back to its start first
    expect(nextBlock(all, 1, -1)).toBe(0)
    expect(nextBlock(all, 4, 1)).toBeNull()
  })
})

describe("router", () => {
  test("file routes round-trip, paths and lines included", () => {
    const route = { name: "file" as const, path: "/Users/me/my repo/shop.py", line: 58 }
    expect(href(route)).toBe("#/file/%2FUsers%2Fme%2Fmy%20repo%2Fshop.py?line=58")
    expect(parse(href(route))).toEqual(route)
    expect(parse("#/file/%2Fw%2Fa.py")).toEqual({ name: "file", path: "/w/a.py", line: null })
  })
  test("the server's links open where they should", () => {
    // agent_history/view.py builds these with quote(path, safe='')
    expect(parse("#/file/%2Fw%2Fshop.py?line=7")).toEqual({ name: "file", path: "/w/shop.py", line: 7 })
  })
  test("other routes", () => {
    expect(parse("#/session/ses_1%3A2")).toEqual({ name: "session", id: "ses_1:2" })
    expect(parse("#/sessions")).toEqual({ name: "sessions" })
    expect(parse("")).toEqual({ name: "home" })
    expect(parse("#/file/x?line=abc")).toEqual({ name: "file", path: "x", line: null })
  })
})

describe("diff", () => {
  test("numbers old and new lines from hunk headers", () => {
    const rows = numbered(["@@ -9,3 +9,4 @@", " a", "-b", "+c", "+d", " e"])
    expect(rows.map((r) => [r.tag, r.old, r.new])).toEqual([
      ["@", null, null], [" ", 9, 9], ["-", 10, null], ["+", null, 10], ["+", null, 11], [" ", 11, 12],
    ])
    expect(rows[3].at).toBe(3) // the API's marked index still points at the right row
  })
  test("a new file has no header and starts at line 1", () => {
    expect(numbered(["+x", "+y"]).map((r) => r.new)).toEqual([1, 2])
    expect(stats(["@@ -1 +1 @@", "-a", "+b", "+c"])).toEqual({ added: 2, removed: 1 })
  })
})

describe("fuzzy", () => {
  test("matches subsequences and prefers word starts", () => {
    expect(score("sp", "shop.py")).toBeGreaterThan(0)
    expect(score("zz", "shop.py")).toBe(-Infinity)
    expect(rank("shop", ["workshop/x.py", "src/shop.py", "other.py"], (s) => s)).toEqual(["src/shop.py", "workshop/x.py"])
    // letters scattered across a long path are noise next to a real match
    expect(rank("shop", ["ah-demo/shop.py", "agent-history/agent_history/hooks.py"], (s) => s)).toEqual(["ah-demo/shop.py"])
  })
})

describe("colours and time", () => {
  test("each agent keeps its own colour", () => {
    expect(new Set(["codex", "claude", "opencode"].map(slot)).size).toBe(3)
    expect(nameColor("you")).toBe("var(--you)")
    expect(nameColor("·")).toBe("var(--nobody)")
  })
  test("relative times and durations", () => {
    const now = Date.UTC(2026, 9, 11, 12)
    expect(ago(now - 30_000, now)).toBe("just now")
    expect(ago(now - 5 * 60_000, now)).toBe("5 min ago")
    expect(ago(now - 3 * 86_400_000, now)).toBe("3 days ago")
    expect(duration(0, 76_400)).toBe("1m 16s")
    expect(duration(0, 6_000)).toBe("6.0s")
  })
})
