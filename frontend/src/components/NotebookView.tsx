import {
  Download,
  Eraser,
  ExternalLink,
  FastForward,
  Loader2,
  Plug,
  Power,
  RotateCcw,
  Settings2,
  Square,
  SquareTerminal,
  Unplug,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ACCELERATOR_LABELS, api, type Cell, type Notebook, type Runtime } from '../api'
import { useEvents } from '../events'
import { CellView, type CellRunState } from './CellView'
import { SettingsDialog } from './SettingsDialog'
import { TerminalDrawer } from './Terminal'

type Props = {
  notebookId: string
  runtimes: Runtime[]
  dark: boolean
  onError: (message: string) => void
  onRenamed: (newId: string) => void
  onNotebookLoaded: (nb: Notebook | null) => void
}

const SAVE_DELAY = 500

export function NotebookView({ notebookId, runtimes, dark, onError, onRenamed, onNotebookLoaded }: Props) {
  const [nb, setNb] = useState<Notebook | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [focus, setFocus] = useState<{ id: string; token: number } | null>(null)
  const [runStates, setRunStates] = useState<Record<string, CellRunState>>({})
  const [phase, setPhase] = useState<string | null>(null)
  const [showSettings, setShowSettings] = useState(false)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [disconnectMenu, setDisconnectMenu] = useState(false)
  const [showTerminal, setShowTerminal] = useState(false)
  const pending = useRef(new Map<string, { timer: number; source: string }>())

  const guard = useCallback(
    async <T,>(p: Promise<T>): Promise<T | undefined> => {
      try {
        return await p
      } catch (e) {
        onError((e as Error).message)
        return undefined
      }
    },
    [onError],
  )

  const reload = useCallback(async () => {
    try {
      const fresh = await api.notebook(notebookId)
      // Keep edits that have not reached the server yet.
      fresh.cells = fresh.cells.map((c) => {
        const p = pending.current.get(c.id)
        return p ? { ...c, source: p.source } : c
      })
      setNb(fresh)
      onNotebookLoaded(fresh)
      if (fresh.running_cell) setRunStates((s) => ({ ...s, [fresh.running_cell!]: 'running' }))
    } catch (e) {
      onError((e as Error).message)
    }
  }, [notebookId, onError, onNotebookLoaded])

  useEffect(() => {
    setNb(null)
    setRunStates({})
    setPhase(null)
    setSelected(null)
    reload()
  }, [notebookId, reload])

  // Flush pending saves when leaving the notebook.
  useEffect(() => {
    const map = pending.current
    return () => {
      map.forEach((p, cellId) => {
        clearTimeout(p.timer)
        api.patchCell(notebookId, cellId, { source: p.source }).catch(() => {})
      })
      map.clear()
    }
  }, [notebookId])

  const updateCell = (cellId: string, patch: Partial<Cell>) =>
    setNb((cur) => (cur ? { ...cur, cells: cur.cells.map((c) => (c.id === cellId ? { ...c, ...patch } : c)) } : cur))

  useEvents((e) => {
    if (e.notebook_id !== undefined && e.notebook_id !== notebookId) return
    switch (e.type) {
      case 'notebook_changed':
        if (e.source !== 'ui' && e.source !== 'execution') reload()
        break
      case 'cell_output':
        updateCell(e.cell_id, { outputs: e.outputs })
        break
      case 'cell_status':
        if (e.status === 'queued' || e.status === 'running') {
          setRunStates((s) => ({ ...s, [e.cell_id]: e.status }))
        } else {
          setRunStates((s) => {
            const { [e.cell_id]: _, ...rest } = s
            return rest
          })
          if (e.outputs) updateCell(e.cell_id, { outputs: e.outputs, execution_count: e.execution_count })
        }
        break
      case 'runtime_status':
        if (e.notebook_id === notebookId) {
          if (e.status === 'assigning' || e.status === 'setup') setPhase(e.status)
          else {
            setPhase(null)
            reload()
          }
        } else if (e.status === 'stopped' && nb?.runtime?.endpoint === e.endpoint) {
          reload()
        }
        break
      case 'error':
        if (e.notebook_id === notebookId) {
          setPhase(null)
          setRunStates({})
          reload()
        }
        break
    }
  })

  if (!nb) {
    return (
      <div className="empty-state">
        <Loader2 className="spin" />
      </div>
    )
  }

  const scheduleSave = (cellId: string, source: string) => {
    updateCell(cellId, { source })
    const prev = pending.current.get(cellId)
    if (prev) clearTimeout(prev.timer)
    const timer = window.setTimeout(() => flush(cellId), SAVE_DELAY)
    pending.current.set(cellId, { timer, source })
  }

  const flush = async (cellId: string) => {
    const p = pending.current.get(cellId)
    if (!p) return
    clearTimeout(p.timer)
    pending.current.delete(cellId)
    await guard(api.patchCell(notebookId, cellId, { source: p.source }))
  }

  const focusCell = (cellId: string) => {
    setSelected(cellId)
    setFocus({ id: cellId, token: Date.now() })
  }

  const flushAll = () => Promise.all([...pending.current.keys()].map(flush))

  const runCell = async (cellId: string, advance: boolean) => {
    await flush(cellId)
    const cell = nb.cells.find((c) => c.id === cellId)
    if (cell?.cell_type === 'code') {
      setRunStates((s) => ({ ...s, [cellId]: 'queued' }))
      await guard(api.runCell(notebookId, cellId))
    }
    if (advance) {
      const idx = nb.cells.findIndex((c) => c.id === cellId)
      if (idx === nb.cells.length - 1) {
        const res = await guard(api.addCell(notebookId, 'code'))
        await reload()
        if (res) focusCell(res.cell_id)
      } else {
        focusCell(nb.cells[idx + 1].id)
      }
    }
  }

  const structural = async (p: Promise<unknown>) => {
    await flushAll()
    await guard(p)
    await reload()
  }

  const addCell = async (type: 'code' | 'markdown') => {
    const idx = selected ? nb.cells.findIndex((c) => c.id === selected) + 1 : nb.cells.length
    await flushAll()
    const res = await guard(api.addCell(notebookId, type, idx))
    await reload()
    if (res) focusCell(res.cell_id)
  }

  const runtime = nb.runtime
  const busy = Object.values(runStates).some(Boolean)
  const freeRuntimes = runtimes.filter((r) => !r.notebook_id)
  const accelMismatch = runtime && runtime.accelerator !== nb.settings.accelerator

  let status: { label: string; cls: string }
  if (phase === 'assigning') status = { label: `Allocation ${ACCELERATOR_LABELS[nb.settings.accelerator]}…`, cls: 'pending' }
  else if (phase === 'setup') status = { label: 'Installation / setup…', cls: 'pending' }
  else if (runtime)
    status = {
      label: `${ACCELERATOR_LABELS[runtime.accelerator] ?? runtime.accelerator}${runtime.high_mem ? ' · High-RAM' : ''} · ${busy ? 'occupé' : 'prêt'}`,
      cls: busy ? 'busy' : 'ok',
    }
  else status = { label: 'Non connecté', cls: 'off' }

  const submitRename = async () => {
    const name = renaming?.trim()
    setRenaming(null)
    if (!name || name === nb.id) return
    await flushAll()
    const res = await guard(api.renameNotebook(nb.id, name))
    if (res) onRenamed(res.id)
  }

  return (
    <div className="notebook">
      <div className="nb-toolbar">
        <div className="nb-title">
          {renaming !== null ? (
            <input
              autoFocus
              value={renaming}
              onChange={(e) => setRenaming(e.target.value)}
              onBlur={submitRename}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submitRename()
                if (e.key === 'Escape') setRenaming(null)
              }}
            />
          ) : (
            <h1 title="Cliquer pour renommer" onClick={() => setRenaming(nb.id)}>
              {nb.name}
            </h1>
          )}
          <select
            className="accel-select"
            value={nb.settings.accelerator}
            onChange={(e) => structural(api.updateSettings(nb.id, { accelerator: e.target.value }))}
            title="Accélérateur utilisé au prochain démarrage du runtime"
          >
            {Object.entries(ACCELERATOR_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
          <button className="icon" title="Configuration du runtime" onClick={() => setShowSettings(true)}>
            <Settings2 size={16} />
          </button>
          <span className={`status-pill ${status.cls}`}>
            {status.cls === 'pending' && <Loader2 size={12} className="spin" />}
            {status.label}
          </span>
          {accelMismatch && <span className="hint">↻ reconnecter pour passer en {ACCELERATOR_LABELS[nb.settings.accelerator]}</span>}
        </div>

        <div className="nb-actions">
          {!runtime ? (
            <>
              <button className="primary" disabled={!!phase} onClick={() => guard(api.connect(nb.id)).then(() => setPhase('assigning'))}>
                <Plug size={15} /> Connecter
              </button>
              {freeRuntimes.length > 0 && (
                <select
                  className="attach-select"
                  value=""
                  onChange={(e) => e.target.value && structural(api.attach(nb.id, e.target.value))}
                  title="Utiliser un runtime déjà alloué"
                >
                  <option value="">Attacher…</option>
                  {freeRuntimes.map((r) => (
                    <option key={r.endpoint} value={r.endpoint}>
                      {ACCELERATOR_LABELS[r.accelerator] ?? r.accelerator} · {r.endpoint.slice(0, 18)}
                    </option>
                  ))}
                </select>
              )}
            </>
          ) : (
            <div className="menu-wrap">
              <button onClick={() => setDisconnectMenu((v) => !v)} title="Déconnecter">
                <Unplug size={15} /> Déconnecter
              </button>
              {disconnectMenu && (
                <div className="menu" onMouseLeave={() => setDisconnectMenu(false)}>
                  <button
                    onClick={() => {
                      setDisconnectMenu(false)
                      structural(api.disconnect(nb.id, true))
                    }}
                  >
                    <Power size={14} /> Libérer le runtime (arrête la VM)
                  </button>
                  <button
                    onClick={() => {
                      setDisconnectMenu(false)
                      structural(api.disconnect(nb.id, false))
                    }}
                  >
                    <Unplug size={14} /> Détacher seulement (VM conservée)
                  </button>
                </div>
              )}
            </div>
          )}
          <button
            title="Tout exécuter"
            onClick={async () => {
              await flushAll()
              guard(api.runAll(nb.id))
            }}
          >
            <FastForward size={15} /> Tout exécuter
          </button>
          <button className="icon" title="Interrompre" disabled={!runtime} onClick={() => guard(api.interrupt(nb.id))}>
            <Square size={15} />
          </button>
          <button className="icon" title="Redémarrer le kernel" disabled={!runtime} onClick={() => guard(api.restart(nb.id)).then(() => setPhase('setup'))}>
            <RotateCcw size={15} />
          </button>
          <button className="icon" title="Effacer les sorties" onClick={() => structural(api.clearOutputs(nb.id))}>
            <Eraser size={15} />
          </button>
          <a className="button icon" title="Télécharger le .ipynb" href={api.downloadNotebookUrl(nb.id)} onClick={() => flushAll()}>
            <Download size={15} />
          </a>
          <button
            className={`icon ${showTerminal ? 'toggled' : ''}`}
            title="Terminal du runtime"
            disabled={!runtime}
            onClick={() => setShowTerminal((v) => !v)}
          >
            <SquareTerminal size={15} />
          </button>
          {runtime && (
            <a className="button icon" title="Ouvrir le runtime dans Colab (navigateur)" href={runtime.colab_url} target="_blank" rel="noreferrer">
              <ExternalLink size={15} />
            </a>
          )}
        </div>
      </div>

      <div className="cells">
        {nb.cells.map((cell, i) => (
          <CellView
            key={cell.id}
            cell={cell}
            index={i}
            total={nb.cells.length}
            selected={selected === cell.id}
            focusToken={focus?.id === cell.id ? focus.token : 0}
            runState={runStates[cell.id]}
            dark={dark}
            onSelect={() => setSelected(cell.id)}
            onChange={(src) => scheduleSave(cell.id, src)}
            onRun={(advance) => runCell(cell.id, advance)}
            onDelete={() => structural(api.deleteCell(nb.id, cell.id))}
            onMove={(d) => structural(api.moveCell(nb.id, cell.id, i + d))}
            onToggleType={() =>
              structural(api.patchCell(nb.id, cell.id, { cell_type: cell.cell_type === 'code' ? 'markdown' : 'code' }))
            }
          />
        ))}
        <div className="add-cells">
          <button onClick={() => addCell('code')}>+ Code</button>
          <button onClick={() => addCell('markdown')}>+ Texte</button>
        </div>
      </div>

      {showTerminal && runtime && (
        <TerminalDrawer endpoint={runtime.endpoint} dark={dark} onClose={() => setShowTerminal(false)} />
      )}

      {showSettings && (
        <SettingsDialog
          notebook={nb}
          dark={dark}
          onClose={() => setShowSettings(false)}
          onSave={async (settings) => {
            const ok = await guard(api.updateSettings(nb.id, settings))
            if (ok) {
              setShowSettings(false)
              reload()
            }
          }}
        />
      )}
    </div>
  )
}
