import { useEffect, useRef, useState } from 'react'
import type { AppEvent } from './api'

type Listener = (event: AppEvent) => void

const listeners = new Set<Listener>()
let socket: WebSocket | null = null
let connected = false
const statusListeners = new Set<(ok: boolean) => void>()

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${proto}://${location.host}/api/events`)
  socket.onopen = () => setConnected(true)
  socket.onmessage = (msg) => {
    const event = JSON.parse(msg.data) as AppEvent
    listeners.forEach((l) => l(event))
  }
  socket.onclose = () => {
    setConnected(false)
    setTimeout(connect, 1500)
  }
}

function setConnected(value: boolean) {
  connected = value
  statusListeners.forEach((l) => l(value))
}

/** Subscribe to server events. The handler may change between renders. */
export function useEvents(handler: Listener) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    if (!socket) connect()
    const l: Listener = (e) => ref.current(e)
    listeners.add(l)
    return () => {
      listeners.delete(l)
    }
  }, [])
}

export function useEventsConnected() {
  const [ok, setOk] = useState(connected)
  useEffect(() => {
    statusListeners.add(setOk)
    return () => {
      statusListeners.delete(setOk)
    }
  }, [])
  return ok
}
