import { markdown } from '@codemirror/lang-markdown'
import { python } from '@codemirror/lang-python'
import { Prec } from '@codemirror/state'
import { EditorView, keymap } from '@codemirror/view'
import CodeMirror, { type ReactCodeMirrorRef } from '@uiw/react-codemirror'
import { ArrowDown, ArrowUp, Code2, Loader2, Play, Trash2, Type } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Cell } from '../api'
import { OutputView, renderMarkdown } from './Output'

export type CellRunState = 'queued' | 'running' | undefined

type Props = {
  cell: Cell
  index: number
  total: number
  selected: boolean
  /** Changes whenever the editor should take keyboard focus. */
  focusToken: number
  runState: CellRunState
  dark: boolean
  onSelect: () => void
  onChange: (source: string) => void
  onRun: (advance: boolean) => void
  onDelete: () => void
  onMove: (delta: number) => void
  onToggleType: () => void
}

export function CellView(p: Props) {
  const { cell } = p
  const [editingMd, setEditingMd] = useState(cell.source.trim() === '')
  const isCode = cell.cell_type === 'code'
  const runRef = useRef(p.onRun)
  runRef.current = p.onRun
  const editor = useRef<ReactCodeMirrorRef>(null)

  useEffect(() => {
    if (!p.focusToken) return
    if (!isCode) setEditingMd(true)
    // The CodeMirror view is created asynchronously after mount: retry briefly.
    let tries = 0
    const id = window.setInterval(() => {
      const view = editor.current?.view
      if (view || ++tries > 30) {
        window.clearInterval(id)
        view?.focus()
      }
    }, 20)
    return () => window.clearInterval(id)
  }, [p.focusToken]) // eslint-disable-line react-hooks/exhaustive-deps

  const extensions = useMemo(
    () => [
      isCode ? python() : markdown(),
      EditorView.lineWrapping,
      Prec.highest(
        keymap.of([
          { key: 'Shift-Enter', run: () => (runRef.current(true), setEditingMd(false), true) },
          { key: 'Mod-Enter', run: () => (runRef.current(false), setEditingMd(false), true) },
        ]),
      ),
    ],
    [isCode],
  )

  const counter = p.runState === 'running' ? '*' : p.runState === 'queued' ? '…' : cell.execution_count ?? ' '

  return (
    <div className={`cell ${p.selected ? 'selected' : ''} ${p.runState ?? ''}`} onMouseDown={p.onSelect}>
      <div className="cell-gutter">
        {isCode ? (
          <button className="run-btn" title="Exécuter (Maj+Entrée)" onClick={() => p.onRun(false)}>
            {p.runState ? <Loader2 size={16} className="spin" /> : <Play size={16} />}
          </button>
        ) : null}
        {isCode && <span className="exec-count">[{counter}]</span>}
      </div>
      <div className="cell-body">
        {isCode || editingMd ? (
          <CodeMirror
            ref={editor}
            value={cell.source}
            onChange={p.onChange}
            extensions={extensions}
            theme={p.dark ? 'dark' : 'light'}
            basicSetup={{ foldGutter: false, highlightActiveLine: false, lineNumbers: isCode }}
            placeholder={isCode ? 'Code Python…' : 'Texte markdown…'}
            autoFocus={!isCode && editingMd}
          />
        ) : (
          <div
            className="markdown md-cell"
            title="Double-clic pour éditer"
            onDoubleClick={() => setEditingMd(true)}
            dangerouslySetInnerHTML={{ __html: renderMarkdown(cell.source) }}
          />
        )}
        {isCode && cell.outputs.length > 0 && (
          <div className="outputs">
            {cell.outputs.map((o, i) => (
              <OutputView key={i} output={o} />
            ))}
          </div>
        )}
      </div>
      <div className="cell-tools">
        <button title="Monter" disabled={p.index === 0} onClick={() => p.onMove(-1)}>
          <ArrowUp size={14} />
        </button>
        <button title="Descendre" disabled={p.index === p.total - 1} onClick={() => p.onMove(1)}>
          <ArrowDown size={14} />
        </button>
        <button title={isCode ? 'Convertir en texte' : 'Convertir en code'} onClick={p.onToggleType}>
          {isCode ? <Type size={14} /> : <Code2 size={14} />}
        </button>
        <button title="Supprimer" className="danger" onClick={p.onDelete}>
          <Trash2 size={14} />
        </button>
      </div>
    </div>
  )
}
