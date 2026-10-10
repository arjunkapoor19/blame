/** Light, dark, or the system's choice; remembered per browser when storage allows. */
import { useEffect, useState } from "react"

export type ThemeChoice = "system" | "light" | "dark"
const KEY = "ah-theme"
const dark = () => matchMedia("(prefers-color-scheme: dark)")

function stored(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY)
    return value === "light" || value === "dark" ? value : "system"
  } catch {
    return "system"
  }
}

function apply(choice: ThemeChoice) {
  document.documentElement.dataset.theme = choice === "system" ? (dark().matches ? "dark" : "light") : choice
}

export function useTheme(): [ThemeChoice, () => void] {
  const [choice, setChoice] = useState<ThemeChoice>(stored)
  useEffect(() => {
    apply(choice)
    try {
      if (choice === "system") localStorage.removeItem(KEY)
      else localStorage.setItem(KEY, choice)
    } catch {
      // private mode: the choice lasts until the tab closes
    }
    if (choice !== "system") return
    const media = dark()
    const follow = () => apply("system")
    media.addEventListener("change", follow)
    return () => media.removeEventListener("change", follow)
  }, [choice])
  const next = () => setChoice((c) => (c === "system" ? "light" : c === "light" ? "dark" : "system"))
  return [choice, next]
}
