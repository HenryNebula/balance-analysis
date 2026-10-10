// Thin client over the FastAPI routes. The server content-negotiates: every
// request below sends Accept: application/json and gets the JSON branch.
// Form-oriented endpoints (account create, pin, re-parse) take form bodies —
// URLSearchParams sets the right content-type and FastAPI's Form() params
// read them fine.
//
// Domain outcomes (duplicate upload, failed parse) arrive as JSON bodies with
// a `status` field and real HTTP status codes; only protocol errors throw.

import type {
  AccountDetailData,
  AccountRow,
  ProbeOutcome,
  RegistryRow,
  RunDetail,
  RunRow,
  StatementDetailData,
  TurnDetail,
  UploadOutcome,
} from "./types"

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail))
    this.status = status
    this.detail = detail
  }
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
  return fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init.headers as object) },
  })
}

async function json(res: Response): Promise<any> {
  if (res.status === 204) return null
  const ct = res.headers.get("content-type") || ""
  const body: unknown = ct.includes("json") ? await res.json() : await res.text()
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in (body as object)
        ? (body as Record<string, unknown>).detail
        : body
    throw new ApiError(res.status, detail)
  }
  return body
}

function form(fields: Record<string, string>): RequestInit {
  return { method: "POST", body: new URLSearchParams(fields) }
}

export const api = {
  async listAccounts(): Promise<AccountRow[]> {
    const res = await request("/")
    const body = await json(res)
    return body.accounts
  },

  async getAccount(slug: string): Promise<AccountDetailData> {
    return json(await request(`/accounts/${slug}`))
  },

  async createAccount(displayName: string, registryKey: string): Promise<any> {
    return json(await request("/accounts", form({ display_name: displayName, registry_key: registryKey })))
  },

  async setKey(slug: string, registryKey: string): Promise<any> {
    return json(
      await request(`/accounts/${slug}/key`, form({ registry_key: registryKey })),
    )
  },

  async deleteAccount(slug: string): Promise<void> {
    await json(await request(`/accounts/${slug}`, { method: "DELETE" }))
  },

  // Returns the raw outcome whatever the status code — duplicates (409) and
  // failed parses (200) are domain results the caller renders, not errors.
  async upload(slug: string, file: File): Promise<UploadOutcome> {
    const body = new FormData()
    body.append("file", file, file.name)
    const res = await request(`/accounts/${slug}/statements`, { method: "POST", body })
    const ct = res.headers.get("content-type") || ""
    const parsed = ct.includes("json") ? await res.json() : { status: "error", message: await res.text() }
    return parsed as UploadOutcome
  },

  async reparse(slug: string, sid: string, key?: string): Promise<UploadOutcome> {
    const fields: Record<string, string> = {}
    if (key) fields.key = key
    return json(
      await request(`/accounts/${slug}/statements/${sid}/parse`, form(fields)),
    )
  },

  async reparseAll(slug: string): Promise<any> {
    return json(await request(`/accounts/${slug}/reparse`, { method: "POST" }))
  },

  async deleteStatement(slug: string, sid: string): Promise<void> {
    await json(await request(`/accounts/${slug}/statements/${sid}`, { method: "DELETE" }))
  },

  async getStatement(slug: string, sid: string): Promise<StatementDetailData> {
    return json(await request(`/accounts/${slug}/statements/${sid}`))
  },

  async probeStored(slug: string, sid: string): Promise<ProbeOutcome> {
    return json(
      await request(`/accounts/${slug}/probe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ statement_id: sid }),
      }),
    )
  },

  async probeFile(slug: string, file: File): Promise<ProbeOutcome> {
    const body = new FormData()
    body.append("file", file, file.name)
    return json(await request(`/accounts/${slug}/probe`, { method: "POST", body }))
  },

  async registry(): Promise<RegistryRow[]> {
    return json(await request("/registry.json"))
  },

  async runs(): Promise<RunRow[]> {
    return json(await request("/runs"))
  },

  async run(id: string): Promise<RunDetail> {
    return json(await request(`/runs/${id}`))
  },

  async cancelRun(id: string): Promise<void> {
    await json(await request(`/runs/${id}`, { method: "DELETE" }))
  },

  async launchRun(payload: {
    account_slug: string
    statement_ids: string[]
    mode: string
    key?: string
    turns?: number
  }): Promise<{ run_id: string }> {
    return json(
      await request("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    )
  },

  async turn(id: string, number: number): Promise<TurnDetail | null> {
    const res = await request(`/runs/${id}/turns/${number}`)
    if (!res.ok) return null // turn not resolvable (yet) — the run view retries
    return res.json()
  },
}
