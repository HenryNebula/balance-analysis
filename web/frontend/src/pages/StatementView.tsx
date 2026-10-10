import { useMemo, useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { AttemptsTable } from "@/components/OutcomeCard"
import { ProbeDialog } from "@/components/ProbeDialog"
import { LaunchDialog } from "@/components/LaunchDialog"
import { ConfirmDialog } from "@/components/ConfirmDialog"
import { StatusChip } from "@/components/StatusChip"
import { ChevronLeft, ChevronRight, Download, FlaskConical, RefreshCw, ScanSearch, ShieldCheck, ShieldX, Trash2 } from "lucide-react"
import { cn } from "cn"
import { useAccount, useDeleteStatement, useReparse, useStatement } from "@/lib/hooks"
import { money, period, signed, size } from "@/lib/format"
import type { Attempt, ProvPage } from "@/lib/types"

// The original PDF with the parser's provenance overlay: every anchor and
// transaction the parser adopted is highlighted where it was read. Bands
// arrive in CSS px at raster scale 2.0 — positioning them by percentage of
// the raster's natural size keeps them aligned at any rendered width.
function PdfViewer({ slug, sid, pages, total }: { slug: string; sid: string; pages: ProvPage[]; total: number }) {
  const [page, setPage] = useState(pages[0]?.page ?? 0)
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  const bands = pages.find((p) => p.page === page)?.bands ?? []
  const byPage = new Map(pages.map((p) => [p.page, p]))

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          {bands.length} highlight{bands.length === 1 ? "" : "s"} — anchors and transactions as
          the parser read them
        </div>
        {total > 1 ? (
          <div className="flex items-center gap-1">
            <Button size="icon-xs" variant="outline" disabled={page === 0} onClick={() => setPage(page - 1)}>
              <ChevronLeft />
            </Button>
            <span className="px-1 text-xs tabular-nums text-muted-foreground">
              page {page + 1} / {total}
            </span>
            <Button size="icon-xs" variant="outline" disabled={page >= total - 1} onClick={() => setPage(page + 1)}>
              <ChevronRight />
            </Button>
          </div>
        ) : null}
      </div>
      <div className="relative overflow-hidden rounded-lg border bg-muted/40">
        <img
          src={`/accounts/${slug}/statements/${sid}/pages/${page}.png`}
          alt={`statement page ${page + 1}`}
          className="block w-full select-none"
          onLoad={(e) =>
            setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
          }
        />
        {natural
          ? bands.map((b, i) => {
              const label = b.kind === "anchor" ? b.label : `#${b.index + 1} ${b.label}`
              return (
                <div
                  key={i}
                  title={label}
                  className={cn(
                    "absolute rounded-[2px] transition-colors hover:z-10",
                    b.kind === "anchor"
                      ? "border border-emerald-600/70 bg-emerald-500/20"
                      : "border border-blue-600/60 bg-blue-500/15",
                  )}
                  style={{
                    left: `${(b.left / natural.w) * 100}%`,
                    top: `${(b.top / natural.h) * 100}%`,
                    width: `${(b.width / natural.w) * 100}%`,
                    height: `${(b.height / natural.h) * 100}%`,
                  }}
                >
                  <span
                    className={cn(
                      "absolute -top-4 left-0 hidden max-w-56 truncate rounded px-1 text-[10px] leading-4 text-white hover:inline-block",
                      b.kind === "anchor" ? "bg-emerald-600" : "bg-blue-600",
                    )}
                  >
                    {label}
                  </span>
                </div>
              )
            })
          : null}
      </div>
      {pages.length > 0 && !byPage.has(page) ? (
        <p className="text-xs text-muted-foreground">
          No highlights on this page (the parser read from {pages.length} of {total}).
        </p>
      ) : null}
    </div>
  )
}

