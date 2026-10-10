import { useEffect, useState } from "react"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "cn"
import { useRound, useRoundOutput } from "@/lib/hooks"

// The bottom pane: the selected round's script (round_N.py) or its trace
// (round_N.out). The live trace arrives WITH the round_ran event — no
// workdir round-trip, it lands the same moment the overlay does.
export function CodePane({
  runId,
  round,
  mode,
  liveRound,
  liveOutput,
  onModeChange,
}: {
  runId: string
  round: number | null
  mode: "code" | "output"
  liveRound: number | null // the round currently executing, while the run lives
  liveOutput: string | undefined
  onModeChange: (mode: "code" | "output") => void
}) {
  const { data: detail, isLoading } = useRound(runId, round)
  const { data: fileOutput } = useRoundOutput(
    runId,
    mode === "output" && liveOutput === undefined ? round : null,
  )

  // "code-fresh" flash when a new round's code lands
  const [flash, setFlash] = useState(0)
  useEffect(() => {
    if (detail) setFlash((f) => f + 1)
  }, [detail?.code]) // eslint-disable-line react-hooks/exhaustive-deps

  const title =
    round == null
      ? "—"
      : mode === "output"
        ? `round_${round}.out${liveRound === round && liveOutput !== undefined ? " · live tail" : ""}`
        : `round_${round}.py${detail ? " · " + detail.kind : ""}`

  const body =
    round == null
      ? "select a round to see its script and trace"
      : mode === "output"
        ? liveOutput ?? fileOutput ?? `(no captured output for round ${round})`
        : isLoading
          ? null
          : detail
            ? detail.code
            : `(round ${round} not resolvable yet — the harness may still be writing it)`

  return (
    <div className="flex h-[38%] min-h-48 flex-col border-t bg-zinc-950 text-zinc-100">
      <div className="flex items-center gap-1 border-b border-zinc-800 px-3 py-1.5">
        <span className="mr-2 font-mono text-xs text-zinc-400">{title}</span>
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={() => onModeChange("code")}
            className={cn(
              "rounded px-2 py-0.5 text-xs",
              mode === "code" ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-300",
            )}
          >
            script
          </button>
          <button
            onClick={() => onModeChange("output")}
            className={cn(
              "rounded px-2 py-0.5 text-xs",
              mode === "output" ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-300",
            )}
          >
            trace
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {body === null ? (
          <Skeleton className="h-4 w-2/3 bg-zinc-800" />
        ) : (
          <pre key={flash} className="whitespace-pre-wrap font-mono text-xs leading-relaxed text-zinc-200">
            {body}
          </pre>
        )}
      </div>
    </div>
  )
}
