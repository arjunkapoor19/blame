/** Subsequence match with bonuses for runs and word starts; -Infinity when `query` doesn't match.
 * Each place the first letter occurs is tried, so "shop" finds `src/shop.py` at the word. */
export function score(query: string, text: string): number {
  const q = query.toLowerCase().replace(/\s+/g, "")
  if (!q) return 0
  const t = text.toLowerCase()
  let best = -Infinity
  for (let from = t.indexOf(q[0]); from >= 0; from = t.indexOf(q[0], from + 1)) {
    best = Math.max(best, from_(q, t, from))
  }
  return best - t.length * 0.01
}

function from_(q: string, t: string, start: number): number {
  let at = start
  let total = 0
  let run = 0
  for (const char of q) {
    const found = t.indexOf(char, at)
    if (found < 0) return -Infinity
    run = found === at && found !== start ? run + 1 : 0
    const wordStart = found === 0 || "/_-. ".includes(t[found - 1])
    total += 1 + run * 2 + (wordStart ? 3 : 0) - Math.min(found - at, 20) * 0.25
    at = found + 1
  }
  return total
}

export function rank<T>(query: string, items: T[], text: (item: T) => string, limit = 50): T[] {
  if (!query.trim()) return items.slice(0, limit)
  const scored = items.map((item) => ({ item, s: score(query, text(item)) })).filter((x) => x.s > -Infinity)
  const best = Math.max(...scored.map((x) => x.s))
  return scored
    .filter((x) => x.s >= best - Math.max(Math.abs(best) / 2, 2)) // far weaker than the best: noise
    .sort((a, b) => b.s - a.s)
    .slice(0, limit)
    .map((x) => x.item)
}
