import { useEffect, useReducer, useState } from "react"
import { initialRunState, runReducer } from "./store"

// EventSource over /runs/{id}/events. The server numbers every event (id:)
// so a dropped connection resumes where it left off via Last-Event-ID — the
// browser reconnects on its own, no polling fallback needed.
export function useRunEvents(id: string | undefined) {
  const [state, dispatch] = useReducer(runReducer, initialRunState)
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    if (!id) return
    const source = new EventSource(`/runs/${id}/events`)
    source.onopen = () => setConnected(true)
    source.onmessage = (message) => {
      try {
        dispatch(JSON.parse(message.data))
      } catch {
        /* skip a torn event */
      }
    }
    source.addEventListener("run_status", (message) => {
      try {
        dispatch({ type: "run_status", status: JSON.parse((message as MessageEvent).data).status })
      } catch {
        /* skip */
      }
    })
    source.onerror = () => setConnected(false)
    return () => source.close()
  }, [id])

  return { state, connected }
}
