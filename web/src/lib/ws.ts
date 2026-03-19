/**
 * React hook for subscribing to backup job progress via WebSocket.
 *
 * Usage:
 *   const { events, lastEvent, isConnected } = useJobWS(activeJobId)
 *
 * - Connect BEFORE triggering POST /api/jobs/{job_id}/run to catch JOB_START.
 * - Set jobId to null to disconnect.
 * - A ping is sent every 20 s to prevent proxy timeouts.
 */

import { useEffect, useRef, useState } from 'react'
import type { WsEvent } from '../types'

interface UseJobWSResult {
  events: WsEvent[]
  lastEvent: WsEvent | null
  isConnected: boolean
  clear: () => void
}

export function useJobWS(jobId: string | null): UseJobWSResult {
  const [events, setEvents] = useState<WsEvent[]>([])
  const [isConnected, setIsConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    if (!jobId) {
      wsRef.current?.close()
      return
    }

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${location.host}/ws/jobs/${jobId}`
    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      setIsConnected(true)
      ws.send('ping')
    }

    ws.onmessage = (e: MessageEvent) => {
      try {
        const ev: WsEvent = JSON.parse(e.data as string)
        setEvents(prev => [...prev, ev])
      } catch { /* ignore malformed frames */ }
    }

    ws.onerror = () => setIsConnected(false)
    ws.onclose = () => setIsConnected(false)

    // Keep-alive ping every 20 s
    pingRef.current = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send('ping')
    }, 20_000)

    return () => {
      clearInterval(pingRef.current ?? undefined)
      ws.close()
      setIsConnected(false)
    }
  }, [jobId])

  const clear = () => setEvents([])

  return {
    events,
    lastEvent: events[events.length - 1] ?? null,
    isConnected,
    clear,
  }
}
