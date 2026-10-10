import { useEffect, useRef } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "cn"
import type { RunViewState } from "./store"

const TONES: Record<string, string> = {
  ok: "bg-emerald-600/10 text-emerald-700 dark:text-emerald-400",
  fail: "bg-destructive/10 text-destructive",
  muted: "bg-muted text-muted-foreground",
  key: "bg-blue-600/10 text-blue-700 dark:text-blue-400",
}

// The trajectory, newest at the bottom. Auto-follow pins the scroll to the
// tail; any manual round click unpins it (the caller's scrub()).
export function Timeline({
  state,
  selected,
  follow,
  onSelect,
  onTrace,
}: {
  state: RunViewState
  selected: number | null
  follow: boolean
  onSelect: (round: number) => void
  onTrace: (round: number) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const activity =
    state.order.reduce(
      (n, k) => n + state.rounds[k].badges.length + state.rounds[k].notes.length + (state.rounds[k].hasOutput ? 1 : 0),
      0,
    ) + state.order.length

  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [activity, follow])

  return (
    <div ref={ref} className="min-h-0 flex-1 overflow-auto p-3">
      {state.order.length === 0 ? (
        <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted-foreground">
          Waiting for the first round… the harness is reading the statement and thinking.
        </div>
      ) : (
        <ol className="space-y-1.5">
          {state.order.map((number) => {
            const r = state.rounds[number]
            const isSel = selected === number
            return (
              <li
                key={number}
                className={cn(
                  "rounded-lg border px-2.5 py-2 transition-colors",
                  isSel
                    ? "border-primary/50 bg-secondary shadow-sm"
                    : "border-transparent bg-card",
                )}
              >
                <div className="flex items-center gap-1.5">
                  <button
                    onClick={() => onSelect(number)}
                    className="rounded px-1 font-mono text-xs font-semibold hover:bg-muted"
                    title={`round ${number}`}
                  >
                    r{number}
                  </button>
                  {r.badges.map((b) => (
                    <Badge
                      key={b.id}
                      variant="secondary"
                      className={cn("font-normal", TONES[b.tone], b.id === "ran" && r.running && "animate-pulse")}
                    >
                      {b.text}
                    </Badge>
                  ))}
                  {r.hasOutput ? (
                    <Button
                      size="xs"
                      variant="ghost"
                      className="ml-auto text-muted-foreground"
                      title="what this round printed"
                      onClick={() => onTrace(number)}
                    >
                      trace ▸
                    </Button>
                  ) : null}
                </div>
                {r.notes.length ? (
                  <ul className="mt-1 space-y-0.5 pl-1">
                    {r.notes.map((note, i) => (
                      <li key={i} className="text-xs leading-snug text-muted-foreground">
                        {note}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
