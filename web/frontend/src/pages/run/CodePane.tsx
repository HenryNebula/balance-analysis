import { useEffect, useState } from "react"
import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "cn"
import { useTurn } from "@/lib/hooks"
import type { ToolCall } from "@/lib/types"

// The bottom pane: parser.py as of the selected turn (write/edit events
// carry snapshots, so scrubbing shows the file at that moment), or the
// turn's tool transcript — every call with its result. The LIVE turn
// renders from the SSE fold alone: no fetch, it lands the moment the
// agent's tools do.
function Transcript({ tools }: { tools: ToolCall[] }) {
  if (!tools.length) return <>{/* nothing said this turn */}</>
  return (
    <>
      {tools.map((call, i) => (
        <div key={i} className="mb-3">
          <div className="text-zinc-400">
            <span className="font-semibold text-zinc-200">▸ {call.name}</span>{" "}
            {call.args ? JSON.stringify(call.args) : ""}
          </div>
          <pre className="mt-0.5 whitespace-pre-wrap font-mono text-xs leading-relaxed text-zinc-400">
            {(call.result ?? "").slice(0, 4000)}
          </pre>
        </div>
      ))}
    </>
  )
}

export function CodePane({
  runId,
  turn,
  mode,
  liveTurn,
  liveCode,
  liveTools,
  onModeChange,
}: {
  runId: string
  turn: number | null
  mode: "parser" | "trace"
  liveTurn: number | null // the turn currently executing, while the run lives
  liveCode: string | undefined // latest parser.py snapshot from the fold
  liveTools: ToolCall[] | undefined
  onModeChange: (mode: "parser" | "trace") => void
}) {
  const isLive = turn != null && turn === liveTurn
  const { data: detail, isLoading } = useTurn(runId, isLive ? null : turn)

  // "code-fresh" flash when a new parser snapshot lands
  const [flash, setFlash] = useState(0)
  useEffect(() => {
    if (liveCode !== undefined || detail?.code) setFlash((f) => f + 1)
  }, [liveCode, detail?.code]) // eslint-disable-line react-hooks/exhaustive-deps

  const code = isLive ? liveCode : detail?.code
  const tools = isLive ? liveTools : detail?.tools

  const title =
    turn == null
      ? "—"
      : mode === "trace"
        ? `${turn === 0 ? "baseline" : `turn ${turn}`} transcript${isLive ? " · live" : ""}`
        : `parser.py${turn != null ? ` @ ${turn === 0 ? "t₀" : `t${turn}`}` : ""}`

  const body =
    turn == null
      ? "select a turn to see parser.py and what the agent said"
      : mode === "trace"
        ? (tools ?? []).length
          ? null // rendered as the transcript below
          : isLive
            ? "the agent is thinking…"
            : "(no tool calls recorded for this turn)"
        : isLoading && !isLive
          ? null
          : code ?? `(no parser.py yet at ${turn === 0 ? "the baseline" : `turn ${turn}`})`

  return (
    <div className="flex h-[38%] min-h-48 flex-col border-t bg-zinc-950 text-zinc-100">
      <div className="flex items-center gap-1 border-b border-zinc-800 px-3 py-1.5">
        <span className="mr-2 font-mono text-xs text-zinc-400">{title}</span>
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={() => onModeChange("parser")}
            className={cn(
              "rounded px-2 py-0.5 text-xs",
              mode === "parser" ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-300",
            )}
          >
            parser
          </button>
          <button
            onClick={() => onModeChange("trace")}
            className={cn(
              "rounded px-2 py-0.5 text-xs",
              mode === "trace" ? "bg-zinc-800 text-zinc-100" : "text-zinc-500 hover:text-zinc-300",
            )}
          >
            trace
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {body === null ? (
          mode === "trace" ? (
            <Transcript tools={tools ?? []} />
          ) : (
            <Skeleton className="h-4 w-2/3 bg-zinc-800" />
          )
        ) : (
          <pre key={flash} className="whitespace-pre-wrap font-mono text-xs leading-relaxed text-zinc-200">
            {body}
          </pre>
        )}
      </div>
    </div>
  )
}
