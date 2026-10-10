import { useEffect, useState } from "react"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { useRegistry } from "@/lib/hooks"

// Parsers are shared per bank: one parser serves every account of that bank,
// and pinning one account doesn't clone it. Unpinned = detection mode (every
// parser is tried, the first that verifies wins).
export function PinDialog({
  current,
  onPin,
  trigger,
}: {
  current: string | null
  onPin: (key: string) => void
  trigger: React.ReactNode
}) {
  const { data: rows } = useRegistry()
  const [open, setOpen] = useState(false)
  const [key, setKey] = useState<string>(current ?? "")
  useEffect(() => setKey(current ?? ""), [current])

  const description = rows?.find((r) => r.key === key)?.description

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{current ? "Change parser pin" : "Pin a parser"}</DialogTitle>
          <DialogDescription>
            Parsers are shared per bank — one parser serves every account of that bank.
            Leave unpinned for detection mode: every registered parser is tried and the first
            that verifies arithmetically wins.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Select value={key} onValueChange={setKey}>
            <SelectTrigger className="w-full font-mono text-xs">
              <SelectValue placeholder="detection mode (no pin)" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="none">detection mode (no pin)</SelectItem>
              {(rows ?? []).map((r) => (
                <SelectItem key={r.key} value={r.key} className="font-mono text-xs">
                  {r.key}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {description ? (
            <p className="text-xs text-muted-foreground">{description}</p>
          ) : null}
        </div>
        <DialogFooter>
          <Button
            onClick={() => {
              onPin(key === "none" ? "" : key)
              setOpen(false)
            }}
          >
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
