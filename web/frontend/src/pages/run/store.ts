// The run view's event fold: every SSE trajectory event folds into this
// state and the timeline/pdf/code panes render from it. No DOM patching, no
// fetch races — selecting a turn and following are plain state.
//
// The unit is a TURN of the agent loop (turn 0 = the baseline/canned zone
// before the loop starts). Tool chips land live as the agent works; the
// turn summary line (arriving when the turn closes) settles the row.
import type { RunEvent, ToolCall } from "@/lib/types"

export type ChipTone = "look" | "code" | "gate" | "fail"

export interface ToolChip {
  name: string
  tone: ChipTone
}

export interface GateBadge {
  verified: boolean
  clean?: number
  total?: number
  error?: string
}

export interface TurnState {
  turn: number
  tools: ToolChip[] // chips in arrival order
  gate: GateBadge | null
  summary: string | null
  charsK: number | null
  notes: string[]
  running: boolean // opened, summary not yet seen
}

export interface RunViewState {
  order: number[] // turn numbers in arrival order
  turns: Record<number, TurnState>
  tools: Record<number, ToolCall[]> // per-turn transcript, live
  parser: Record<number, string> // parser.py snapshot as of each turn
  currentCode: string // the latest snapshot (live code pane)
  selections: Record<number, RunEvent> // last look event per turn
  status: string
  terminal: boolean
  finishMessage: string | null
  model: string // from run_started — available while live, no refetch needed
  convergedKey: string // from the converged event (attach/canned runs may
  // never persist it into the run record — the banner needs it anyway)
  saved: boolean | null
}

export const initialRunState: RunViewState = {
  order: [],
  turns: {},
  tools: {},
  parser: {},
  currentCode: "",
  selections: {},
  status: "running",
  terminal: false,
  finishMessage: null,
  model: "",
  convergedKey: "",
  saved: null,
}

const TONES: Record<string, ChipTone> = {
  grep: "look",
  render: "look",
  table: "look",
  python: "look",
  read: "code",
  write: "code",
  edit: "code",
  run: "gate",
}

function turn(state: RunViewState, number: number, baseline = false): TurnState {
  return state.turns[number] ?? {
    turn: number,
    tools: [],
    gate: null,
    summary: null,
    charsK: null,
    notes: baseline ? ["baseline (staged parser.py)"] : [],
    running: true,
  }
}

function withTurn(state: RunViewState, t: TurnState): RunViewState {
  if (!state.turns[t.turn]) state.order.push(t.turn)
  state.turns[t.turn] = t
  return { ...state, turns: { ...state.turns }, order: [...state.order] }
}

function gateBadge(verdict: RunEvent["verdict"]): GateBadge {
  if (!verdict) return { verified: false }
  const results = verdict.results ?? []
  return {
    verified: Boolean(verdict.verified),
    clean: verdict.clean,
    total: results.length || undefined,
    error: (results.find((r) => r.error)?.error ?? "").split("\n")[0].slice(0, 80) || undefined,
  }
}

function clearAllRunning(state: RunViewState): RunViewState {
  // a terminal event settles every in-flight turn — none may keep its
  // "running" pulse after the run ended
  const turns = Object.fromEntries(
    Object.entries(state.turns).map(([k, t]) => [
      Number(k),
      t.summary ? t : { ...t, running: false, summary: t.summary ?? "stopped" },
    ]),
  )
  return { ...state, turns }
}

export function runReducer(
  state: RunViewState,
  event: RunEvent | { type: "run_status"; status: string },
): RunViewState {
  if (event.type === "run_started") {
    return { ...state, model: (event as RunEvent).model || state.model }
  }
  if (event.type === "frames") return state // header info; nothing to fold
  const number = (event as RunEvent).turn ?? 0
  switch (event.type) {
    case "tool": {
      const e = event as RunEvent
      let t = turn(state, number)
      t = { ...t, tools: [...t.tools, { name: e.name ?? "?", tone: TONES[e.name ?? ""] ?? "look" }] }
      const next = withTurn(state, t)
      next.tools = {
        ...state.tools,
        [number]: [...(state.tools[number] ?? []), { name: e.name ?? "?", args: e.args, result: e.result }],
      }
      return next
    }
    case "parser": {
      const e = event as RunEvent
      const next = { ...state, currentCode: e.content ?? state.currentCode }
      next.parser = { ...state.parser, [number]: e.content ?? "" }
      return next
    }
    case "gate": {
      const e = event as RunEvent
      const t = turn(state, number, e.baseline)
      const gated = { ...t, gate: gateBadge(e.verdict) }
      if (e.baseline && gated.summary === null) gated.summary = "baseline"
      return withTurn(state, gated)
    }
    case "look": {
      const t = turn(state, number)
      const next = withTurn(state, t)
      next.selections = {
        ...state.selections,
        [number]: { ...(event as RunEvent), rect: (event as RunEvent).rect ?? [] },
      }
      return next
    }
    case "turn": {
      const e = event as RunEvent
      let t = turn(state, number)
      t = {
        ...t,
        running: false,
        summary: e.summary ?? t.summary,
        charsK: e.chars_k ?? t.charsK,
        notes: [...t.notes, ...(e.notes ?? [])],
      }
      return withTurn(state, t)
    }
    case "elided": {
      const e = event as RunEvent
      const t = turn(state, number)
      return withTurn(state, {
        ...t,
        notes: [...t.notes, `${e.forced ? "overflow — forced " : ""}elision (${e.chars ?? 0} chars)`],
      })
    }
    case "chat_failed": {
      const e = event as RunEvent
      const t = turn(state, number, false)
      return withTurn(state, {
        ...t,
        tools: [...t.tools, { name: "chat", tone: "fail" as const }],
        notes: [...t.notes, String(e.error ?? "").slice(0, 120)],
      })
    }
    case "converged":
      return clearAllRunning({
        ...state,
        status: "converged",
        terminal: true,
        convergedKey: (event as RunEvent).key || state.convergedKey,
        saved: (event as RunEvent).saved ?? true,
        finishMessage: `VERIFIED — parser ${((event as RunEvent).key || "") + ((event as RunEvent).saved ?? true ? " saved to the registry" : " (draft not saved)")}`,
      })
    case "failed":
      return clearAllRunning({
        ...state,
        status: "failed",
        terminal: true,
        finishMessage: `Failed — ${(event as RunEvent).reason || ""}`,
      })
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
