import { Badge } from "@/components/ui/badge"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "cn"
import type { Chain, PeriodWarning } from "@/lib/types"

export function ChainChip({ chain, warnings = [] }: { chain: Chain; warnings?: PeriodWarning[] }) {
  const clean = chain.ok === chain.total
  const warnCount = warnings.length
  return (
    <span className="inline-flex items-center gap-1.5">
      <Tooltip>
        <TooltipTrigger asChild>
          <Badge
            variant="secondary"
            className={cn(
              "cursor-default",
              chain.total === 0
                ? "bg-muted text-muted-foreground"
                : clean
                  ? "bg-emerald-600/10 text-emerald-700 dark:text-emerald-400"
                  : "bg-destructive/10 text-destructive",
            )}
          >
            {chain.total === 0
              ? "no chain yet"
              : clean
                ? `chain ✓ ${chain.total}/${chain.total}`
                : `chain break ${chain.total - chain.ok}/${chain.total}`}
          </Badge>
        </TooltipTrigger>
        <TooltipContent>
          {chain.total === 0
            ? "Two parsed statements make a chain: one's closing balance must be the next's opening."
            : clean && !warnCount
              ? "Every statement's closing balance carries into the next one's opening."
              : chain.breaks.map((b, i) => (
                  <div key={i}>
                    {b.from_id.slice(0, 6)} → {b.to_id.slice(0, 6)}: closing ≠ opening
                  </div>
                ))}
        </TooltipContent>
      </Tooltip>
      {warnings.map((w) => (
        <Tooltip key={w.id}>
          <TooltipTrigger asChild>
            <Badge
              variant="secondary"
              className="cursor-default bg-amber-500/15 text-amber-700 dark:text-amber-400"
            >
              {w.kind} {w.days}d
            </Badge>
          </TooltipTrigger>
          <TooltipContent>
            {w.kind === "gap"
              ? `Statement ${w.id.slice(0, 6)} starts ${w.days} days after the previous one ends.`
              : `Statement ${w.id.slice(0, 6)} overlaps the previous one by ${w.days} days.`}
          </TooltipContent>
        </Tooltip>
      ))}
    </span>
  )
}
