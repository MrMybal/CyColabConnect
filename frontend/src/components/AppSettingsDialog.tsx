import { Download, ExternalLink, Loader2, RefreshCw, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, type UpdateStatus } from '../api'
import { renderMarkdown } from './Output'

const CHOICES = [0, 15, 30, 60, 120, 240]

type Props = {
  onClose: () => void
  onError: (m: string) => void
  update: UpdateStatus | null
  onUpdateStatus: (s: UpdateStatus) => void
  onInstalled: (version: string, restart: 'auto' | 'manual') => void
}

export function AppSettingsDialog({ onClose, onError, update, onUpdateStatus, onInstalled }: Props) {
  const [minutes, setMinutes] = useState<number | null>(null)
  const [hasToken, setHasToken] = useState(false)
  const [token, setToken] = useState('')
  const [checking, setChecking] = useState(false)
  const [installing, setInstalling] = useState(false)

  useEffect(() => {
    api
      .appSettings()
      .then((s) => {
        setMinutes(s.auto_stop_minutes)
        setHasToken(s.has_github_token)
      })
      .catch((e) => onError(e.message))
  }, [onError])

  const save = async () => {
    try {
      await api.updateAppSettings({ auto_stop_minutes: minutes ?? 0, ...(token ? { github_token: token } : {}) })
      onClose()
    } catch (e) {
      onError((e as Error).message)
    }
  }

  const check = async () => {
    setChecking(true)
    try {
      if (token) {
        await api.updateAppSettings({ github_token: token })
        setHasToken(true)
        setToken('')
      }
      onUpdateStatus(await api.checkUpdate())
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setChecking(false)
    }
  }

  const install = async () => {
    if (!confirm(`Installer la version ${update?.latest} et redémarrer ColabConnect ?`)) return
    setInstalling(true)
    try {
      const res = await api.installUpdate()
      onInstalled(res.installed, res.restart)
    } catch (e) {
      onError((e as Error).message)
      setInstalling(false)
    }
  }

  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Paramètres</h2>
          <button className="icon" onClick={onClose}>
            <X size={16} />
          </button>
        </div>
        <div className="form-row">
          <label>Libérer automatiquement les runtimes inactifs</label>
          <div className="accel-grid">
            {CHOICES.map((m) => (
              <button key={m} className={minutes === m ? 'chip active' : 'chip'} onClick={() => setMinutes(m)}>
                {m === 0 ? 'Jamais' : m < 60 ? `${m} min` : `${m / 60} h`}
              </button>
            ))}
          </div>
          <span className="muted small">
            Un runtime est inactif quand aucune cellule, commande, transfert de fichier ni terminal n'est en cours. Seuls les
            runtimes créés par ColabConnect sont concernés ; ceux ouverts dans Colab web ne sont jamais arrêtés. Économise vos
            unités de calcul.
          </span>
        </div>

        <div className="form-row update-section">
          <label>Mises à jour</label>
          <div className="update-line">
            <span>
              Version installée : <b>{update?.current ?? '…'}</b>
              {update?.latest && (
                <>
                  {' '}
                  · dernière : <b>{update.latest}</b>
                </>
              )}
            </span>
            <button onClick={check} disabled={checking}>
              {checking ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />} Vérifier
            </button>
          </div>
          {update?.error && <div className="error-box small">{update.error}</div>}
          {update?.update_available && (
            <div className="update-box">
              <div className="update-line">
                <b>Nouvelle version {update.latest}</b>
                {update.html_url && (
                  <a className="small" href={update.html_url} target="_blank" rel="noreferrer noopener">
                    Voir sur GitHub <ExternalLink size={11} />
                  </a>
                )}
              </div>
              {update.notes && <div className="markdown small notes" dangerouslySetInnerHTML={{ __html: renderMarkdown(update.notes) }} />}
              {update.install_mode === 'git' ? (
                <p className="muted small">
                  Installation depuis un clone git : mettez à jour avec <code>git pull</code> (ColabConnect n'écrase pas vos
                  fichiers).
                </p>
              ) : (
                <button className="primary" onClick={install} disabled={installing || !update.asset}>
                  {installing ? <Loader2 size={14} className="spin" /> : <Download size={14} />} Installer et redémarrer
                </button>
              )}
            </div>
          )}
          {update && !update.update_available && !update.error && update.latest && (
            <span className="muted small">ColabConnect est à jour.</span>
          )}
          <label className="small token-label">
            Token GitHub {hasToken && <span className="badge on">enregistré</span>}
          </label>
          <input
            type="password"
            autoComplete="off"
            placeholder={hasToken ? '•••••••• (laisser vide pour conserver)' : 'Nécessaire tant que le dépôt est privé'}
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <span className="muted small">
            Token « fine-grained » en lecture seule (Contents : Read) sur {update?.repo ?? 'le dépôt'}. Stocké localement dans
            ~/.colabconnect/settings.json, jamais renvoyé à l'interface.
          </span>
        </div>

        <div className="modal-actions">
          <button onClick={onClose}>Annuler</button>
          <button className="primary" disabled={minutes === null} onClick={save}>
            Enregistrer
          </button>
        </div>
      </div>
    </div>
  )
}
