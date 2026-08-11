import { useEffect, useRef, useState } from 'react'
import type { QueueFiles } from '../api/types'

export type SocketState = 'connecting' | 'open' | 'reconnecting'

interface SnapshotMessage {
  type: 'snapshot'
  queues: QueueSnapshotMessage[]
}

interface QueueSnapshotMessage {
  type: 'queue_snapshot'
  queue_id: number
  queue_name: string
  nodes: QueueFiles['nodes']
  scanned_at: string | null
}

interface ScanErrorMessage {
  type: 'scan_error'
  queue_id: number
  queue_name: string
  message: string
}

type WsMessage = SnapshotMessage | QueueSnapshotMessage | ScanErrorMessage

const RECONNECT_DELAY_MS = 3000

function toQueueFiles(msg: QueueSnapshotMessage, error: string | null): QueueFiles {
  return {
    queue_id: msg.queue_id,
    queue_name: msg.queue_name,
    scanned_at: msg.scanned_at,
    error,
    nodes: msg.nodes,
  }
}

/**
 * DESIGN.md §2/§9: one WebSocket, a full snapshot on connect, deltas thereafter. Here
 * "delta" is the fresh reconciled state for one queue (`core/engine.py` publishes one per
 * finished scan) — this hook merges each into a `queue_id`-keyed map, so a queue that
 * hasn't rescanned yet keeps showing its last-known state rather than disappearing.
 *
 * Reconnects automatically on close with a visible `state` — DESIGN.md §7 (Files page)
 * calls for "a visible reconnect state" rather than a live view that silently goes stale.
 */
export function useFilesSocket(): { queues: QueueFiles[]; state: SocketState } {
  const [queuesById, setQueuesById] = useState<Record<number, QueueFiles>>({})
  const [state, setState] = useState<SocketState>('connecting')

  // Only used inside the effect's closures; a ref avoids re-running the effect on every
  // message (which would tear down and reopen the socket).
  const queuesRef = useRef(queuesById)
  queuesRef.current = queuesById

  useEffect(() => {
    let cancelled = false
    let ws: WebSocket | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined

    const connect = () => {
      if (cancelled) return
      setState((prev) => (prev === 'open' ? 'reconnecting' : 'connecting'))

      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${protocol}//${window.location.host}/api/ws`)

      ws.onopen = () => {
        if (cancelled) return
        setState('open')
      }

      ws.onmessage = (event) => {
        if (cancelled) return
        let msg: WsMessage
        try {
          msg = JSON.parse(event.data as string) as WsMessage
        } catch {
          return
        }

        if (msg.type === 'snapshot') {
          const next: Record<number, QueueFiles> = {}
          for (const q of msg.queues) next[q.queue_id] = toQueueFiles(q, null)
          setQueuesById(next)
        } else if (msg.type === 'queue_snapshot') {
          setQueuesById((prev) => ({ ...prev, [msg.queue_id]: toQueueFiles(msg, null) }))
        } else if (msg.type === 'scan_error') {
          setQueuesById((prev) => {
            const existing = prev[msg.queue_id]
            return {
              ...prev,
              [msg.queue_id]: existing
                ? { ...existing, error: msg.message }
                : { queue_id: msg.queue_id, queue_name: msg.queue_name, scanned_at: null, error: msg.message, nodes: [] },
            }
          })
        }
      }

      ws.onclose = () => {
        if (cancelled) return
        setState('reconnecting')
        retryTimer = setTimeout(connect, RECONNECT_DELAY_MS)
      }

      ws.onerror = () => {
        ws?.close()
      }
    }

    connect()

    return () => {
      cancelled = true
      if (retryTimer) clearTimeout(retryTimer)
      ws?.close()
    }
  }, [])

  const queues = Object.values(queuesById).sort((a, b) => a.queue_id - b.queue_id)
  return { queues, state }
}
