import { useEffect, useRef } from "react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { cn } from "cn"
import type { ChipTone, RunViewState } from "./store"

const TONES: Record<ChipTone | "ok" | "muted" | "key", string> = {
  ok: "bg-emerald-600/10 text-emerald-700 dark:text-emerald-400",
  fail: "bg-destructive/10 text-destructive",
  muted: "bg-muted text-muted-foreground",
  key: "bg-blue-600/10 text-blue-700 dark:text-blue-400",
  look: "bg-muted text-muted-foreground",
  code: "bg-violet-600/10 text-violet-700 dark:text-violet-400",
  gate: "bg-amber-600/10 text-amber-700 dark:text-amber-400",
}

// The trajectory, newest at the bottom: one row per turn, the agent's tool
// chips landing live as it works. Auto-follow pins the scroll to the tail;
// any manual turn click unpins it (the caller's scrub()).
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
  onSelect: (turn: number) => void
  onTrace: (turn: number) => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const activity =
    state.order.reduce(
      (n, k) => n + state.turns[k].tools.length + state.turns[k].notes.length + (state.turns[k].gate ? 1 : 0),
      0,
    ) + state.order.length

  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight
  }, [activity, follow])

  return (
    <div ref={ref} className="min-h-0 flex-1 overflow-auto p-3">
      {state.order.length === 0 ? (
        <div className="flex h-full items-center justify-center px-6 text-center text-sm text-muted-foreground">
          Waiting for the first turn… the agent is reading the statement and thinking.
        </div>
      ) : (
        <ol className="space-y-1.5">
          {state.order.map((number) => {
            const t = state.turns[number]
            const isSel = selected === number
            const verified = t.summary === "VERIFIED"
            return (
              <li
                key={number}
                className={cn(
                  "rounded-lg border px-2.5 py-2 transition-colors",
                  isSel
                    ? "border-primary/50 bg-secondary shadow-sm"
                    : verified
                      ? "border-emerald-600/40 bg-emerald-600/5"
                      : "border-transparent bg-card",
                )}
              >
                <div className="flex flex-wrap items-center gap-1.5">
                  <button
                    onClick={() => onSelect(number)}
                    className="rounded px-1 font-mono text-xs font-semibold hover:bg-muted"
                    title={number === 0 ? "baseline / canned gate" : `turn ${number}`}
                  >
                    {number === 0 ? "t₀" : `t${number}`}
                  </button>
                  {t.tools.map((chip, i) => (
                    <Badge
                      key={i}
                      variant="secondary"
                      className={cn("font-mono text-[11px] font-normal", TONES[chip.tone], t.running && i === t.tools.length - 1 && "animate-pulse")}
                    >
                      {chip.name}
                    </Badge>
                  ))}
                  {t.gate ? (
                    <Badge variant="secondary" className={cn("max-w-full truncate font-normal", t.gate.verified ? TONES.ok : TONES.fail)}>
                      {t.gate.verified
                        ? "gate ✓ VERIFIED"
                        : `gate ✗ ${t.gate.clean ?? 0}/${t.gate.total ?? "?"}${t.gate.error ? " · " + t.gate.error : ""}`}
                    </Badge>
                  ) : null}
                  {t.summary && !t.gate?.verified ? (
                    <Badge
                      variant="secondary"
                      className={cn(
                        "max-w-full truncate font-normal",
                        t.summary === "loop abort" ? TONES.fail : verified ? TONES.ok : TONES.muted,
                      )}
                    >
                      {t.summary}
                      {t.charsK != null ? ` · ${t.charsK}k` : ""}
                    </Badge>
                  ) : null}
                  {(state.tools[number]?.length ?? 0) > 0 ? (
                    <Button
                      size="xs"
                      variant="ghost"
                      className="ml-auto text-muted-foreground"
                      title="what the agent said this turn"
                      onClick={() => onTrace(number)}
                    >
                      trace ▸
                    </Button>
                  ) : null}
                </div>
                {t.notes.length ? (
                  <ul className="mt-1 space-y-0.5 pl-1">
                    {t.notes.map((note, i) => (
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
