import { useRef, useState } from "react"
import { FileUp, Loader2 } from "lucide-react"
import { cn } from "cn"

export function Dropzone({
  onFile,
  busy,
  hint,
}: {
  onFile: (file: File) => void
  busy: boolean
  hint?: string
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label="Upload statement PDF"
      onClick={() => !busy && inputRef.current?.click()}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !busy) {
          e.preventDefault()
          inputRef.current?.click()
        }
      }}
      onDragOver={(e) => {
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setOver(false)
        const file = e.dataTransfer.files?.[0]
        if (file && !busy) onFile(file)
      }}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors",
        over
          ? "border-primary bg-primary/5"
          : "border-border text-muted-foreground hover:border-foreground/25 hover:bg-muted/50",
        busy && "pointer-events-none opacity-60",
      )}
    >
      <input
        ref={inputRef}
        type="file"
        accept="application/pdf"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0]
          if (file) onFile(file)
          e.target.value = "" // re-uploading the same file must still fire change
        }}
      />
      {busy ? (
        <Loader2 className="size-7 animate-spin text-primary" />
      ) : (
        <FileUp className="size-7" />
      )}
      <div className="text-sm font-medium">
        {busy ? "Parsing…" : "Drop a statement PDF, or click to browse"}
      </div>
      <div className="text-xs text-muted-foreground">
        {hint ?? "The original PDF is stored durably before anything is parsed."}
      </div>
    </div>
  )
}
