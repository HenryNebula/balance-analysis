import { useRef, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Badge } from "@/components/ui/badge"
import { Loader2, ScanSearch } from "lucide-react"
import { toast } from "sonner"
import { api } from "@/lib/api"
import type { ProbeOutcome } from "@/lib/types"
import { useSetKey } from "@/lib/hooks"

// Dry-run "what would happen?": try every registered parser, persist nothing.
export function ProbeDialog({
  slug,
  sid,
  trigger,
}: {
  slug: string
  sid?: string
  trigger: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<ProbeOutcome | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const resultsRef = useRef<HTMLDivElement>(null)
  const pin = useSetKey(slug)
  const navigate = useNavigate()

  async function run() {
    setBusy(true)
    setOutcome(null)
    try {
      const result = sid
        ? await api.probeStored(slug, sid)
        : await api.probeFile(slug, file!)
      setOutcome(result)
      // probes finish in milliseconds — the outcome renders below the
      // button, so bring it into view or the click looks like a no-op
      requestAnimationFrame(() =>
        resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }),
      )
    } catch (e: any) {
      toast.error(`Probe failed: ${String(e.detail ?? e.message ?? e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (!o) setOutcome(null) }}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ScanSearch className="size-4" /> Probe every parser
          </DialogTitle>
          <DialogDescription>
            A dry run against every registered parser. Nothing is persisted — it answers
            "what would happen?".
          </DialogDescription>
        </DialogHeader>
        {sid ? null : (
          <div className="grid gap-1.5">
            <Label htmlFor="probe-file">Statement PDF</Label>
            <Input
              id="probe-file"
              type="file"
              accept="application/pdf"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
        )}
        <Button size="sm" disabled={busy || (!sid && !file)} onClick={run}>
          {busy ? <Loader2 className="animate-spin" /> : null}
          {busy ? "Probing…" : outcome ? "Re-run probe" : "Run probe"}
        </Button>
        {outcome ? (
          <div ref={resultsRef} className="space-y-3">
            <div className="text-sm">
              <span className="font-medium">{outcome.verified_count}</span> parser
              {outcome.verified_count === 1 ? "" : "s"} verified
              {outcome.note ? (
                <span className="text-muted-foreground"> — {outcome.note}</span>
              ) : null}
            </div>
            <div className="max-h-64 overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>parser</TableHead>
                    <TableHead>outcome</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {outcome.results.map((r) => (
                    <TableRow key={r.key}>
                      <TableCell className="break-all font-mono text-xs">{r.key}</TableCell>
                      <TableCell>
                        {r.ok ? (
                          <Badge variant="secondary" className="bg-emerald-600/10 text-emerald-700 dark:text-emerald-400">
                            verified · {r.rows} rows
                          </Badge>
                        ) : (
                          <span className="text-xs text-muted-foreground">
                            {(r.error || "did not verify").split("\n")[0].slice(0, 90)}
                          </span>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            {outcome.suggested_key ? (
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="text-muted-foreground">Exactly one verified:</span>
                <Button
                  size="sm"
                  onClick={() => {
                    pin.mutate(outcome.suggested_key!)
                    setOpen(false)
                  }}
                >
                  Pin to {outcome.suggested_key}
                </Button>
              </div>
            ) : null}
            {outcome.verified_count === 0 ? (
              <DialogFooter className="sm:justify-start">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setOpen(false)
                    navigate(`/accounts/${slug}?launch=1${sid ? `&sid=${sid}` : ""}`)
                  }}
                >
                  No match — distill a parser
                </Button>
              </DialogFooter>
            ) : null}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  )
}
