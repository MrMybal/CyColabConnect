import { ExternalLink, KeyRound, Loader2, Terminal } from 'lucide-react'
import { useState } from 'react'
import { api, type AuthStatus } from '../api'

export function Login({ status, onDone }: { status: AuthStatus | null; onDone: (s: AuthStatus) => void }) {
  const [url, setUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async (fn: () => Promise<AuthStatus>) => {
    setBusy(true)
    setError(null)
    try {
      onDone(await fn())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const start = async () => {
    setError(null)
    try {
      const { url } = await api.loginStart()
      setUrl(url)
      window.open(url, '_blank', 'noopener')
    } catch (e) {
      setError((e as Error).message)
    }
  }

  return (
    <div className="login">
      <div className="login-card">
        <img src="/favicon.svg" width={48} height={48} alt="" />
        <h1>ColabConnect</h1>
        <p className="muted">Créez, configurez et exécutez vos notebooks sur Google Colab — avec un serveur MCP intégré pour vos agents IA.</p>

        <ol className="steps">
          <li>
            <button className="primary" onClick={start}>
              <KeyRound size={15} /> Se connecter avec Google
            </button>
            {url && (
              <a className="small" href={url} target="_blank" rel="noreferrer noopener">
                Rouvrir la page de connexion <ExternalLink size={11} />
              </a>
            )}
          </li>
          <li className={url ? '' : 'disabled'}>
            <span>Après autorisation, Google affiche un code : collez-le ici.</span>
            <div className="code-row">
              <input
                placeholder="4/0Ab…"
                value={code}
                disabled={!url}
                onChange={(e) => setCode(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && code && run(() => api.loginFinish(code))}
              />
              <button className="primary" disabled={!url || !code || busy} onClick={() => run(() => api.loginFinish(code))}>
                {busy ? <Loader2 size={15} className="spin" /> : 'Valider'}
              </button>
            </div>
          </li>
        </ol>

        {error && <div className="error-box">{error}</div>}

        <details className="adc">
          <summary>
            <Terminal size={13} /> Utiliser plutôt gcloud (Application Default Credentials)
          </summary>
          <p className="small muted">Lancez cette commande dans un terminal, puis cliquez ci-dessous :</p>
          <pre className="small">{status?.adc_command}</pre>
          <button disabled={busy} onClick={() => run(api.loginAdc)}>
            Utiliser les identifiants gcloud
          </button>
        </details>

        <p className="small muted legal">
          Les identifiants restent sur cette machine (~/.colabconnect). ColabConnect utilise les mêmes API non officielles que google-colab-cli.
        </p>
      </div>
    </div>
  )
}
