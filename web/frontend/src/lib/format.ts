// Formatting mirrors the Jinja filters in web/app.py so numbers read the
// same in both UIs.

export function money(v: number | null | undefined): string {
  if (v == null) return "—"
  return v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

export function signed(v: number | null | undefined): string {
  if (v == null) return "—"
  return (v >= 0 ? "+ " : "- ") + money(Math.abs(v))
}

export function period(p: { start: string; end: string } | null | undefined): string {
  if (!p) return "—"
  return p.start.slice(0, 7) === p.end.slice(0, 7)
    ? p.start.slice(0, 7)
    : `${p.start} → ${p.end}`
}

export function dt(iso: string | null | undefined): string {
  return (iso || "").slice(0, 16).replace("T", " ")
}

export function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KiB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}

export function duration(created?: string | null, ended?: string | null): string {
  if (!created || !ended) return ""
  const seconds = Math.max(
    0,
    (new Date(ended).getTime() - new Date(created).getTime()) / 1000,
  )
  const s = Math.floor(seconds)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`
}

export const TERMINAL_STATUSES = ["converged", "failed", "cancelled", "orphaned"]
