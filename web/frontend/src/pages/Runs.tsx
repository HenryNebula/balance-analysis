import { Link } from "react-router-dom"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Button } from "@/components/ui/button"
import { StatusChip } from "@/components/StatusChip"
import { useCancelRun, useRuns } from "@/lib/hooks"
import { dt } from "@/lib/format"
import { X } from "lucide-react"

export default function Runs() {
  const { data: runs, isLoading } = useRuns()
  const cancel = useCancelRun()

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Distill runs</h1>
      <p className="max-w-2xl text-sm text-muted-foreground">
        One run at a time — bounded LLM spend. A run streams its whole trajectory live:
        every round's script, probe selections on the statement, gate verdicts.
      </p>
      {isLoading ? (
        <Skeleton className="h-40 rounded-xl" />
      ) : !runs?.length ? (
        <div className="rounded-xl border border-dashed py-16 text-center text-sm text-muted-foreground">
          No distill runs yet. Launch one from an account with unparsed statements.
        </div>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>account</TableHead>
                  <TableHead>mode</TableHead>
                  <TableHead>status</TableHead>
                  <TableHead>started</TableHead>
                  <TableHead className="text-right">duration</TableHead>
                  <TableHead className="w-10" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => {
                  const active = run.status === "running" || run.status === "cancelling"
                  return (
                    <TableRow key={run.id} className="cursor-pointer">
                      <TableCell className="font-medium">
                        <Link to={`/runs/${run.id}`} className="after:absolute after:inset-0">
                          {run.display_name || run.account_slug}
                        </Link>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {run.mode === "maintain" ? `maintain ${run.key ?? ""}` : "draft"}
                      </TableCell>
                      <TableCell>
                        <StatusChip status={run.status} />
                      </TableCell>
                      <TableCell className="text-muted-foreground">{dt(run.created_at)}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {run.duration ?? ""}
                      </TableCell>
                      <TableCell className="relative z-10 text-right">
                        {active ? (
                          <Button
                            size="icon-xs"
                            variant="ghost"
                            title="Cancel run"
                            onClick={() => cancel.mutate(run.id)}
                          >
                            <X />
                          </Button>
                        ) : null}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
