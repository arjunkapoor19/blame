import { useState } from "react"
import { Palette, Shortcuts } from "./components/Palette"
import { TopBar } from "./components/TopBar"
import { useKeys } from "./lib/keys"
import { go, useRoute } from "./lib/router"
import { FileView } from "./views/FileView"
import { Home } from "./views/Home"
import { SessionView } from "./views/SessionView"
import { Sessions } from "./views/Sessions"

export function App() {
  const route = useRoute()
  const [overlay, setOverlay] = useState<"palette" | "keys" | null>(null)
  useKeys({
    "mod+k": () => setOverlay((o) => (o === "palette" ? null : "palette")),
    "/": () => setOverlay("palette"),
    "?": () => setOverlay((o) => (o === "keys" ? null : "keys")),
    "g s": () => go({ name: "sessions" }),
    "g f": () => go({ name: "home" }),
  })
  const close = () => setOverlay(null)
  return (
    <div className="flex h-full flex-col">
      <TopBar route={route} onSearch={() => setOverlay("palette")} />
      <main className="min-h-0 flex-1">
        {route.name === "file" ? (
          <FileView key={route.path} path={route.path} line={route.line} />
        ) : route.name === "sessions" ? (
          <Sessions />
        ) : route.name === "session" ? (
          <SessionView key={route.id} id={route.id} />
        ) : (
          <Home onSearch={() => setOverlay("palette")} />
        )}
      </main>
      {overlay === "palette" && <Palette onClose={close} />}
      {overlay === "keys" && <Shortcuts onClose={close} />}
    </div>
  )
}
