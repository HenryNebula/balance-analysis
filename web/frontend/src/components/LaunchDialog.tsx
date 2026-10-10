import { useEffect, useState } from "react"
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { FlaskConical, Loader2 } from "lucide-react"
import { api } from "@/lib/api"
import type { Meta } from "@/lib/types"
import { period } from "@/lib/format"

// Launch a distill run: the LLM harness drafts a parser for statements no
// registered parser verifies. Draft = new bank; maintain = evolve the pin
// (for format drift). One run at a time — bounded LLM spend.
export function LaunchDialog({
  slug,
  statements,
  defaultIds,
  pinnedKey,
  model,
  trigger,
  open,
  onOpenChange,
}: {
  slug: string
  statements: Meta[]
  defaultIds?: string[]
  pinnedKey?: string | null
  model?: string
  trigger?: React.ReactNode
  open?: boolean
  onOpenChange?: (open: boolean) => void
}) {
  const [internalOpen, setInternalOpen] = useState(false)
  const isControlled = open !== undefined
  const isOpen = isControlled ? open : internalOpen
  const setOpen = onOpenChange ?? setInternalOpen

  const [mode, setMode] = useState("draft")
  const [ids, setIds] = useState<string[]>(defaultIds ?? statements.map((s) => s.id))
  const [rounds, setRounds] = useState("")
  const [maxProbes, setMaxProbes] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const navigate = useNavigate()

  useEffect(() => {
    if (isOpen) {
      setIds(defaultIds ?? statements.map((s) => s.id))
      setMode("draft")
      setError("")
    }
  }, [isOpen]) // eslint-disable-line react-hooks/exhaustive-deps

  async function launch() {
    setBusy(true)
    setError("")
    try {
      const { run_id } = await api.launchRun({
        account_slug: slug,
        statement_ids: ids,
        mode,
        key: mode === "maintain" ? pinnedKey ?? undefined : undefined,
        rounds: rounds ? Number(rounds) : undefined,
        max_probes: maxProbes ? Number(maxProbes) : undefined,
      })
      navigate(`/runs/${run_id}`)
    } catch (e: any) {
      setError(String(e.detail ?? e.message ?? e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={isOpen} onOpenChange={setOpen}>
      {trigger ? <DialogTrigger asChild>{trigger}</DialogTrigger> : null}
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FlaskConical className="size-4" /> Register new bank
          </DialogTitle>
          <DialogDescription>
            The distill harness drafts a parser from your statements and saves it only after
            the arithmetic gate verifies it. The run streams live — you can watch every round
            and cancel anytime.
            {model ? ` Model: ${model}.` : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid gap-1.5">
            <Label>Mode</Label>
            <Select value={mode} onValueChange={setMode}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="draft">draft — register a new bank</SelectItem>
                {pinnedKey ? (
                  <SelectItem value="maintain">maintain — evolve {pinnedKey}</SelectItem>
                ) : null}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label>Statements to learn from</Label>
            <div className="max-h-40 space-y-1 overflow-auto rounded-md border p-2">
              {statements.length === 0 ? (
                <p className="px-1 py-2 text-xs text-muted-foreground">
                  No stored statements — upload one first.
                </p>
              ) : (
                statements.map((s) => (
                  <label key={s.id} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-sm hover:bg-muted/60">
                    <input
                      type="checkbox"
                      className="accent-foreground"
                      checked={ids.includes(s.id)}
                      onChange={(e) =>
                        setIds((prev) =>
                          e.target.checked ? [...prev, s.id] : prev.filter((x) => x !== s.id),
                        )
                      }
                    />
                    <span className="min-w-0 flex-1 truncate">{s.original_filename}</span>
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {s.status === "parsed" ? period(s.period) : s.status}
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="grid gap-1.5">
              <Label htmlFor="launch-rounds">Rounds (optional)</Label>
              <Input
                id="launch-rounds"
                type="number"
                min={1}
                placeholder="until verified"
                value={rounds}
                onChange={(e) => setRounds(e.target.value)}
              />
            </div>
            <div className="grid gap-1.5">
              <Label htmlFor="launch-probes">Max probes (optional)</Label>
              <Input
                id="launch-probes"
                type="number"
                min={0}
                placeholder="default"
                value={maxProbes}
                onChange={(e) => setMaxProbes(e.target.value)}
              />
            </div>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
        </div>
        <DialogFooter className="flex-row justify-between">
          <Button variant="ghost" onClick={() => setOpen(false)}>
            Close
          </Button>
          <Button disabled={busy || ids.length === 0} onClick={launch}>
            {busy ? <Loader2 className="animate-spin" /> : null}
            Launch run
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
