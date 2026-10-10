import { useEffect, useMemo, useState } from "react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { ChainChip } from "@/components/ChainChip"
import { StatusChip } from "@/components/StatusChip"
import { Dropzone } from "@/components/Dropzone"
import { OutcomeCard } from "@/components/OutcomeCard"
import { PinDialog } from "@/components/PinDialog"
import { ProbeDialog } from "@/components/ProbeDialog"
import { LaunchDialog } from "@/components/LaunchDialog"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { ArrowLeft, FlaskConical, KeyRound, RefreshCw, ScanSearch, Trash2 } from "lucide-react"
import { useAccount, useDeleteAccount, useReparseAll, useSetKey, useUpload } from "@/lib/hooks"
import { dt, money, period } from "@/lib/format"
import type { UploadOutcome } from "@/lib/types"

export default function AccountDetail() {
  const { slug } = useParams()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const { data, isLoading } = useAccount(slug)
  const upload = useUpload(slug ?? "")
  const pin = useSetKey(slug ?? "")
  const reparseAll = useReparseAll(slug ?? "")
  const deleteAccount = useDeleteAccount()
  const [outcome, setOutcome] = useState<UploadOutcome | null>(null)
  const [launchOpen, setLaunchOpen] = useState(false)

  // deep link from ProbeDialog's "no match" path: ?launch=1&sid=…
  useEffect(() => {
    if (params.get("launch")) {
      setLaunchOpen(true)
      params.delete("launch")
      params.delete("sid")
      setParams(params, { replace: true })
    }
  }, [params, setParams])

  const unparsed = useMemo(
    () => (data?.metas ?? []).filter((m) => m.status !== "parsed"),
    [data],
  )

  if (isLoading || !data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-24 rounded-xl" />
        <Skeleton className="h-64 rounded-xl" />
      </div>
    )
  }

  const { account, metas, chain, warnings, runs, llm_model } = data
  const parsedCount = metas.filter((m) => m.status === "parsed").length
  const failedCount = metas.filter((m) => m.status === "failed").length
  const latest = [...metas].reverse().find((m) => m.status === "parsed")

  async function onFile(file: File) {
    setOutcome(null)
    setOutcome(await upload.mutateAsync(file))
  }

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <Link
          to="/"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" /> Accounts
        </Link>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <h1 className="min-w-0 break-words text-2xl font-semibold tracking-tight">
              {account.display_name}
            </h1>
            {account.registry_key ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="inline-flex cursor-default items-center gap-1 rounded-full bg-secondary px-2 py-0.5 font-mono text-xs text-secondary-foreground">
                    <KeyRound className="size-3" /> {account.registry_key}
                  </span>
                </TooltipTrigger>
                <TooltipContent>Parser pinned — every parse uses this bank's format.</TooltipContent>
              </Tooltip>
            ) : (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span className="cursor-default rounded-full bg-secondary px-2 py-0.5 text-xs text-muted-foreground">
                    auto-detect
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  Detection mode: every registered parser is tried; the first that verifies wins.
                </TooltipContent>
              </Tooltip>
            )}
          </div>
          <div className="flex items-center gap-2">
            <PinDialog
              current={account.registry_key}
              onPin={(key) => pin.mutate(key)}
              trigger={
                <Button variant="outline" size="sm">
                  <KeyRound data-icon="inline-start" />
                  {account.registry_key ? "Change pin" : "Pin parser"}
                </Button>
              }
            />
            <ProbeDialog
              slug={account.slug}
              trigger={
                <Button variant="outline" size="sm">
                  <ScanSearch data-icon="inline-start" /> Probe
                </Button>
              }
            />
            {unparsed.length > 0 && (
              <LaunchDialog
                slug={account.slug}
                statements={unparsed}
                defaultIds={unparsed.map((m) => m.id)}
                pinnedKey={account.registry_key}
                model={llm_model}
                open={launchOpen}
                onOpenChange={setLaunchOpen}
                trigger={
                  <Button size="sm">
                    <FlaskConical data-icon="inline-start" />
                    {account.registry_key ? "Distill update" : "Register bank"}
                  </Button>
                }
              />
            )}
          </div>
        </div>
      </div>

      {/* summary strip */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm text-muted-foreground">
        <span>
          <strong className="font-semibold text-foreground tabular-nums">{metas.length}</strong>{" "}
          statement{metas.length === 1 ? "" : "s"}
          {parsedCount ? ` · ${parsedCount} parsed` : ""}
          {failedCount ? ` · ${failedCount} failed` : ""}
        </span>
        {latest?.summary ? (
          <span>
            latest {period(latest.period)}: closes at{" "}
            <strong className="font-semibold text-foreground tabular-nums">
              {money(latest.summary.closing_balance)}
            </strong>
          </span>
        ) : null}
        <ChainChip chain={chain} warnings={warnings} />
      </div>

      {/* upload */}
      <div className="space-y-4">
        <Dropzone onFile={onFile} busy={upload.isPending} />
        {outcome ? (
          <OutcomeCard
            outcome={outcome}
            slug={account.slug}
            onLaunch={() => setLaunchOpen(true)}
            onPin={(key) => pin.mutate(key)}
          />
        ) : null}
      </div>

      {/* statements */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Statements</h2>
          {account.registry_key && metas.length > 0 ? (
            <Button
              variant="ghost"
              size="sm"
              disabled={reparseAll.isPending}
              onClick={() => reparseAll.mutate()}
            >
              <RefreshCw data-icon="inline-start" />
              {reparseAll.isPending ? "Re-parsing…" : "Re-parse all with pin"}
            </Button>
          ) : null}
        </div>
        {metas.length === 0 ? (
          <div className="rounded-xl border border-dashed py-12 text-center text-sm text-muted-foreground">
            No statements yet — drop one above.
          </div>
        ) : (
          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>statement</TableHead>
                    <TableHead>period</TableHead>
                    <TableHead className="text-right">transactions</TableHead>
                    <TableHead className="text-right">closing</TableHead>
                    <TableHead className="w-28">status</TableHead>
                    <TableHead className="w-40 text-right">uploaded</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {metas.map((m) => (
                    <TableRow key={m.id} className="cursor-pointer">
                      <TableCell className="max-w-64 truncate font-medium">
                        <Link
                          to={`/accounts/${account.slug}/statements/${m.id}`}
                          className="after:absolute after:inset-0 hover:underline"
                        >
                          {m.original_filename}
                        </Link>
                      </TableCell>
                      <TableCell className="text-muted-foreground">{period(m.period)}</TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {m.summary?.transactions ?? "—"}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {m.summary ? money(m.summary.closing_balance) : "—"}
                      </TableCell>
                      <TableCell>
                        <StatusChip status={m.status} />
                      </TableCell>
                      <TableCell className="text-right text-muted-foreground">
                        {dt(m.uploaded_at)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}
      </div>

      {/* recent runs */}
      {runs.length > 0 ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold">Recent distill runs</h2>
            <Link to="/runs" className="text-sm text-muted-foreground hover:text-foreground">
              all runs →
            </Link>
          </div>
          <Card>
            <CardContent className="p-0">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>started</TableHead>
                    <TableHead>mode</TableHead>
                    <TableHead>status</TableHead>
                    <TableHead className="text-right">duration</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runs.map((r) => (
                    <TableRow key={r.id} className="cursor-pointer">
                      <TableCell className="font-medium">
                        <Link to={`/runs/${r.id}`} className="after:absolute after:inset-0">
                          {dt(r.created_at)}
                        </Link>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {r.mode === "maintain" ? `maintain ${r.key ?? ""}` : "draft"}
                      </TableCell>
                      <TableCell>
                        <StatusChip status={r.status} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {r.duration ?? ""}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>
      ) : null}

      {/* danger zone — quiet by default; red lives on the button, not the shelf */}
      <div className="flex items-start justify-between gap-4 rounded-xl border p-4">
        <div>
          <div className="text-sm font-medium">Delete this account</div>
          <p className="text-xs text-muted-foreground">
            Removes the account and every stored statement PDF and ledger under it. Parsers
            in the registry are not touched.
          </p>
        </div>
        <ConfirmDialog
          title={`Delete ${account.display_name}?`}
          description="This permanently removes the account folder — original PDFs, ledgers, and parse history. This cannot be undone."
          confirmLabel="Delete account"
          onConfirm={() => {
            deleteAccount.mutate(account.slug)
            navigate("/")
          }}
          trigger={
            <Button variant="destructive" size="sm">
              <Trash2 data-icon="inline-start" /> Delete
            </Button>
          }
        />
      </div>
    </div>
  )
}
