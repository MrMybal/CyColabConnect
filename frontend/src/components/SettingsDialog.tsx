import { python } from '@codemirror/lang-python'
import CodeMirror from '@uiw/react-codemirror'
import { X } from 'lucide-react'
import { useState } from 'react'
import { ACCELERATOR_LABELS, type Notebook, type NotebookSettings } from '../api'

type Props = {
  notebook: Notebook
  dark: boolean
  onClose: () => void
  onSave: (settings: Partial<NotebookSettings>) => void
}

const HIGH_MEM_ONLY = new Set(['L4', 'V5E1', 'V6E1'])

export function SettingsDialog({ notebook, dark, onClose, onSave }: Props) {
  const s = notebook.settings
  const [accelerator, setAccelerator] = useState(s.accelerator)
  const [highMem, setHighMem] = useState(s.high_mem)
  const [pip, setPip] = useState(s.pip_packages.join('\n'))
  const [env, setEnv] = useState(
    Object.entries(s.env)
      .map(([k, v]) => `${k}=${v}`)
      .join('\n'),
  )
  const [setup, setSetup] = useState(s.setup_code)

  const save = () => {
    const envObj: Record<string, string> = {}
    for (const line of env.split('\n')) {
      const i = line.indexOf('=')
      if (i > 0) envObj[line.slice(0, i).trim()] = line.slice(i + 1).trim()
    }
    onSave({
      accelerator,
      high_mem: highMem,
      pip_packages: pip
        .split(/[\n,]/)
        .map((p) => p.trim())
        .filter(Boolean),
      env: envObj,
      setup_code: setup,
    })
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Configuration du runtime</h2>
          <button className="icon" onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <p className="muted">
          Appliquée à chaque démarrage (connexion ou redémarrage du kernel). Un changement d'accélérateur nécessite de
          reconnecter le notebook.
        </p>

        <div className="form-row">
          <label>Accélérateur</label>
          <div className="accel-grid">
            {Object.entries(ACCELERATOR_LABELS).map(([k, v]) => (
              <button key={k} className={accelerator === k ? 'chip active' : 'chip'} onClick={() => setAccelerator(k)}>
                {v}
              </button>
            ))}
          </div>
        </div>

        <div className="form-row">
          <label>
            <input
              type="checkbox"
              checked={highMem || HIGH_MEM_ONLY.has(accelerator)}
              disabled={HIGH_MEM_ONLY.has(accelerator)}
              onChange={(e) => setHighMem(e.target.checked)}
            />{' '}
            High-RAM {HIGH_MEM_ONLY.has(accelerator) && <span className="muted">(toujours activé pour cet accélérateur)</span>}
          </label>
        </div>

        <div className="form-row">
          <label>Paquets pip (un par ligne)</label>
          <textarea rows={4} value={pip} onChange={(e) => setPip(e.target.value)} placeholder={'transformers\naccelerate>=1.0'} spellCheck={false} />
        </div>

        <div className="form-row">
          <label>Variables d'environnement (CLE=valeur)</label>
          <textarea rows={3} value={env} onChange={(e) => setEnv(e.target.value)} placeholder="HF_HOME=/content/hf" spellCheck={false} />
          <span className="muted small">Stockées en clair dans le .ipynb : n'y mettez pas de secrets si vous partagez le fichier.</span>
        </div>

        <div className="form-row">
          <label>Code de setup (exécuté après pip)</label>
          <div className="setup-editor">
            <CodeMirror value={setup} onChange={setSetup} extensions={[python()]} theme={dark ? 'dark' : 'light'} minHeight="80px" basicSetup={{ foldGutter: false }} />
          </div>
        </div>

        <div className="modal-actions">
          <button onClick={onClose}>Annuler</button>
          <button className="primary" onClick={save}>
            Enregistrer
          </button>
        </div>
      </div>
    </div>
  )
}
