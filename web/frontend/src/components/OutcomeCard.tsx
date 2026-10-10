import { Link } from "react-router-dom"
import { Alert } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { CheckCircle2, Diamond, ShieldX } from "lucide-react"
import type { Attempt, UploadOutcome } from "@/lib/types"

// Per-parser attempt rows — shared by the pinned-failure card and the
// statement page's failed state. Collapsed by default: harmless try-all
// failures shouldn't look alarming.
export function AttemptsTable({ attempts }: { attempts: Attempt[] }) {
  if (!attempts.length) return null
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>parser</TableHead>
          <TableHead>outcome</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {attempts.map((a) => (
          <TableRow key={a.key}>
            <TableCell className="font-mono text-xs">{a.key}</TableCell>
            <TableCell className="text-muted-foreground">
              {a.ok ? `verified ${a.rows} rows` : (a.error || "did not verify").split("\n")[0]}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

// The outcome of an upload / re-parse. Failure copy follows the review-loop
// ruling: a failed try-all is the normal first step for a new bank, so it
// reads amber and calm — never a wall of red stack traces.
export function OutcomeCard({
  outcome,
  slug,
  onLaunch,
  onPin,
}: {
  outcome: UploadOutcome
  slug: string
  onLaunch?: () => void
  onPin?: (key: string) => void
}) {
  const sid = outcome.id || outcome.statement?.id

  if (outcome.status === "parsed") {
    const s = outcome.statement?.summary
    return (
      <Alert className="border-emerald-600/30 bg-emerald-600/5 text-foreground">
        <CheckCircle2 className="size-4 text-emerald-600" />
        <div>
          <span className="font-medium text-emerald-700 dark:text-emerald-400">Parsed</span>{" "}
          with <code className="font-mono text-xs">{outcome.key}</code>
          {s
            ? ` — ${s.transactions} transactions, closing ${s.closing_balance?.toLocaleString("en-US", { minimumFractionDigits: 2 })}`
            : ""}
          {sid ? (
            <>
              {" · "}
              <Link className="font-medium underline underline-offset-4" to={`/accounts/${slug}/statements/${sid}`}>
                view statement
              </Link>
            </>
          ) : null}
        </div>
      </Alert>
    )
  }

  if (outcome.status === "duplicate") {
    return (
      <Alert>
        <span className="font-medium">Already stored</span> — this exact PDF (same content) is
        already in the account
        {sid ? (
          <>
            {" · "}
            <Link className="font-medium underline underline-offset-4" to={`/accounts/${slug}/statements/${sid}`}>
              view it
            </Link>
          </>
        ) : null}
        .
      </Alert>
    )
  }

  if (outcome.status === "error") {
    return (
      <Alert variant="destructive">
        {outcome.message || "The upload could not be processed."}
      </Alert>
    )
  }

  // failed
  if (outcome.pinned) {
    return (
      <Alert variant="destructive" className="space-y-3">
        <ShieldX className="size-4" />
        <div className="space-y-1">
          <div className="font-medium">The pinned parser did not verify this statement.</div>
          <p className="text-muted-foreground">
            The arithmetic gate rejects a parse whose balances don't reconcile — nothing was
            recorded as parsed. The original PDF is safe.
          </p>
        </div>
        <details className="text-sm">
          <summary className="cursor-pointer text-muted-foreground select-none">
            {outcome.attempts?.length || 0} attempt(s)
          </summary>
          <div className="mt-2 rounded-md border bg-muted/40">
            <AttemptsTable attempts={outcome.attempts || []} />
          </div>
        </details>
        {sid ? (
          <div>
            {/* text-foreground: the destructive Alert's color would otherwise
                cascade into the outline button and read as a red action */}
            <Button size="sm" variant="outline" className="text-foreground" asChild>
              <Link to={`/accounts/${slug}/statements/${sid}`}>Open statement</Link>
            </Button>
          </div>
        ) : null}
      </Alert>
    )
  }

  // unpinned failure — the calm first-step state
  return (
    <Alert className="space-y-3 border-amber-500/40 bg-amber-500/5">
      <div>
        <Diamond className="inline size-3.5 text-amber-600" />{" "}
        <span className="font-medium">No parser for this bank yet — that's the normal first step.</span>{" "}
        Every registered parser was tried; none verified against this statement's arithmetic.
      </div>
      <details className="text-sm">
        <summary className="cursor-pointer text-muted-foreground select-none">
          diagnostics (harmless try-all failures)
        </summary>
        <div className="mt-2 rounded-md border bg-muted/40">
          <AttemptsTable attempts={outcome.attempts || []} />
          {outcome.note ? (
            <div className="border-t px-3 py-2 text-xs text-muted-foreground">{outcome.note}</div>
          ) : null}
        </div>
      </details>
      <div className="flex flex-wrap items-center gap-2">
        {onLaunch ? (
          <Button size="sm" onClick={onLaunch}>
            Distill a parser for this bank
          </Button>
        ) : null}
      </div>
      {outcome.suggest_pin && onPin ? (
        <div className="text-sm text-muted-foreground">
          Exactly one parser verified on a previous attempt —{" "}
          <Button size="xs" variant="outline" onClick={() => onPin(outcome.suggest_pin!)}>
            pin to {outcome.suggest_pin}
          </Button>
        </div>
      ) : null}
      {outcome.ambiguous && outcome.ambiguous.length > 1 && onPin ? (
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
          Several parsers verified — pick one:
          {outcome.ambiguous.map((k) => (
            <Button key={k} size="xs" variant="outline" onClick={() => onPin(k)}>
              {k}
            </Button>
          ))}
        </div>
      ) : null}
    </Alert>
  )
}
