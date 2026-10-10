/** Syntax colours, loaded only when a file is shown: the core, the JavaScript regex engine (no
 * WebAssembly), two themes, and just the grammar the file needs. Never on the first paint. */
import type { HighlighterCore, LanguageRegistration } from "@shikijs/core"

type Grammar = () => Promise<{ default: LanguageRegistration[] }>

const GRAMMARS: Record<string, [string, Grammar]> = {
  py: ["python", () => import("@shikijs/langs/python")],
  ts: ["typescript", () => import("@shikijs/langs/typescript")],
  tsx: ["tsx", () => import("@shikijs/langs/tsx")],
  js: ["javascript", () => import("@shikijs/langs/javascript")],
  mjs: ["javascript", () => import("@shikijs/langs/javascript")],
  cjs: ["javascript", () => import("@shikijs/langs/javascript")],
  jsx: ["jsx", () => import("@shikijs/langs/jsx")],
  json: ["json", () => import("@shikijs/langs/json")],
  md: ["markdown", () => import("@shikijs/langs/markdown")],
  css: ["css", () => import("@shikijs/langs/css")],
  html: ["html", () => import("@shikijs/langs/html")],
  go: ["go", () => import("@shikijs/langs/go")],
  rs: ["rust", () => import("@shikijs/langs/rust")],
  java: ["java", () => import("@shikijs/langs/java")],
  kt: ["kotlin", () => import("@shikijs/langs/kotlin")],
  swift: ["swift", () => import("@shikijs/langs/swift")],
  rb: ["ruby", () => import("@shikijs/langs/ruby")],
  php: ["php", () => import("@shikijs/langs/php")],
  c: ["c", () => import("@shikijs/langs/c")],
  h: ["c", () => import("@shikijs/langs/c")],
  cpp: ["cpp", () => import("@shikijs/langs/cpp")],
  sh: ["shellscript", () => import("@shikijs/langs/shellscript")],
  bash: ["shellscript", () => import("@shikijs/langs/shellscript")],
  zsh: ["shellscript", () => import("@shikijs/langs/shellscript")],
  yaml: ["yaml", () => import("@shikijs/langs/yaml")],
  yml: ["yaml", () => import("@shikijs/langs/yaml")],
  toml: ["toml", () => import("@shikijs/langs/toml")],
  sql: ["sql", () => import("@shikijs/langs/sql")],
}

const MAX_LINES = 8000 // beyond this, plain text: colours aren't worth a frozen tab

export type Token = { content: string; style?: Record<string, string> }

let core: Promise<HighlighterCore> | undefined

function highlighter(): Promise<HighlighterCore> {
  core ??= Promise.all([
    import("@shikijs/core"),
    import("@shikijs/engine-javascript"),
    import("@shikijs/themes/github-light-default"),
    import("@shikijs/themes/github-dark-default"),
  ]).then(([{ createHighlighterCore }, { createJavaScriptRegexEngine }, light, dark]) =>
    createHighlighterCore({ themes: [light.default, dark.default], langs: [], engine: createJavaScriptRegexEngine() }),
  )
  return core
}

export function language(path: string): string | null {
  const ext = path.split("/").pop()?.split(".").pop()?.toLowerCase() ?? ""
  return GRAMMARS[ext]?.[0] ?? null
}

/** Tokens per line, or null when the language isn't known or the file is too big. */
export async function tokenize(code: string, path: string, lines: number): Promise<Token[][] | null> {
  const ext = path.split("/").pop()?.split(".").pop()?.toLowerCase() ?? ""
  const grammar = GRAMMARS[ext]
  if (!grammar || lines > MAX_LINES) return null
  const [hl, lang] = await Promise.all([highlighter(), grammar[1]()])
  await hl.loadLanguage(...lang.default)
  await idle()
  const result = hl.codeToTokens(code, {
    lang: grammar[0],
    themes: { light: "github-light-default", dark: "github-dark-default" },
    defaultColor: false,
  })
  return result.tokens.map((line) =>
    line.map((t) => ({ content: t.content, style: t.htmlStyle as Record<string, string> | undefined })),
  )
}

function idle(): Promise<void> {
  return new Promise((resolve) =>
    "requestIdleCallback" in window ? requestIdleCallback(() => resolve(), { timeout: 300 }) : setTimeout(resolve, 0),
  )
}
