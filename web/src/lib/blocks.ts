import type { BlameLine } from "../api"

/** Runs of consecutive lines written by the same change, like the blocks of `git blame`. */
export type Block = { start: number; end: number; a: number | null } // indexes into lines, end exclusive

export function blocks(lines: BlameLine[]): Block[] {
  const out: Block[] = []
  lines.forEach((line, i) => {
    const last = out[out.length - 1]
    if (last && last.a === line.a) last.end = i + 1
    else out.push({ start: i, end: i + 1, a: line.a })
  })
  return out
}

/** The first line of the next (dir 1) or previous (dir -1) block from line index `i`;
 * going back from inside a block lands on its own start first. */
export function nextBlock(all: Block[], i: number, dir: 1 | -1): number | null {
  const at = all.findIndex((b) => i >= b.start && i < b.end)
  if (at < 0) return null
  if (dir === -1 && i > all[at].start) return all[at].start
  const target = all[at + dir]
  return target ? target.start : null
}
