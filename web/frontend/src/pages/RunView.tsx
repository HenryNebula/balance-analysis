import { useEffect, useMemo, useRef, useState } from "react"
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
// the trajectory + parser artifacts on the right. Everything renders from the
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
  const [paneMode, setPaneMode] = useState<"parser" | "trace">("parser")

  // -- resizable trajectory sidebar (width persists across visits) ----------
  const [sidebarW, setSidebarW] = useState(
    () => Number(localStorage.getItem("run-sidebar-w")) || 432,
  )
  const sidebarWRef = useRef(sidebarW)
  sidebarWRef.current = sidebarW
  const dragging = useRef(false)

  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (!dragging.current) return
      // width from the right edge: drag left = wider sidebar
      const w = window.innerWidth - e.clientX
      setSidebarW(Math.min(760, Math.max(320, w)))
    }
    const up = () => {
      if (!dragging.current) return
      dragging.current = false
      document.body.style.userSelect = ""
      localStorage.setItem("run-sidebar-w", String(sidebarWRef.current))
    }
    window.addEventListener("pointermove", move)
    window.addEventListener("pointerup", up)
    return () => {
      window.removeEventListener("pointermove", move)
      window.removeEventListener("pointerup", up)
    }
  }, [])

  const startDrag = (e: React.PointerEvent) => {
    e.preventDefault()
    dragging.current = true
    document.body.style.userSelect = "none"
  }

  const active = state.status === "running" || state.status === "cancelling"
  const lastTurn = state.order.length ? state.order[state.order.length - 1] : null
  const liveTurn = active ? lastTurn : null

  // auto-follow keeps the newest turn selected; a manual click unpins it
  useEffect(() => {
    if (follow && lastTurn != null) setSelected(lastTurn)
  }, [follow, lastTurn, state.turns[lastTurn ?? -1]?.tools.length])

  // terminal: refresh the run record (auto_parse outcomes land at converge)
  useEffect(() => {
    if (state.terminal && id) qc.invalidateQueries({ queryKey: ["run", id] })
  }, [state.terminal]) // eslint-disable-line react-hooks/exhaustive-deps

  const scrub = (turn: number) => {
    setFollow(false)
    setSelected(turn)
    setPaneMode("parser")
  }
  const trace = (turn: number) => {
    setFollow(false)
    setSelected(turn)
    setPaneMode("trace")
  }

  // the newest look stays on the page while the next turn thinks — the pane
  // only blanks when a scrubbed turn has no look (blanking read as flicker)
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

  return (
    <div className="flex h-svh flex-col bg-background">
      <header className="flex h-12 min-w-0 shrink-0 items-center gap-3 border-b px-4">
        {/* left cluster truncates; the status group never shrinks — long
            account names or models must not push the status chip off-screen */}
        <div className="flex min-w-0 flex-1 items-center gap-3">
          <Link
            to={`/accounts/${run.account_slug}`}
            className="inline-flex min-w-0 items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-3.5 shrink-0" />
            <span className="truncate">{run.display_name || run.account_slug}</span>
          </Link>
          <span className="flex min-w-0 items-center gap-1.5 text-sm text-muted-foreground">
            <span className="shrink-0 rounded bg-secondary px-1.5 py-0.5 font-mono text-xs text-secondary-foreground">
              {run.mode === "maintain" ? `maintain ${run.key ?? ""}` : "draft"}
            </span>
            {state.model || run.model ? (
              <span className="truncate">{state.model || run.model}</span>
            ) : null}
            {lastTurn != null ? (
              <span className="shrink-0 text-xs">
                {state.terminal ? `${lastTurn + 1} turns` : `t${lastTurn}`}
              </span>
            ) : null}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <StatusChip status={state.status} />
          {!state.terminal ? (
            <Button
              size="sm"
              variant={follow ? "secondary" : "ghost"}
              onClick={() => setFollow(!follow)}
              title="Follow the newest turn automatically — any manual click unpins"
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

        <aside
          className="relative flex shrink-0 flex-col border-l bg-muted/20"
          style={{ width: sidebarW }}
        >
          {/* drag to resize — the width persists in localStorage */}
          <div
            onPointerDown={startDrag}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize trajectory sidebar"
            title="Drag to resize"
            className="absolute inset-y-0 -left-1 z-10 w-2 cursor-col-resize touch-none after:absolute after:inset-y-0 after:left-1/2 after:w-px after:bg-transparent after:transition-colors hover:after:bg-primary/50"
          />
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
                  {(() => {
                    const key = run.converged_key || state.convergedKey
                    const registered = run.auto_parse !== null && run.auto_parse !== undefined
                    return key
                      ? `Parser ${key} ${registered ? "saved to the registry." : "verified (not registered)."}`
                      : "Parser verified (not registered)."
                  })()}
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
            turn={selected}
            mode={paneMode}
            liveTurn={liveTurn}
            liveCode={selected != null ? state.currentCode : undefined}
            liveTools={selected != null ? state.tools[selected] : undefined}
            onModeChange={setPaneMode}
          />
        </aside>
      </div>
    </div>
  )
}
