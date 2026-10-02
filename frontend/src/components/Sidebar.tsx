import { Cpu, FilePlus2, FileUp, NotebookPen, Power, Trash2 } from 'lucide-react'
import { useRef, useState } from 'react'
import { ACCELERATOR_LABELS, api, type NotebookSummary, type Runtime } from '../api'

type Props = {
  notebooks: NotebookSummary[]
  runtimes: Runtime[]
  selected: string | null
  onSelect: (id: string) => void
  onChanged: () => void
  onError: (m: string) => void
}

function idleLabel(ts: number | null) {
  if (!ts) return 'inactif'
  const minutes = Math.floor((Date.now() / 1000 - ts) / 60)
  if (minutes < 1) return "à l'instant"
  return minutes < 60 ? `inactif ${minutes} min` : `inactif ${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

export function Sidebar({ notebooks, runtimes, selected, onSelect, onChanged, onError }: Props) {
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [accel, setAccel] = useState('NONE')
  const fileInput = useRef<HTMLInputElement>(null)
  const byNotebook = new Map(runtimes.filter((r) => r.notebook_id).map((r) => [r.notebook_id!, r]))

  const create = async () => {
    try {
      const nb = await api.createNotebook(name.trim() || 'Sans titre', accel)
      setCreating(false)
      setName('')
      onChanged()
      onSelect(nb.id)
    } catch (e) {
      onError((e as Error).message)
    }
  }

  return (
    <aside className="sidebar">
      <div className="side-head">
        <span>Notebooks</span>
        <button className="icon" title="Nouveau notebook" onClick={() => setCreating((v) => !v)}>
          <FilePlus2 size={16} />
        </button>
        <button className="icon" title="Importer un .ipynb" onClick={() => fileInput.current?.click()}>
          <FileUp size={16} />
        </button>
        <input
          ref={fileInput}
          type="file"
          accept=".ipynb"
          hidden
          onChange={async (e) => {
            const f = e.target.files?.[0]
            e.target.value = ''
            if (!f) return
            try {
              const nb = await api.importNotebook(f)
              onChanged()
              onSelect(nb.id)
            } catch (err) {
              onError((err as Error).message)
            }
          }}
        />
      </div>

      {creating && (
        <div className="new-nb">
          <input autoFocus placeholder="Nom du notebook" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && create()} />
          <select value={accel} onChange={(e) => setAccel(e.target.value)}>
            {Object.entries(ACCELERATOR_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
          <button className="primary" onClick={create}>
            Créer
          </button>
        </div>
      )}

      <ul className="nb-list">
        {notebooks.map((nb) => {
          const rt = byNotebook.get(nb.id)
          return (
            <li key={nb.id} className={selected === nb.id ? 'active' : ''} onClick={() => onSelect(nb.id)}>
              <NotebookPen size={14} />
              <span className="fname">{nb.name}</span>
              <span className={`badge ${rt ? 'on' : ''}`} title={rt ? 'Runtime connecté' : 'Accélérateur configuré'}>
                {ACCELERATOR_LABELS[nb.settings.accelerator] ?? nb.settings.accelerator}
              </span>
              <button
                className="icon danger hover-only"
                title="Supprimer"
                onClick={async (e) => {
                  e.stopPropagation()
                  if (!confirm(`Supprimer le notebook « ${nb.name} » ?${rt ? '\nSon runtime sera arrêté.' : ''}`)) return
                  try {
                    await api.deleteNotebook(nb.id)
                    onChanged()
                  } catch (err) {
                    onError((err as Error).message)
                  }
                }}
              >
                <Trash2 size={13} />
              </button>
            </li>
          )
        })}
        {notebooks.length === 0 && <li className="muted">Aucun notebook. Créez-en un avec ＋.</li>}
      </ul>

      <div className="side-head runtimes-head">
        <span>Runtimes Colab</span>
      </div>
      <ul className="rt-list">
        {runtimes.map((r) => (
          <li key={r.endpoint}>
            <Cpu size={14} />
            <div className="rt-info">
              <span>
                {ACCELERATOR_LABELS[r.accelerator] ?? r.accelerator}
                {r.high_mem ? ' · HM' : ''}
              </span>
              <span className="muted small" title={r.endpoint}>
                {r.notebook_id ?? (r.managed ? 'libre' : 'externe (Colab web / CLI)')}
                {' · '}
                {r.busy ? <span className="busy-text">actif</span> : idleLabel(r.last_activity)}
              </span>
            </div>
            <button
              className="icon danger"
              title="Arrêter ce runtime"
              onClick={async () => {
                if (!confirm('Arrêter ce runtime ? Les fichiers et variables en mémoire seront perdus.')) return
                try {
                  await api.stopRuntime(r.endpoint)
                  onChanged()
                } catch (e) {
                  onError((e as Error).message)
                }
              }}
            >
              <Power size={13} />
            </button>
          </li>
        ))}
        {runtimes.length === 0 && <li className="muted small">Aucun runtime actif.</li>}
      </ul>
    </aside>
  )
}
