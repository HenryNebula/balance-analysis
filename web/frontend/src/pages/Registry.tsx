import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { useRegistry } from "@/lib/hooks"

// The parser registry: one parser per bank, shared by every account. Parsers
// distilled at runtime land here too (user data in the overlay — never the
// repo), no restart needed.
export default function Registry() {
  const { data: rows, isLoading } = useRegistry()

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Registry</h1>
        <p className="text-sm text-muted-foreground">
          {rows ? `${rows.length} parser${rows.length === 1 ? "" : "s"}` : ""}
        </p>
      </div>
      {isLoading ? (
        <Skeleton className="h-48 rounded-xl" />
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>key</TableHead>
                  <TableHead>description</TableHead>
                  <TableHead className="text-right">accounts</TableHead>
                  <TableHead className="text-right">statements parsed</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {(rows ?? []).map((r) => (
                  <TableRow key={r.key}>
                    <TableCell className="font-mono text-xs">{r.key}</TableCell>
                    <TableCell className="max-w-lg text-muted-foreground">
                      {r.description || <span className="opacity-50">—</span>}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{r.accounts}</TableCell>
                    <TableCell className="text-right tabular-nums">{r.statements}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
