// Shapes returned by the FastAPI JSON branches (web/app.py wants_json).
// Verdict payloads from the harness are deliberately loose.

export interface Account {
  slug: string
  display_name: string
  registry_key: string | null
  registry_key_source: string | null
  created_at: string
  notes: string
}

export interface Meta {
  id: string
  sha256: string
  original_filename: string
  size_bytes: number
  uploaded_at: string
  status: "unparsed" | "parsed" | "failed"
  period: { start: string; end: string } | null
  summary: {
    transactions: number
    opening_balance: number
    closing_balance: number
  } | null
  history: Array<Record<string, unknown>>
}

export interface Chain {
  total: number
  ok: number
  breaks: Array<{ from_id: string; to_id: string }>
}

export interface PeriodWarning {
  id: string
  kind: "gap" | "overlap"
  days: number
}

export interface RunRow {
  id: string
  account_slug: string
  statement_ids: string[]
  mode: "draft" | "maintain"
  key: string | null
  status: "running" | "cancelling" | "converged" | "failed" | "cancelled" | "orphaned"
  created_at: string
  ended_at: string | null
  display_name?: string
  duration?: string
  converged_key?: string
  auto_parse?: Array<{ id: string; status: string; transactions?: number }> | null
}

export interface RunDetail extends RunRow {
  model?: string
  pages?: number
  sid?: string | null
}

export interface AccountRow {
  account: Account
  total: number
  parsed: number
  failed: number
  latest: Meta | null
  chain: Chain
  updated: string | null
}

export interface AccountDetailData {
  account: Account
  metas: Meta[]
  chain: Chain
  warnings: PeriodWarning[]
  runs: RunRow[]
  llm_model: string
}

export interface Attempt {
  key: string
  ok: boolean
  error?: string
  rows?: number
}

export interface UploadOutcome {
  status: "parsed" | "failed" | "duplicate" | "error"
  id?: string
  statement?: Meta
  key?: string
  attempts?: Attempt[]
  note?: string
  suggest_pin?: string
  ambiguous?: string[]
  pinned?: boolean
  message?: string
}

export interface ProbeOutcome {
  results: Array<{ key: string; ok: boolean; error?: string; rows?: number }>
  verified_count: number
  suggested_key: string | null
  note: string
}

export interface RegistryRow {
  key: string
  description: string
  accounts: number
  statements: number
}

export interface Txn {
  date: string
  description: string
  amount: number
  balance: number
  line?: number | null
}

export interface Ledger {
  opening_balance: number
  closing_balance: number
  transactions: Txn[]
  _key?: string
  _anchors?: Record<string, number>
}

export interface ProvBand {
  kind: "anchor" | "txn"
  index: number
  label: string
  left: number
  top: number
  width: number
  height: number
}

export interface ProvPage {
  page: number
  bands: ProvBand[]
}

export interface StatementDetailData {
  meta: Meta
  ledger: Ledger | null
  verified: boolean | null
  prov_pages: ProvPage[]
  llm_model: string
}

export interface RoundDetail {
  round: number
  kind: "probe" | "script"
  code: string
  verdict?: {
    verified?: boolean
    clean?: number
    results?: Array<{ error?: string }>
  } | null
}

// -- harness trajectory events (SSE) ------------------------------------------

export interface RunEvent {
  type: string
  ts?: string
  round?: number
  kind?: string
  label?: string
  rect?: number[]
  line_ids?: number[]
  page?: number
  verdict?: RoundDetail["verdict"]
  returncode?: number
  stdout_tail?: string
  violation?: string
  error?: string
  summary?: string
  key?: string
  saved?: boolean
  reason?: string
  model?: string
}
