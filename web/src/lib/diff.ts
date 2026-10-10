/** Unified-diff lines (`@@` headers, `+`, `-`, ` `) with old and new line numbers. */
export type DiffLine = { tag: "+" | "-" | " " | "@"; text: string; old: number | null; new: number | null; at: number }

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

/** `at` is the line's index in the input, so a marked index from the API still finds it. */
export function numbered(lines: string[], firstOld = 1, firstNew = 1): DiffLine[] {
  let old = firstOld
  let now = firstNew
  const out: DiffLine[] = []
  lines.forEach((raw, at) => {
    const hunk = HUNK.exec(raw)
    if (hunk) {
      old = Number(hunk[1])
      now = Number(hunk[3])
      out.push({ tag: "@", text: raw, old: null, new: null, at })
    } else if (raw.startsWith("\\")) {
      return
    } else if (raw.startsWith("+")) {
      out.push({ tag: "+", text: raw.slice(1), old: null, new: now++, at })
    } else if (raw.startsWith("-")) {
      out.push({ tag: "-", text: raw.slice(1), old: old++, new: null, at })
    } else {
      out.push({ tag: " ", text: raw.slice(1), old: old++, new: now++, at })
    }
  })
  return out
}

export function stats(lines: string[]): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const l of lines) {
    if (l.startsWith("+")) added++
    else if (l.startsWith("-")) removed++
  }
  return { added, removed }
}
