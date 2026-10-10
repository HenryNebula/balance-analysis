import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"
import { api } from "./api"

export function useAccounts() {
  return useQuery({ queryKey: ["accounts"], queryFn: api.listAccounts })
}

export function useAccount(slug: string | undefined) {
  return useQuery({
    queryKey: ["account", slug],
    queryFn: () => api.getAccount(slug!),
    enabled: !!slug,
  })
}

export function useRegistry() {
  return useQuery({ queryKey: ["registry"], queryFn: api.registry })
}

export function useCreateAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ name, key }: { name: string; key: string }) =>
      api.createAccount(name, key),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
    onError: (e: any) => toast.error(String(e.detail ?? e.message ?? e)),
  })
}

export function useSetKey(slug: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (key: string) => api.setKey(slug, key),
    onSuccess: (_data, key) => {
      qc.invalidateQueries({ queryKey: ["account", slug] })
      qc.invalidateQueries({ queryKey: ["accounts"] })
      qc.invalidateQueries({ queryKey: ["registry"] })
      toast.success(key ? `Parser pinned: ${key}` : "Parser unpinned — back to detection mode")
    },
    onError: (e: any) => toast.error(String(e.detail ?? e.message ?? e)),
  })
}

export function useUpload(slug: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (file: globalThis.File) => api.upload(slug, file),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account", slug] })
      qc.invalidateQueries({ queryKey: ["accounts"] })
    },
  })
}

export function useReparse(slug: string, sid: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (key?: string) => api.reparse(slug, sid, key),
    onSuccess: (outcome) => {
      qc.invalidateQueries({ queryKey: ["statement", slug, sid] })
      qc.invalidateQueries({ queryKey: ["account", slug] })
      if (outcome.status === "parsed") {
        toast.success(`Parsed with ${outcome.key} — ${outcome.statement?.summary?.transactions} transactions`)
      } else if (outcome.status === "failed") {
        toast.error("Parse failed — see the statement page for attempts")
      }
    },
    onError: (e: any) => toast.error(String(e.detail ?? e.message ?? e)),
  })
}

export function useReparseAll(slug: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.reparseAll(slug),
    onSuccess: (report) => {
      qc.invalidateQueries({ queryKey: ["account", slug] })
      const changed = (report.results || []).filter((r: any) => r.changed).length
      toast.success(`Re-parsed ${report.results.length} statement(s) — ${changed} changed`)
    },
    onError: (e: any) => toast.error(String(e.detail ?? e.message ?? e)),
  })
}

export function useDeleteStatement(slug: string, sid: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api.deleteStatement(slug, sid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["account", slug] })
      qc.invalidateQueries({ queryKey: ["accounts"] })
    },
  })
}

export function useDeleteAccount() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (slug: string) => api.deleteAccount(slug),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["accounts"] }),
  })
}

export function useStatement(slug: string | undefined, sid: string | undefined) {
  return useQuery({
    queryKey: ["statement", slug, sid],
    queryFn: () => api.getStatement(slug!, sid!),
    enabled: !!slug && !!sid,
  })
}

export function useRuns() {
  return useQuery({
    queryKey: ["runs"],
    queryFn: api.runs,
    refetchInterval: (query) =>
      (query.state.data || []).some(
        (r) => r.status === "running" || r.status === "cancelling",
      )
        ? 2000
        : false,
  })
}

export function useRun(id: string | undefined) {
  return useQuery({
    queryKey: ["run", id],
    queryFn: () => api.run(id!),
    enabled: !!id,
  })
}

export function useCancelRun() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.cancelRun(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  })
}

export function useRound(id: string, number: number | null) {
  return useQuery({
    queryKey: ["round", id, number],
    queryFn: () => api.round(id, number!),
    enabled: number != null,
    // the round_saved event can beat the manager's workdir resolution — the
    // file exists but the run record doesn't know where yet; retry quietly
    retry: 3,
    retryDelay: 900,
  })
}

export function useRoundOutput(id: string, number: number | null) {
  return useQuery({
    queryKey: ["round-output", id, number],
    queryFn: () => api.roundOutput(id, number!),
    enabled: number != null,
  })
}
