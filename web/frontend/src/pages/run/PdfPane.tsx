import { useEffect, useLayoutEffect, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { ChevronLeft, ChevronRight } from "lucide-react"
import type { RunEvent } from "@/lib/types"

const SCALE = 2.0 // raster px per pdfplumber point (web/pages.py)

// The statement under the microscope: zoomable page raster with the probe's
// selection drawn where it looked. Zoom presets plus fit-to-pane (the
// amounts are exactly what the gate verifies — clipping them is not OK).
export function PdfPane({
  slug,
  sid,
  pages,
  selection,
}: {
  slug: string
  sid: string | null
  pages: number
  selection?: RunEvent
}) {
  const [page, setPage] = useState(selection?.page ?? 0)
  const [zoomMode, setZoomMode] = useState<"fit" | number>("fit")
  const [fitted, setFitted] = useState(1)
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null)
  const viewportRef = useRef<HTMLDivElement>(null)

  const zoom = zoomMode === "fit" ? fitted : zoomMode

  useLayoutEffect(() => {
    const el = viewportRef.current
    if (!el || !natural) return
    const fit = () => setFitted(Math.min(1, (el.clientWidth - 24) / natural.w))
    fit()
    const observer = new ResizeObserver(fit)
    observer.observe(el)
    return () => observer.disconnect()
  }, [natural])

  // follow the selection across pages (auto-follow keeps the probe in view)
  useEffect(() => {
    if (selection && selection.page !== page) setPage(selection.page ?? 0)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection])

  if (!sid) {
    return (
      <div className="flex flex-1 items-center justify-center bg-muted/30 text-sm text-muted-foreground">
        No statement attached to this run (attach mode) — the trajectory still streams on
        the right.
      </div>
    )
  }

  const rect = selection?.rect ?? []
  const bandVisible =
    selection && selection.page === page && rect.length === 4 && natural
  const label = selection?.label
    ? String(selection.label).slice(0, 44)
    : selection
      ? `${selection.line_ids?.length ?? "?"} lines`
      : null

  return (
    <div className="flex min-w-0 flex-1 flex-col">
      <div className="flex items-center justify-between gap-3 border-b bg-card px-3 py-1.5">
        <div className="flex items-center gap-1">
          {pages > 1 ? (
            <>
              <Button size="icon-xs" variant="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>
                <ChevronLeft />
              </Button>
              <span className="min-w-16 text-center text-xs tabular-nums text-muted-foreground">
                page {page + 1}/{pages}
              </span>
              <Button size="icon-xs" variant="ghost" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>
                <ChevronRight />
              </Button>
            </>
          ) : null}
        </div>
        <div className="flex items-center gap-0.5">
          {([0.5, 0.75, 1] as const).map((z) => (
            <Button
              key={z}
              size="xs"
              variant={zoomMode === z ? "secondary" : "ghost"}
              className="tabular-nums"
              onClick={() => setZoomMode(z)}
            >
              {Math.round(z * 100)}%
            </Button>
          ))}
          <Button
            size="xs"
            variant={zoomMode === "fit" ? "secondary" : "ghost"}
            onClick={() => setZoomMode("fit")}
          >
            fit
          </Button>
        </div>
      </div>
      <div ref={viewportRef} className="pdf-viewport flex-1 overflow-auto bg-muted/40 p-3">
        <div style={natural ? { width: natural.w * zoom, height: natural.h * zoom } : undefined}>
          <div
            className="relative"
            style={
              natural
                ? { width: natural.w, height: natural.h, transform: `scale(${zoom})`, transformOrigin: "top left" }
                : undefined
            }
          >
            <img
              src={`/accounts/${slug}/statements/${sid}/pages/${page}.png`}
              alt={`statement page ${page + 1}`}
              className="block shadow-md"
              onLoad={(e) =>
                setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
              }
            />
            {bandVisible ? (
              <div
                className="absolute rounded-[2px] border-2 border-amber-500 bg-amber-400/20"
                title={`what the probe looked at: ${label}`}
                style={{
                  left: rect[0] * SCALE,
                  top: rect[1] * SCALE,
                  width: (rect[2] - rect[0]) * SCALE,
                  height: (rect[3] - rect[1]) * SCALE,
                }}
              >
                <span
                  className="absolute -top-5 left-0 max-w-56 truncate rounded bg-amber-500 px-1 text-white"
                  style={{ fontSize: Math.round(11 / zoom) }}
                >
                  {label}
                </span>
              </div>
            ) : null}
          </div>
        </div>
      </div>
      <div className="border-t bg-card px-3 py-1.5 text-xs text-muted-foreground">
        {selection
          ? selection.page === page
            ? `selection: r${selection.round} · ${label}`
            : `selection: round ${selection.round} is on page ${(selection.page ?? 0) + 1}`
          : "selection: —"}
      </div>
    </div>
  )
}
