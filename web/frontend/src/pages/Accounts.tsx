import { useEffect, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { ChainChip } from "@/components/ChainChip"
import { Plus } from "lucide-react"
import { useAccounts, useCreateAccount, useRegistry } from "@/lib/hooks"
import { dt, money, period } from "@/lib/format"

function CreateAccountDialog() {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [key, setKey] = useState("")
  const [error, setError] = useState("")
  const { data: registry } = useRegistry()
  const create = useCreateAccount()
  const navigate = useNavigate()

  useEffect(() => {
    if (open) {
      setName("")
      setKey("")
      setError("")
    }
  }, [open])

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button>
          <Plus data-icon="inline-start" /> New account
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>New account</DialogTitle>
          <DialogDescription>
            One account per bank account. Statements are stored durably under it, parsed
            with one shared parser per bank.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="grid gap-1.5">
            <Label htmlFor="account-name">
              Account name <span className="text-destructive">*</span>
            </Label>
            <Input
              id="account-name"
              placeholder="e.g. Commbank Everyday"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div className="grid gap-1.5">
            <Label>Parser (optional)</Label>
            <Select value={key} onValueChange={setKey}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder="detection mode (no pin)" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="none">detection mode (no pin)</SelectItem>
                {(registry ?? []).map((r) => (
                  <SelectItem key={r.key} value={r.key} className="font-mono text-xs">
                    {r.key}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Parsers are shared per bank — pinning selects the bank's format instead of
              trying every parser.
            </p>
          </div>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
        </div>
        <DialogFooter>
          <Button
            disabled={!name.trim() || create.isPending}
            onClick={async () => {
              try {
                const account = await create.mutateAsync({
                  name: name.trim(),
                  key: key && key !== "none" ? key : "",
                })
                setOpen(false)
                navigate(`/accounts/${account.slug}`)
              } catch (e: any) {
                setError(String(e.detail ?? e.message ?? e))
              }
            }}
          >
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default function Accounts() {
  const { data: rows, isLoading } = useAccounts()

  if (isLoading) {
    return (
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-40 rounded-xl" />
        ))}
      </div>
    )
  }

  if (!rows?.length) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
          <CreateAccountDialog />
        </div>
        <div className="flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed py-24 text-center">
          <p className="text-lg font-medium">No accounts yet</p>
          <p className="max-w-sm text-sm text-muted-foreground">
            Create an account for a bank, drop in a statement PDF, and statementledger
            verifies a parser against its arithmetic — or distills one.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
        <CreateAccountDialog />
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {rows.map(({ account, parsed, failed, total, latest, chain, updated }) => (
          <Link key={account.slug} to={`/accounts/${account.slug}`} className="group">
            <Card className="h-full transition-all group-hover:border-foreground/25 group-hover:shadow-sm">
              <CardContent className="space-y-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="font-medium leading-tight">{account.display_name}</div>
                  {account.registry_key ? (
                    <span className="shrink-0 rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px] text-secondary-foreground">
                      pinned
                    </span>
                  ) : (
                    <span className="shrink-0 rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      auto-detect
                    </span>
                  )}
                </div>
                <div className="text-3xl font-semibold tabular-nums tracking-tight">
                  {latest?.summary ? money(latest.summary.closing_balance) : "—"}
                </div>
                <div className="text-xs text-muted-foreground">
                  {latest?.summary ? `closing · ${period(latest.period)}` : "no parsed statement yet"}
                </div>
                <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  <span>
                    {total} statement{total === 1 ? "" : "s"}
                    {parsed ? ` · ${parsed} parsed` : null}
                    {failed ? ` · ${failed} failed` : null}
                  </span>
                  <ChainChip chain={chain} />
                </div>
                {updated ? (
                  <div className="text-[11px] text-muted-foreground/70">updated {dt(updated)}</div>
                ) : null}
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  )
}
