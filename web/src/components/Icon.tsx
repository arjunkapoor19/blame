/** A small set of line icons (24-unit grid, 1.75 stroke), drawn inline: no icon font, no requests. */
const PATHS: Record<string, string> = {
  read: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
  edit: "M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4Z M13.5 6.5l4 4",
  command: "M4 17l5-5-5-5 M12 19h8",
  test: "M9 3h6 M10 3v6L4.5 18.5A1.6 1.6 0 0 0 6 21h12a1.6 1.6 0 0 0 1.5-2.5L14 9V3 M7.5 14h9",
  agent: "M21 12a8 8 0 0 1-11.8 7L4 20l1.1-4.6A8 8 0 1 1 21 12Z",
  answer: "M12 3l2.1 5.2L19.5 9l-4 3.7 1.1 5.3L12 15.3 7.4 18l1.1-5.3-4-3.7 5.4-.8L12 3Z",
  subagent: "M6 3v12 M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z M18 9a9 9 0 0 1-9 9",
  error: "M12 9v4 M12 17h.01 M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z",
  thinking: "M9 18h6 M10 22h4 M12 2a7 7 0 0 0-4 12.7V16h8v-1.3A7 7 0 0 0 12 2Z",
  user: "M20 21a8 8 0 0 0-16 0 M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z",
  tool: "M14.7 6.3a4 4 0 0 0 5 5L21 13l-8 8-2-2 M3 21l7.5-7.5 M14.7 6.3 13 4.6l-8 8 2 2",
  approval: "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z M9 12l2 2 4-4",
  search: "M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z M21 21l-4.3-4.3",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10Z M12 1v2 M12 21v2 M4.2 4.2l1.4 1.4 M18.4 18.4l1.4 1.4 M1 12h2 M21 12h2 M4.2 19.8l1.4-1.4 M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z",
  system: "M3 4h18v12H3z M8 20h8 M12 16v4",
  close: "M18 6 6 18 M6 6l12 12",
  chevron: "M9 18l6-6-6-6",
  down: "M6 9l6 6 6-6",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Z M14 2v6h6",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z",
  sessions: "M3 6h18 M3 12h18 M3 18h12",
  keyboard: "M2 6h20v12H2z M6 10h.01 M10 10h.01 M14 10h.01 M18 10h.01 M7 14h10",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z M12 6v6l4 2",
  check: "M20 6 9 17l-5-5",
  x: "M18 6 6 18 M6 6l12 12",
  dot: "M12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
  history: "M3 12a9 9 0 1 0 3-6.7L3 8 M3 3v5h5 M12 7v5l3 2",
  link: "M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7 M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7",
}

export function Icon({ name, size = 14, className = "" }: { name: string; size?: number; className?: string }) {
  const d = PATHS[name] ?? PATHS.dot
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={`shrink-0 ${className}`}
      aria-hidden="true"
    >
      {d.split(" M").map((part, i) => (
        <path key={i} d={i ? "M" + part : part} />
      ))}
    </svg>
  )
}
