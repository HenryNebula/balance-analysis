import { useEffect, useMemo, useState } from "react"
import { Link, useParams } from "react-router-dom"
import { useQueryClient } from "@tanstack/react-query"
import { Button } from "@/components/ui/button"
import { StatusChip } from "@/components/StatusChip"
import { ArrowLeft, Ban, Crosshair, ShieldX, X } from "lucide-react"
import { cn } from "cn"
import { useCancelRun, useRun } from "@/lib/hooks"
import { useRunEvents } from "./run/useRunEvents"
import { Timeline } from "./run/Timeline"
import { CodePane } from "./run/CodePane"
import { PdfPane } from "./run/PdfPane"

// The distill run workspace: the statement under the microscope on the left,
// the trajectory + round artifacts on the right. Everything renders from the
// SSE event fold in ./run/store — auto-follow, scrubbing, and the code/trace
// pane are plain state, so they can't race each other.
export default function RunView() {
  const { id } = useParams()
  const qc = useQueryClient()
  const { data: run, isLoading } = useRun(id)
  const { state } = useRunEvents(id)
  const cancel = useCancelRun()

  const [selected, setSelected] = useState<number | null>(null)
  const [follow, setFollow] = useState(true)
  const [paneMode, setPaneMode] = useState<"code" | "output">("code")

  const lastRound = state.order.length ? state.order[state.order.length - 1] : null

  // auto-follow keeps the newest round selected; a manual click unpins it
  useEffect(() => {
    if (follow && lastRound != null) setSelected(lastRound)
  }, [follow, lastRound, state.rounds[lastRound ?? -1]?.hasOutput])

  // terminal: refresh the run record (auto_parse outcomes land at converge)
  useEffect(() => {
    if (state.terminal && id) qc.invalidateQueries({ queryKey: ["run", id] })
  }, [state.terminal]) // eslint-disable-line react-hooks/exhaustive-deps

  const scrub = (round: number) => {
    setFollow(false)
    setSelected(round)
    setPaneMode("code")
  }
  const trace = (round: number) => {
    setFollow(false)
    setSelected(round)
    setPaneMode("output")
  }

  // the newest probe's selection stays on the page while the next round
  // thinks — the pane only blanks when a scrubbed round has no selection
  // (the Jinja UI blanked between rounds; that read as flicker)
  const lastSelection = useMemo(() => {
    const keys = Object.keys(state.selections)
    return keys.length ? state.selections[Number(keys[keys.length - 1])] : undefined
  }, [state.selections])
  const shownSelection = (selected != null ? state.selections[selected] : undefined)
    ?? (follow ? lastSelection : undefined)

  if (isLoading || !run) {
    return (
      <div className="flex h-svh items-center justify-center text-sm text-muted-foreground">
        loading run…
      </div>
    )
  }

  const active = state.status === "running" || state.status === "cancelling"

  return (
    <div className="flex h-svh flex-col bg-background">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b px-4">
        <Link
          to={`/accounts/${run.account_slug}`}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" /> {run.display_name || run.account_slug}
        </Link>
        <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
          <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-xs text-secondary-foreground">
            {run.mode === "maintain" ? `maintain ${run.key ?? ""}` : "draft"}
          </span>
          {(state.model || run.model) ? <span>{state.model || run.model}</span> : null}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <StatusChip status={state.status} />
          {!state.terminal ? (
            <Button
              size="sm"
              variant={follow ? "secondary" : "ghost"}
              onClick={() => setFollow(!follow)}
              title="Follow the newest round automatically — any manual click unpins"
            >
              <Crosshair data-icon="inline-start" className={cn(follow && "text-blue-600")} />
              {follow ? "following" : "follow"}
            </Button>
          ) : null}
          {active ? (
            <Button size="sm" variant="outline" onClick={() => id && cancel.mutate(id)}>
              <X data-icon="inline-start" /> Cancel
            </Button>
          ) : null}
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <PdfPane
          slug={run.account_slug}
          sid={run.sid ?? null}
          pages={run.pages ?? 1}
          selection={shownSelection}
        />

        <aside className="flex w-[27rem] shrink-0 flex-col border-l bg-muted/20">
          <div className="border-b bg-card px-3 py-2 text-sm font-semibold">Trajectory</div>
          <Timeline
            state={state}
            selected={selected}
            follow={follow}
            onSelect={scrub}
            onTrace={trace}
          />

          {state.terminal ? (
            state.status === "converged" ? (
              <div className="space-y-2 border-t border-emerald-600/30 bg-emerald-600/5 p-3 text-sm">
                <div className="font-medium text-emerald-700 dark:text-emerald-400">
                  Parser {run.converged_key || "?"}{" "}
                  {run.auto_parse === null
                    ? "verified but not registered (attach or canned run)."
                    : "saved to the registry."}
                </div>
                {run.auto_parse && run.auto_parse.length ? (
                  <div className="text-muted-foreground">
                    {run.auto_parse.filter((e) => e.status === "parsed").length} of{" "}
                    {run.auto_parse.length} pending statement(s) parsed.
                  </div>
                ) : null}
                <Button size="sm" asChild>
                  <Link to={`/accounts/${run.account_slug}`}>Go to account</Link>
                </Button>
              </div>
            ) : (
              <div
                className={cn(
                  "flex items-center gap-2 border-t px-3 py-2.5 text-sm",
                  state.status === "failed"
                    ? "border-destructive/30 bg-destructive/5 text-destructive"
                    : "border-border bg-muted/50 text-muted-foreground",
                )}
              >
                {state.status === "failed" ? (
                  <ShieldX className="size-4 shrink-0" />
                ) : (
                  <Ban className="size-4 shrink-0" />
                )}
                {state.finishMessage}
              </div>
            )
          ) : null}

          <CodePane
            runId={id ?? ""}
            round={selected}
            mode={paneMode}
            liveRound={active ? lastRound : null}
            liveOutput={selected != null ? state.outputs[selected] : undefined}
            onModeChange={setPaneMode}
          />
        </aside>
      </div>
    </div>
  )
}
