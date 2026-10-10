// The run view's event fold: every SSE trajectory event folds into this
// state and the timeline/pdf/code panes render from it. No DOM patching, no
// fetch races — selecting a round and following are plain state.
import type { RunEvent } from "@/lib/types"

export interface Badge {
  id: string // one badge per category; refiring replaces it
  text: string
  tone: "ok" | "fail" | "muted" | "key"
}

export interface RoundState {
  number: number
  kind: "probe" | "script" | null
  badges: Badge[]
  notes: string[]
  running: boolean
  hasOutput: boolean
}

export interface RunViewState {
  order: number[] // round numbers in arrival order
  rounds: Record<number, RoundState>
  selections: Record<number, RunEvent> // last probe_select per round
  outputs: Record<number, string> // live stdout tail from round_ran
  status: string
  terminal: boolean
  finishMessage: string | null
  model: string // from run_started — available while live, no refetch needed
}

export const initialRunState: RunViewState = {
  order: [],
  rounds: {},
  selections: {},
  outputs: {},
  status: "running",
  terminal: false,
  finishMessage: null,
  model: "",
}

function round(state: RunViewState, number: number): RoundState {
  return state.rounds[number] ?? { number, kind: null, badges: [], notes: [], running: false, hasOutput: false }
}

function withRound(state: RunViewState, r: RoundState): RunViewState {
  if (!state.rounds[r.number]) state.order.push(r.number)
  state.rounds[r.number] = r
  return { ...state, rounds: { ...state.rounds }, order: [...state.order] }
}

function badge(r: RoundState, b: Badge): RoundState {
  return { ...r, badges: [...r.badges.filter((x) => x.id !== b.id), b] }
}

function gateSummary(verdict: RunEvent["verdict"]): { text: string; tone: Badge["tone"] } {
  if (!verdict) return { text: "gate ?", tone: "muted" }
  if (verdict.verified) return { text: "gate ✓", tone: "ok" }
  const first = (verdict.results || [])[0] || {}
  const error = (first.error || "").split("\n")[0].slice(0, 60)
  return {
    text: `gate ✗ ${verdict.clean ?? 0}/${verdict.results?.length ?? "?"}${error ? " · " + error : ""}`,
    tone: "fail",
  }
}

function clearAllRunning(state: RunViewState): RunViewState {
  // a terminal event settles every in-flight round — none may keep its
  // "running…" badge (text OR pulse) after the run ended
  const rounds = Object.fromEntries(
    Object.entries(state.rounds).map(([k, r]) => [
      Number(k),
      {
        ...r,
        running: false,
        badges: r.badges.map((b) =>
          b.id === "ran" && b.text.startsWith("running")
            ? { ...b, text: "stopped", tone: "muted" as const }
            : b,
        ),
      },
    ]),
  )
  return { ...state, rounds }
}

export function runReducer(state: RunViewState, event: RunEvent | { type: "run_status"; status: string }): RunViewState {
  if (event.type === "run_started") {
    return { ...state, model: (event as RunEvent).model || state.model }
  }
  // every case below except the terminal ones carries a round number — the
  // harness guarantees it; the non-null assertions encode that contract
  const number = (event as RunEvent).round!
  switch (event.type) {
    case "round_saved": {
      let r = round(state, number)
      r = badge(r, { id: "kind", text: event.kind === "probe" ? "probe" : "script", tone: event.kind === "probe" ? "key" : "muted" })
      r = badge(r, { id: "ran", text: "running…", tone: "key" }) // blue = in flight
      r.running = true
      r.kind = (event.kind as "probe" | "script") ?? r.kind
      return withRound(state, r)
    }
    case "round_ran": {
      let r = round(state, number)
      r.running = false
      r = badge(r, { id: "ran", text: event.returncode === 0 ? "ran ✓" : "ran ✗", tone: event.returncode === 0 ? "ok" : "fail" })
      const next = withRound(state, r)
      if (event.stdout_tail) {
        // immutable folds: identity-based consumers (useMemo, memo props)
        // must see a new object, not an in-place mutation
        next.outputs = { ...state.outputs, [number]: event.stdout_tail }
        next.rounds[number] = { ...next.rounds[number], hasOutput: true }
      }
      return next
    }
    case "gate": {
      let r = round(state, number)
      r.running = false
      const g = gateSummary(event.verdict)
      r = badge(r, { id: "gate", ...g })
      return withRound(state, r)
    }
    case "probe_select": {
      const r = round(state, number)
      const next = withRound(state, r)
      next.selections = {
        ...state.selections,
        [number]: { ...event, rect: event.rect ?? [] },
      }
      return next
    }
    case "lint_rejected": {
      let r = round(state, number)
      r.running = false
      r = badge(r, { id: "lint", text: "lint ✗", tone: "fail" })
      r.notes = [...r.notes, String(event.violation || "").slice(0, 120)]
      return withRound(state, r)
    }
    case "chat_failed": {
      let r = round(state, number)
      r.running = false
      r = badge(r, { id: "chat", text: "chat ✗", tone: "fail" })
      r.notes = [...r.notes, String(event.error || "").slice(0, 120)]
      return withRound(state, r)
    }
    case "feedback_sent": {
      const r = round(state, number)
      r.notes = [...r.notes, String(event.summary || "").slice(0, 140)]
      return withRound(state, r)
    }
    case "converged":
      return clearAllRunning({
        ...state,
        status: "converged",
        terminal: true,
        finishMessage: `Converged — parser ${event.key || ""}${event.saved ? " saved to the registry" : ""}`,
      })
    case "failed":
      return clearAllRunning({ ...state, status: "failed", terminal: true, finishMessage: `Failed — ${event.reason || ""}` })
    case "cancelled":
      return clearAllRunning({ ...state, status: "cancelled", terminal: true, finishMessage: "Cancelled." })
    case "run_status":
      return clearAllRunning({
        ...state,
        status: (event as { status: string }).status,
        terminal: true,
        finishMessage: `Run ${(event as { status: string }).status}.`,
      })
    default:
      return state
  }
}
