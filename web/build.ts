/** Build steps around Vite: a size budget, and the source hash that lets
 * the Python tests notice a `web/` change that was never rebuilt into `agent_history/static/`. */
import { createHash } from "node:crypto"
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs"
import { join, relative, sep } from "node:path"
import { gzipSync } from "node:zlib"
import type { Plugin } from "vite"

const BUDGET = { js: 120 * 1024, css: 20 * 1024 } // gzipped, what the first paint needs

/** Every source file under `web/`, as the Python test also lists them: no `node_modules`, no
 * dot-files, no screenshots. Sorted, then hashed path by path. */
export function sourceHash(root: string): string {
  const files: string[] = []
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      if (name.startsWith(".") || name === "node_modules") continue
      const path = join(dir, name)
      if (statSync(path).isDirectory()) walk(path)
      else files.push(relative(root, path).split(sep).join("/"))
    }
  }
  walk(root)
  const hash = createHash("sha256")
  for (const file of files.filter((f) => !f.startsWith("screenshots/")).sort()) {
    hash.update(file + "\0")
    hash.update(readFileSync(join(root, file)))
    hash.update("\0")
  }
  return hash.digest("hex")
}

export function ahBuild(root: string): Plugin {
  return {
    name: "ah-build",
    apply: "build",
    writeBundle(options, bundle) {
      const out = options.dir!
      // the size budget: the entry chunk, what it imports statically, and their CSS
      const initial = new Set<string>()
      const css = new Set<string>()
      const visit = (name: string) => {
        const chunk = bundle[name]
        if (!chunk || chunk.type !== "chunk" || initial.has(name)) return
        initial.add(name)
        chunk.viteMetadata?.importedCss.forEach((c) => css.add(c))
        chunk.imports.forEach(visit)
      }
      Object.values(bundle).forEach((c) => c.type === "chunk" && c.isEntry && visit(c.fileName))
      const gz = (names: Set<string>) =>
        [...names].reduce((sum, n) => sum + gzipSync(readFileSync(join(out, n))).length, 0)
      const js = gz(initial)
      const styles = gz(css)
      const kb = (n: number) => `${(n / 1024).toFixed(1)} KB`
      this.info(`initial JS ${kb(js)} / ${kb(BUDGET.js)} · CSS ${kb(styles)} / ${kb(BUDGET.css)} (gzipped)`)
      if (js > BUDGET.js || styles > BUDGET.css) this.error("over the size budget: see web/build.ts")

      writeFileSync(join(out, ".source-hash"), sourceHash(root) + "\n")
    },
  }
}
