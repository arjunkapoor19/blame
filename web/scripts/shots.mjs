// Screenshots of the viewer for reviewing its look: desktop, phone, light and dark.
//   ah view --no-open --port 4646 &      (any database with some history)
//   npm run shots -- http://127.0.0.1:4646 /path/to/a/file.py 58
// Uses an installed Chromium browser (AH_BROWSER, or Chrome/Brave/Edge in the usual places).
import { existsSync, mkdirSync } from "node:fs"
import { chromium } from "playwright-core"

const [base = "http://127.0.0.1:4646", file, line = "1"] = process.argv.slice(2)
const out = new URL("../screenshots/", import.meta.url).pathname
mkdirSync(out, { recursive: true })

const browsers = [
  process.env.AH_BROWSER,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean)
const executablePath = browsers.find((p) => existsSync(p))
if (!executablePath) throw new Error("no Chromium browser found: set AH_BROWSER")

const browser = await chromium.launch({ executablePath })
const fileHash = file ? `#/file/${encodeURIComponent(file)}` : null

async function shot(name, hash, { theme = "light", width = 1440, height = 900, act } = {}) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2, colorScheme: theme })
  await page.goto(base + "/" + hash)
  await page.waitForLoadState("networkidle")
  if (act) await act(page)
  await page.waitForTimeout(700) // let entrance animations settle
  await page.screenshot({ path: `${out}${name}-${theme}.png` })
  await page.close()
  console.log(`${name}-${theme}.png`)
}

for (const theme of ["light", "dark"]) {
  await shot("home", "#/", { theme })
  if (fileHash) {
    await shot("file", fileHash, { theme })
    await shot("story", `${fileHash}?line=${line}`, { theme })
  }
  await shot("sessions", "#/sessions", { theme })
}
const [session] = await (await fetch(base + "/api/sessions")).json()
if (session) await shot("session", `#/session/${encodeURIComponent(session.id)}`)
await shot("palette", "#/", { act: (p) => p.keyboard.press("Meta+k").then(() => p.keyboard.type("shop")) })
if (fileHash) {
  await shot("hover", fileHash, { act: (p) => p.hover(`[data-n="${line}"]`) })
  await shot("phone-file", fileHash, { width: 390, height: 844 })
  await shot("phone-story", `${fileHash}?line=${line}`, { width: 390, height: 844, theme: "dark" })
}
await browser.close()
