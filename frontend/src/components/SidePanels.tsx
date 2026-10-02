import { Check, Copy, Download, File, Folder, Loader2, RefreshCw, Trash2, Upload } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type AppEvent, type FileEntry, type Info, type Runtime } from '../api'

// -- files ------------------------------------------------------------------------

export function FilesPanel({ runtime, onError }: { runtime: Runtime | null; onError: (m: string) => void }) {
  const [path, setPath] = useState('content')
  const [files, setFiles] = useState<FileEntry[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const endpoint = runtime?.endpoint

  const load = useCallback(async () => {
    if (!endpoint) return
    setLoading(true)
    try {
      setFiles(await api.files(endpoint, path))
    } catch (e) {
      onError((e as Error).message)
      setFiles([])
    } finally {
      setLoading(false)
    }
  }, [endpoint, path, onError])

  useEffect(() => {
    load()
  }, [load])

  if (!endpoint) return <p className="muted pad">Connectez le notebook à un runtime pour parcourir ses fichiers.</p>

  const upload = async (list: FileList | null) => {
    if (!list) return
    setLoading(true)
    for (const f of Array.from(list)) {
      try {
        await api.uploadFile(endpoint, path, f)
      } catch (e) {
        onError(`${f.name} : ${(e as Error).message}`)
      }
    }
    load()
  }

  const parts = path.split('/').filter(Boolean)

  return (
    <div
      className={`files ${dragging ? 'dragging' : ''}`}
      onDragOver={(e) => {
        e.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragging(false)
        upload(e.dataTransfer.files)
      }}
    >
      <div className="files-head">
        <div className="crumbs">
          <button onClick={() => setPath('')}>/</button>
          {parts.map((p, i) => (
            <button key={i} onClick={() => setPath(parts.slice(0, i + 1).join('/'))}>
              {p}/
            </button>
          ))}
        </div>
        <button className="icon" title="Rafraîchir" onClick={load}>
          {loading ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
        <button className="icon" title="Envoyer des fichiers" onClick={() => input.current?.click()}>
          <Upload size={14} />
        </button>
        <input ref={input} type="file" multiple hidden onChange={(e) => upload(e.target.files)} />
      </div>
      <ul className="file-list">
        {parts.length > 0 && (
          <li onClick={() => setPath(parts.slice(0, -1).join('/'))}>
            <Folder size={14} /> ..
          </li>
        )}
        {files?.map((f) => (
          <li key={f.path} onClick={() => f.type === 'directory' && setPath(f.path)} className={f.type}>
            {f.type === 'directory' ? <Folder size={14} /> : <File size={14} />}
            <span className="fname">{f.name}</span>
            {f.type !== 'directory' && (
              <>
                <span className="muted small">{formatBytes(f.size)}</span>
                <a className="button icon" title="Télécharger" href={api.downloadUrl(endpoint, f.path)} onClick={(e) => e.stopPropagation()}>
                  <Download size={13} />
                </a>
                <button
                  className="icon danger"
                  title="Supprimer"
                  onClick={async (e) => {
                    e.stopPropagation()
                    if (!confirm(`Supprimer ${f.path} du runtime ?`)) return
                    try {
                      await api.deleteFile(endpoint, f.path)
                    } catch (err) {
                      onError((err as Error).message)
                    }
                    load()
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </>
            )}
          </li>
        ))}
        {files?.length === 0 && <li className="muted">Dossier vide — glissez des fichiers ici.</li>}
      </ul>
    </div>
  )
}

// -- resources ----------------------------------------------------------------------

export function ResourcesPanel({ runtime }: { runtime: Runtime | null }) {
  const [data, setData] = useState<Record<string, any> | null>(null)
  const endpoint = runtime?.endpoint

  useEffect(() => {
    if (!endpoint) return
    let alive = true
    const tick = () =>
      api
        .resources(endpoint)
        .then((d) => alive && setData(d))
        .catch(() => {})
    tick()
    const id = setInterval(tick, 5000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [endpoint])

  if (!endpoint) return <p className="muted pad">Aucun runtime connecté.</p>
  if (!data) return <Loader2 className="spin pad" />
  if (data.error) return <p className="muted pad">Ressources indisponibles ({data.error}).</p>

  const mem = data.memory
  const bars: { label: string; used: number; total: number; pct?: number }[] = []
  if (mem?.totalBytes) bars.push({ label: 'RAM', used: mem.totalBytes - mem.freeBytes, total: mem.totalBytes })
  for (const g of data.gpus ?? [])
    bars.push({ label: `GPU ${g.name ?? ''} (VRAM)`, used: g.memoryUsedBytes ?? 0, total: g.memoryTotalBytes ?? 0, pct: g.gpuUtilization })
  for (const d of data.disks ?? [])
    if (d.filesystem?.totalBytes) bars.push({ label: `Disque ${d.filesystem.label ?? ''}`, used: d.filesystem.usedBytes, total: d.filesystem.totalBytes })

  return (
    <div className="pad resources">
      {bars.map((b) => (
        <div key={b.label} className="bar-row">
          <div className="bar-label">
            <span>{b.label}</span>
            <span className="muted small">
              {formatBytes(b.used)} / {formatBytes(b.total)}
              {b.pct !== undefined && ` · util. ${Math.round(b.pct * 100)}%`}
            </span>
          </div>
          <div className="bar">
            <div style={{ width: `${b.total ? Math.min(100, (b.used / b.total) * 100) : 0}%` }} />
          </div>
        </div>
      ))}
      {bars.length === 0 && <pre className="small">{JSON.stringify(data, null, 2)}</pre>}
    </div>
  )
}

// -- MCP -----------------------------------------------------------------------------------

function CopyBlock({ text }: { text: string }) {
  const [done, setDone] = useState(false)
  return (
    <div className="copy-block">
      <pre>{text}</pre>
      <button
        className="icon"
        title="Copier"
        onClick={() => {
          navigator.clipboard.writeText(text)
          setDone(true)
          setTimeout(() => setDone(false), 1200)
        }}
      >
        {done ? <Check size={13} /> : <Copy size={13} />}
      </button>
    </div>
  )
}

export function McpPanel({ info, activity }: { info: Info | null; activity: AppEvent[] }) {
  const url = info?.mcp_url ?? 'http://127.0.0.1:8420/mcp'
  const stdio = JSON.stringify(
    {
      mcpServers: {
        colabconnect: { command: 'uv', args: ['--directory', info?.backend_dir ?? '<chemin>/backend', 'run', 'colabconnect', 'mcp-stdio'] },
      },
    },
    null,
    2,
  )
  return (
    <div className="pad mcp">
      <p>
        Le serveur MCP tourne dans cette app : l'agent manipule les mêmes notebooks et runtimes, et vous voyez ses actions en direct.
      </p>
      <details className="mcp-setup" open={activity.length === 0}>
        <summary>Connecter un agent</summary>
        <h4>Claude Code</h4>
        <CopyBlock text={`claude mcp add --transport http colabconnect ${url}`} />
        <h4>Gemini CLI (settings.json)</h4>
        <CopyBlock text={JSON.stringify({ mcpServers: { colabconnect: { httpUrl: url } } }, null, 2)} />
        <h4>Clients stdio (Claude Desktop…)</h4>
        <CopyBlock text={stdio} />
        <p className="muted small">Le proxy stdio nécessite que ColabConnect soit lancé.</p>
      </details>

      <h4>Activité de l'agent</h4>
      {activity.length === 0 && <p className="muted small">Aucun appel pour l'instant.</p>}
      <ul className="activity">
        {activity.map((a) => (
          <li key={a.id} className={a.status}>
            <div>
              <code>{a.tool}</code>
              <span className="muted small">
                {a.status === 'running' ? <Loader2 size={11} className="spin" /> : a.status === 'error' ? 'erreur' : `${(a.duration ?? 0).toFixed(1)}s`}
              </span>
            </div>
            {a.arguments && Object.keys(a.arguments).length > 0 && (
              <div className="args small">
                {Object.entries(a.arguments)
                  .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
                  .join('  ')}
              </div>
            )}
            {a.error && <div className="err small">{a.error}</div>}
          </li>
        ))}
      </ul>
    </div>
  )
}

export function formatBytes(n: number | null | undefined) {
  if (n == null) return ''
  const units = ['o', 'Ko', 'Mo', 'Go', 'To']
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(i && n < 10 ? 1 : 0)} ${units[i]}`
}
