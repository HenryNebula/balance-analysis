import { Badge } from "@/components/ui/badge"
import { cn } from "cn"

// Semantic colors: green = verified/parsed, red = hard failure, amber = needs
// attention (no parser yet, orphaned), blue = in flight, muted = neutral end
// state. Same map the Jinja run view used.
const MAP: Record<string, { label: string; className: string }> = {
  parsed: { label: "parsed", className: "bg-emerald-600/10 text-emerald-700 dark:text-emerald-400" },
  failed: { label: "failed", className: "bg-destructive/10 text-destructive" },
  unparsed: { label: "unparsed", className: "bg-muted text-muted-foreground" },
  duplicate: { label: "duplicate", className: "bg-muted text-muted-foreground" },
  running: { label: "running", className: "bg-blue-600/10 text-blue-700 dark:text-blue-400" },
  cancelling: { label: "cancelling…", className: "bg-amber-500/15 text-amber-700 dark:text-amber-400" },
  converged: { label: "converged ✓", className: "bg-emerald-600/10 text-emerald-700 dark:text-emerald-400" },
  cancelled: { label: "cancelled", className: "bg-muted text-muted-foreground" },
  orphaned: { label: "orphaned", className: "bg-amber-500/15 text-amber-700 dark:text-amber-400" },
}

export function StatusChip({ status, className }: { status: string; className?: string }) {
  const entry = MAP[status] ?? { label: status, className: "bg-muted text-muted-foreground" }
  return (
    <Badge variant="secondary" className={cn(entry.className, className)}>
      {entry.label}
    </Badge>
  )
}
