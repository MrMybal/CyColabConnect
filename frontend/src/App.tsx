import { Activity, ArrowUpCircle, Bot, FolderOpen, Loader2, LogOut, PanelRightClose, PanelRightOpen, Settings, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { api, type AppEvent, type AuthStatus, type Info, type Notebook, type NotebookSummary, type Runtime, type UpdateStatus, type Usage } from './api'
import { AppSettingsDialog } from './components/AppSettingsDialog'
import { Login } from './components/Login'
import { NotebookView } from './components/NotebookView'
import { FilesPanel, McpPanel, ResourcesPanel } from './components/SidePanels'
import { Sidebar } from './components/Sidebar'
import { useEvents, useEventsConnected } from './events'

type Toast = { id: number; message: string; kind: 'error' | 'info' }
type Tab = 'files' | 'resources' | 'mcp'

const storage = {
  get: (k: string) => {
    try {
      return localStorage.getItem(k)
    } catch {
      return null
    }
  },
  set: (k: string, v: string | null) => {
    try {
      if (v === null) localStorage.removeItem(k)
      else localStorage.setItem(k, v)
    } catch {
      /* ignore */
    }
  },
}

function usePrefersDark() {
  const q = '(prefers-color-scheme: dark)'
  const [dark, setDark] = useState(() => matchMedia(q).matches)
  useEffect(() => {
    const m = matchMedia(q)
    const on = () => setDark(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return dark
}

export default function App() {
  const dark = usePrefersDark()
  const connected = useEventsConnected()
  const [auth, setAuth] = useState<AuthStatus | null>(null)
  const [info, setInfo] = useState<Info | null>(null)
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([])
  const [runtimes, setRuntimes] = useState<Runtime[]>([])
  const [usage, setUsage] = useState<Usage | null>(null)
  const [selected, setSelected] = useState<string | null>(() => storage.get('cc.notebook'))
  const [current, setCurrent] = useState<Notebook | null>(null)
  const [tab, setTab] = useState<Tab>(() => (storage.get('cc.tab') as Tab) || 'mcp')
  const [panelOpen, setPanelOpen] = useState(() => storage.get('cc.panel') !== '0')
  const [activity, setActivity] = useState<AppEvent[]>([])
  const [toasts, setToasts] = useState<Toast[]>([])
  const [driveUrl, setDriveUrl] = useState<string | null>(null)

  const [showSettings, setShowSettings] = useState(false)
  const [update, setUpdate] = useState<UpdateStatus | null>(null)
  const [restarting, setRestarting] = useState<string | null>(null)
  const [, setTick] = useState(0)

  const toast = useCallback((message: string, kind: Toast['kind'] = 'error') => {
    const id = Date.now() + Math.random()
    setToasts((t) => [...t.slice(-3), { id, message, kind }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 8000)
  }, [])

  const loadNotebooks = useCallback(() => api.notebooks().then(setNotebooks).catch((e) => toast(e.message)), [toast])
  const loadRuntimes = useCallback(
    (refresh = true) =>
      api
        .runtimes()
        .then(setRuntimes)
        .catch((e) => (refresh ? toast(e.message) : undefined)),
    [toast],
  )
  const loadUsage = useCallback(() => api.usage().then(setUsage).catch(() => {}), [])

  useEffect(() => {
    api.authStatus().then(setAuth)
    api.info().then(setInfo)
    api.activity().then(setActivity).catch(() => {})
    api.updateStatus().then(setUpdate).catch(() => {})
  }, [])

  const onInstalled = (version: string, restart: 'auto' | 'manual') => {
    setShowSettings(false)
    if (restart === 'manual') {
      toast(`Version ${version} installée : relancez ColabConnect pour l'utiliser.`, 'info')
      return
    }
    setRestarting(version)
    // Wait for the server to come back with the new version, then reload.
    const started = Date.now()
    const poll = setInterval(async () => {
      try {
        const info = await api.info()
        if (info.version === version) {
          clearInterval(poll)
          location.reload()
        }
      } catch {
        /* server restarting */
      }
      if (Date.now() - started > 120_000) {
        clearInterval(poll)
        setRestarting(null)
        toast("Le redémarrage prend du temps : relancez ColabConnect manuellement si besoin.", 'info')
      }
    }, 1500)
  }

  useEffect(() => {
    if (!auth?.authenticated) return
    loadNotebooks()
    loadRuntimes()
    loadUsage()
    const id = setInterval(loadUsage, 60_000)
    return () => clearInterval(id)
  }, [auth?.authenticated, loadNotebooks, loadRuntimes, loadUsage])

  // Re-render every 30 s so idle durations stay current.
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => storage.set('cc.notebook', selected), [selected])
  useEffect(() => storage.set('cc.tab', tab), [tab])
  useEffect(() => storage.set('cc.panel', panelOpen ? '1' : '0'), [panelOpen])

  useEvents((e) => {
    switch (e.type) {
      case 'runtimes':
        setRuntimes(e.runtimes)
        loadUsage()
        break
      case 'notebook_changed':
        if (e.source !== 'ui' && e.source !== 'execution') loadNotebooks()
        if (e.source === 'mcp' || e.source === 'create') setSelected((s) => s ?? e.notebook_id)
        break
      case 'notebook_deleted':
        loadNotebooks()
        setSelected((s) => (s === e.notebook_id ? null : s))
        break
      case 'runtime_status':
        if (e.status === 'error') toast(`Runtime : ${e.error}`)
        if (e.status === 'setup_error') toast('Échec du setup du runtime (voir les paquets pip / code de setup).')
        if (e.status === 'lost') toast("Le runtime a été arrêté par Colab. Relancez une cellule pour en allouer un nouveau.", 'info')
        break
      case 'runtime_auto_stopped':
        toast(`Runtime ${e.accelerator}${e.notebook_id ? ` (${e.notebook_id})` : ''} libéré après ${e.minutes} min d'inactivité.`, 'info')
        break
      case 'error':
        toast(e.message)
        break
      case 'update_available':
        setUpdate(e as unknown as UpdateStatus)
        break
      case 'drive_auth_required':
        setDriveUrl(e.url)
        break
      case 'drive_auth_done':
        setDriveUrl(null)
        break
      case 'mcp_call':
        setActivity((list) => {
          const i = list.findIndex((a) => a.id === e.id)
          if (i >= 0) {
            const copy = [...list]
            copy[i] = { ...copy[i], ...e }
            return copy
          }
          return [e, ...list].slice(0, 100)
        })
        if (e.status === 'running' && e.arguments?.notebook_id) setSelected(e.arguments.notebook_id)
        break
    }
  })

  if (!auth) return null
  if (!auth.authenticated) return <Login status={auth} onDone={setAuth} />

  const selectedExists = selected && notebooks.some((n) => n.id === selected)
  const runningAgent = activity.some((a) => a.status === 'running')

  return (
    <div className={`app ${panelOpen ? 'with-panel' : ''}`}>
      <header className="topbar">
        <div className="brand">
          <img src="/favicon.svg" width={22} height={22} alt="" />
          ColabConnect
        </div>
        {!connected && <span className="status-pill busy">Reconnexion au serveur…</span>}
        <div className="spacer" />
        {usage?.balance != null && (
          <span className="usage" title="Unités de calcul Colab">
            {usage.balance.toFixed(1)} UC{usage.rate_per_hour ? ` · ${usage.rate_per_hour.toFixed(2)}/h` : ''}
          </span>
        )}
        <span className="account" title={auth.token_storage ? `Jeton Google : ${auth.token_storage}` : undefined}>{auth.email ?? (auth.method === 'adc' ? 'gcloud ADC' : 'Compte Google')}</span>
        {update?.update_available && (
          <button className="update-badge" onClick={() => setShowSettings(true)} title="Mise à jour disponible">
            <ArrowUpCircle size={14} /> v{update.latest}
          </button>
        )}
        <button className="icon" title="Paramètres" onClick={() => setShowSettings(true)}>
          <Settings size={16} />
        </button>
        <button
          className="icon"
          title="Se déconnecter"
          onClick={async () => {
            if (confirm('Se déconnecter du compte Google ?')) setAuth(await api.logout())
          }}
        >
          <LogOut size={16} />
        </button>
        <button className="icon" title={panelOpen ? 'Masquer le panneau' : 'Afficher le panneau'} onClick={() => setPanelOpen((v) => !v)}>
          {panelOpen ? <PanelRightClose size={16} /> : <PanelRightOpen size={16} />}
        </button>
      </header>

      {driveUrl && (
        <div className="banner">
          Le notebook demande l'accès à Google Drive.{' '}
          <a href={driveUrl} target="_blank" rel="noreferrer noopener">
            Autoriser l'accès
          </a>{' '}
          — l'exécution reprend automatiquement.
          <button className="icon" onClick={() => setDriveUrl(null)}>
            <X size={14} />
          </button>
        </div>
      )}

      <Sidebar
        notebooks={notebooks}
        runtimes={runtimes}
        selected={selected}
        onSelect={setSelected}
        onChanged={() => {
          loadNotebooks()
          loadRuntimes(false)
        }}
        onError={toast}
      />

      <main className="main">
        {selectedExists ? (
          <NotebookView
            key={selected}
            notebookId={selected!}
            runtimes={runtimes}
            dark={dark}
            onError={toast}
            onNotebookLoaded={setCurrent}
            onRenamed={(id) => {
              loadNotebooks()
              setSelected(id)
            }}
          />
        ) : (
          <div className="empty-state">
            <h2>Aucun notebook ouvert</h2>
            <p className="muted">Créez ou importez un notebook dans la barre latérale, ou demandez à votre agent IA via MCP.</p>
          </div>
        )}
      </main>

      {panelOpen && (
        <aside className="panel">
          <div className="tabs">
            <button className={tab === 'files' ? 'active' : ''} onClick={() => setTab('files')}>
              <FolderOpen size={14} /> Fichiers
            </button>
            <button className={tab === 'resources' ? 'active' : ''} onClick={() => setTab('resources')}>
              <Activity size={14} /> Ressources
            </button>
            <button className={tab === 'mcp' ? 'active' : ''} onClick={() => setTab('mcp')}>
              <Bot size={14} /> Agent MCP {runningAgent && <span className="dot" />}
            </button>
          </div>
          <div className="panel-body">
            {tab === 'files' && <FilesPanel key={current?.runtime?.endpoint} runtime={selectedExists ? current?.runtime ?? null : null} onError={toast} />}
            {tab === 'resources' && <ResourcesPanel runtime={selectedExists ? current?.runtime ?? null : null} />}
            {tab === 'mcp' && <McpPanel info={info} activity={activity} />}
          </div>
        </aside>
      )}

      {showSettings && (
        <AppSettingsDialog
          onClose={() => setShowSettings(false)}
          onError={toast}
          update={update}
          onUpdateStatus={setUpdate}
          onInstalled={onInstalled}
        />
      )}

      {restarting && (
        <div className="modal-backdrop">
          <div className="modal restarting">
            <Loader2 className="spin" /> Installation de la version {restarting} et redémarrage…
          </div>
        </div>
      )}

      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`} onClick={() => setToasts((l) => l.filter((x) => x.id !== t.id))}>
            {t.message}
          </div>
        ))}
      </div>
    </div>
  )
}
