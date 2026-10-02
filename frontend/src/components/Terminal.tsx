import { FitAddon } from '@xterm/addon-fit'
import { Terminal as XTerm } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { RotateCcw, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

type Props = { endpoint: string; dark: boolean; onClose: () => void }

/** Interactive bash (tmux) on the Colab runtime, bridged by the backend to /colab/tty. */
export function TerminalDrawer({ endpoint, dark, onClose }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const [session, setSession] = useState(0)
  const [state, setState] = useState<'connecting' | 'open' | 'closed'>('connecting')

  useEffect(() => {
    const term = new XTerm({
      fontFamily: "ui-monospace, 'Cascadia Code', Consolas, monospace",
      fontSize: 13,
      cursorBlink: true,
      theme: dark
        ? { background: '#0d1014', foreground: '#e3e7ec' }
        : { background: '#fbfcfd', foreground: '#1c2128', cursor: '#1c2128', selectionBackground: '#c8d6e5' },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(host.current!)
    fit.fit()

    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(`${proto}://${location.host}/api/runtimes/${encodeURIComponent(endpoint)}/terminal`)
    const sendSize = () => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ cols: term.cols, rows: term.rows }))
    }
    setState('connecting')
    ws.onopen = () => {
      setState('open')
      sendSize()
      term.focus()
    }
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (typeof msg.data === 'string') term.write(msg.data)
      } catch {
        /* ignore non-JSON frames */
      }
    }
    ws.onclose = () => {
      setState('closed')
      term.write('\r\n\x1b[90m[session terminée]\x1b[0m\r\n')
    }
    const input = term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ data }))
    })
    const resize = term.onResize(sendSize)
    const observer = new ResizeObserver(() => {
      try {
        fit.fit()
      } catch {
        /* element hidden */
      }
    })
    observer.observe(host.current!)

    return () => {
      observer.disconnect()
      input.dispose()
      resize.dispose()
      ws.close()
      term.dispose()
    }
  }, [endpoint, dark, session])

  return (
    <div className="terminal-drawer">
      <div className="terminal-head">
        <span>Terminal du runtime</span>
        <span className={`status-pill ${state === 'open' ? 'ok' : state === 'connecting' ? 'pending' : 'off'}`}>
          {state === 'open' ? 'connecté' : state === 'connecting' ? 'connexion…' : 'fermé'}
        </span>
        <div className="spacer" />
        <button className="icon" title="Nouvelle session" onClick={() => setSession((s) => s + 1)}>
          <RotateCcw size={14} />
        </button>
        <button className="icon" title="Fermer" onClick={onClose}>
          <X size={14} />
        </button>
      </div>
      <div className="terminal-host" ref={host} />
    </div>
  )
}