export default function StatementView() {
  const { slug, sid } = useParams()
  const navigate = useNavigate()
  const { data, isLoading } = useStatement(slug, sid)
  const { data: accountData } = useAccount(slug)
  const reparse = useReparse(slug ?? "", sid ?? "")
  const del = useDeleteStatement(slug ?? "", sid ?? "")
  const [launchOpen, setLaunchOpen] = useState(false)

  const lastFailed = useMemo(() => {
    const h = data?.meta.history ?? []
    return [...h].reverse().find((e: any) => e.outcome === "failed")
  }, [data])

  if (isLoading || !data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-96" />
        <Skeleton className="h-40 rounded-xl" />
        <Skeleton className="h-96 rounded-xl" />
      </div>
    )
  }

  const { meta, ledger, verified, prov_pages } = data
  const parsed = meta.status === "parsed" && ledger

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <Link
          to={`/accounts/${slug}`}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          ← {accountData?.account.display_name ?? slug}
        </Link>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <h1 className="min-w-0 break-words text-2xl font-semibold tracking-tight">
              {meta.original_filename}
            </h1>
            <StatusChip status={meta.status} className="shrink-0" />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {parsed ? (
              <>
                <Button variant="outline" size="sm" asChild>
                  <a href={`/accounts/${slug}/statements/${sid}/ledger.json`} download>
                    <Download data-icon="inline-start" /> JSON
                  </a>
                </Button>
                <Button variant="outline" size="sm" asChild>
                  <a href={`/accounts/${slug}/statements/${sid}/ledger.csv`} download>
                    <Download data-icon="inline-start" /> CSV
                  </a>
                </Button>
              </>
            ) : null}
            <ProbeDialog
              slug={slug!}
              sid={sid}
              trigger={
                <Button variant="outline" size="sm">
                  <ScanSearch data-icon="inline-start" /> Probe
                </Button>
              }
            />
            <Button variant="outline" size="sm" disabled={reparse.isPending} onClick={() => reparse.mutate()}>
              <RefreshCw data-icon="inline-start" />
              {reparse.isPending ? "Parsing…" : "Re-parse"}
            </Button>
            <LaunchDialog
              slug={slug!}
              statements={[meta]}
              defaultIds={[meta.id]}
              pinnedKey={accountData?.account.registry_key ?? null}
              model={data.llm_model}
              open={launchOpen}
              onOpenChange={setLaunchOpen}
              trigger={
                <Button size="sm" variant={parsed ? "outline" : "default"}>
                  <FlaskConical data-icon="inline-start" /> Distill
                </Button>
              }
            />
            <ConfirmDialog
              title="Delete this statement?"
              description={`Removes ${meta.original_filename} — the original PDF, its ledger, and parse history. This cannot be undone.`}
              confirmLabel="Delete statement"
              onConfirm={() => {
                del.mutate(undefined, {
                  onSuccess: () => navigate(`/accounts/${slug}`),
                })
              }}
              trigger={
                <Button variant="ghost" size="sm" aria-label="Delete statement">
                  <Trash2 data-icon="inline-start" /> Delete
                </Button>
              }
            />
          </div>
        </div>
        <p className="text-sm text-muted-foreground">
          {period(meta.period)} · stored {meta.original_filename} ({size(meta.size_bytes)})
        </p>
      </div>

      {parsed ? (
        <>
          {verified ? (
            <div className="flex items-center gap-2 rounded-lg border border-emerald-600/30 bg-emerald-600/5 px-4 py-3 text-sm">
              <ShieldCheck className="size-4 text-emerald-600" />
              <span>
                <strong className="font-medium text-emerald-700 dark:text-emerald-400">RECONCILED</strong>{" "}
                — every transaction checks out against the running balance; the closing balance
                is the real one.
              </span>
            </div>
          ) : (
            <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm">
              <ShieldX className="size-4 text-destructive" />
              <span>
                <strong className="font-medium text-destructive">Reconcile failed</strong> — the
                transactions don't add up to the closing balance. Treat this ledger as suspect.
              </span>
            </div>
          )}
          <div className="grid gap-3 sm:grid-cols-3">
            <Card>
              <CardContent className="py-4">
                <div className="text-xs text-muted-foreground">opening</div>
                <div className="text-xl font-semibold tabular-nums">{money(ledger.opening_balance)}</div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="py-4">
                <div className="text-xs text-muted-foreground">net movement</div>
                <div className="text-xl font-semibold tabular-nums">
                  {signed(ledger.closing_balance - ledger.opening_balance)}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardContent className="py-4">
                <div className="text-xs text-muted-foreground">closing</div>
                <div className="text-xl font-semibold tabular-nums">{money(ledger.closing_balance)}</div>
              </CardContent>
            </Card>
          </div>
          <div className="grid gap-6 xl:grid-cols-2">
            <div className="min-w-0 space-y-2">
              <div className="text-sm text-muted-foreground">
                transactions · {ledger.transactions.length}
              </div>
              <Card>
                <CardContent className="p-0">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>date</TableHead>
                        <TableHead>description</TableHead>
                        <TableHead className="text-right">amount</TableHead>
                        <TableHead className="text-right">balance</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {ledger.transactions.map((t, i) => (
                        <TableRow key={i} className={cn(t.line != null && "hover:bg-blue-500/5")}>
                          <TableCell className="whitespace-nowrap tabular-nums text-muted-foreground">{t.date}</TableCell>
                          <TableCell>{t.description}</TableCell>
                          <TableCell className="text-right font-medium tabular-nums">
                            <span className={t.amount >= 0 ? "text-emerald-700 dark:text-emerald-400" : undefined}>
                              {signed(t.amount)}
                            </span>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{money(t.balance)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            </div>
            <div className="min-w-0">
              <PdfViewer slug={slug!} sid={sid!} pages={prov_pages} total={prov_pages.length ? Math.max(...prov_pages.map((p) => p.page)) + 1 : 1} />
            </div>
          </div>
        </>
      ) : (
        <Card>
          <CardContent className="space-y-4">
            {meta.status === "failed" ? (
              <>
                <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 px-4 py-3 text-sm">
                  ◇{" "}
                  {data.meta.history?.some((h: any) => h.outcome === "parsed") ? (
                    <>This statement parsed before but the latest attempt failed — the PDF is safe, and the diagnostics below show what changed.</>
                  ) : (
                    <>No parser for this bank yet — that's the normal first step. The PDF is stored durably; nothing is lost by failing.</>
                  )}
                </div>
                <details className="text-sm">
                  <summary className="cursor-pointer text-muted-foreground select-none">
                    diagnostics (per-parser attempts)
                  </summary>
                  <div className="mt-2 rounded-md border bg-muted/40">
                    <AttemptsTable attempts={(lastFailed?.attempts as Attempt[]) ?? []} />
                    {lastFailed?.note ? (
                      <div className="border-t px-3 py-2 text-xs text-muted-foreground">
                        {String(lastFailed.note)}
                      </div>
                    ) : null}
                  </div>
                </details>
                <Button onClick={() => setLaunchOpen(true)}>
                  {data.meta.history?.some((h: any) => h.outcome === "parsed")
                    ? "Distill an update"
                    : "Register this bank (distill a parser)"}
                </Button>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                Stored, not yet parsed. Re-parse to try again, or probe to see what every
                parser would do.
              </p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
