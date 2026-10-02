export type Output = {
  output_type: 'stream' | 'display_data' | 'execute_result' | 'error'
  name?: string
  text?: string | string[]
  data?: Record<string, string | string[]>
  execution_count?: number | null
  ename?: string
  evalue?: string
  traceback?: string[]
}

export type Cell = {
  id: string
  cell_type: 'code' | 'markdown'
  source: string
  outputs: Output[]
  execution_count: number | null
}

export type NotebookSettings = {
  accelerator: string
  high_mem: boolean
  pip_packages: string[]
  env: Record<string, string>
  setup_code: string
  runtime_endpoint: string | null
}

export type Runtime = {
  endpoint: string
  accelerator: string
  variant: string
  high_mem: boolean
  notebook_id: string | null
  created_at: number | null
  managed: boolean
  kernel_id: string | null
  kernel_connected: boolean
  kernel_state: string
  colab_url: string
  last_activity: number | null
  busy: boolean
  terminals: number
}

export type Notebook = {
  id: string
  name: string
  settings: NotebookSettings
  cells: Cell[]
  runtime: Runtime | null
  running_cell: string | null
}

export type NotebookSummary = {
  id: string
  name: string
  modified: number
  cells: number
  settings: NotebookSettings
}

export type AuthStatus = {
  authenticated: boolean
  method: 'oauth' | 'adc' | null
  email: string | null
  login_pending: boolean
  adc_command: string
}

export type AppSettings = { auto_stop_minutes: number; has_github_token: boolean }

export type UpdateStatus = {
  current: string
  repo: string
  install_mode: 'git' | 'release'
  has_token: boolean
  installing: boolean
  latest?: string
  update_available?: boolean
  notes?: string
  published_at?: string
  html_url?: string
  asset?: { id: number; name: string; size: number } | null
  error?: string | null
}

export type Usage = { balance: number | null; rate_per_hour: number | null; assignments: number | null }

export type Info = { version: string; mcp_url: string; notebooks_dir: string; backend_dir: string; accelerators: string[] }

export type FileEntry = { name: string; path: string; type: string; size: number | null; last_modified: string | null }

export type AppEvent = { type: string; ts: number; [key: string]: any }

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} }
  if (body instanceof FormData) {
    init.body = body
  } else if (body !== undefined) {
    init.body = JSON.stringify(body)
    ;(init.headers as Record<string, string>)['Content-Type'] = 'application/json'
  }
  const resp = await fetch(path, init)
  if (!resp.ok) {
    let message = `${resp.status} ${resp.statusText}`
    try {
      const data = await resp.json()
      if (data.detail) message = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
    } catch {
      /* not json */
    }
    throw new ApiError(resp.status, message)
  }
  return resp.json() as Promise<T>
}

const nb = (id: string) => `/api/notebooks/${encodeURIComponent(id)}`
const rt = (endpoint: string) => `/api/runtimes/${encodeURIComponent(endpoint)}`

export const api = {
  info: () => request<Info>('GET', '/api/info'),
  activity: () => request<AppEvent[]>('GET', '/api/activity'),
  authStatus: () => request<AuthStatus>('GET', '/api/auth/status'),
  loginStart: () => request<{ url: string }>('POST', '/api/auth/login/start'),
  loginFinish: (code: string) => request<AuthStatus>('POST', '/api/auth/login/finish', { code }),
  loginAdc: () => request<AuthStatus>('POST', '/api/auth/adc'),
  logout: () => request<AuthStatus>('POST', '/api/auth/logout'),
  usage: () => request<Usage>('GET', '/api/usage'),
  appSettings: () => request<AppSettings>('GET', '/api/settings'),
  updateAppSettings: (s: { auto_stop_minutes?: number; github_token?: string }) =>
    request<AppSettings>('PUT', '/api/settings', s),
  updateStatus: () => request<UpdateStatus>('GET', '/api/update'),
  checkUpdate: () => request<UpdateStatus>('POST', '/api/update/check'),
  installUpdate: () => request<{ installed: string; restart: 'auto' | 'manual' }>('POST', '/api/update/install'),

  runtimes: () => request<Runtime[]>('GET', '/api/runtimes'),
  createRuntime: (accelerator: string, high_mem: boolean) =>
    request<Runtime>('POST', '/api/runtimes', { accelerator, high_mem }),
  stopRuntime: (endpoint: string) => request('DELETE', rt(endpoint)),
  resources: (endpoint: string) => request<Record<string, any>>('GET', `${rt(endpoint)}/resources`),
  files: (endpoint: string, path: string) =>
    request<FileEntry[]>('GET', `${rt(endpoint)}/files?path=${encodeURIComponent(path)}`),
  uploadFile: (endpoint: string, directory: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    form.append('directory', directory)
    return request<FileEntry>('POST', `${rt(endpoint)}/files`, form)
  },
  downloadUrl: (endpoint: string, path: string) =>
    `${rt(endpoint)}/files/download?path=${encodeURIComponent(path)}`,
  deleteFile: (endpoint: string, path: string) =>
    request('DELETE', `${rt(endpoint)}/files?path=${encodeURIComponent(path)}`),

  notebooks: () => request<NotebookSummary[]>('GET', '/api/notebooks'),
  createNotebook: (name: string, accelerator = 'NONE', high_mem = false) =>
    request<Notebook>('POST', '/api/notebooks', { name, accelerator, high_mem }),
  importNotebook: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Notebook>('POST', '/api/notebooks/import', form)
  },
  notebook: (id: string) => request<Notebook>('GET', nb(id)),
  deleteNotebook: (id: string) => request('DELETE', nb(id)),
  renameNotebook: (id: string, name: string) => request<Notebook>('POST', `${nb(id)}/rename`, { name }),
  downloadNotebookUrl: (id: string) => `${nb(id)}/download`,
  updateSettings: (id: string, settings: Partial<NotebookSettings>) =>
    request<NotebookSettings>('PUT', `${nb(id)}/settings`, settings),

  addCell: (id: string, cell_type: 'code' | 'markdown', index?: number, source = '') =>
    request<{ cell_id: string }>('POST', `${nb(id)}/cells`, { cell_type, index, source }),
  patchCell: (id: string, cellId: string, patch: { source?: string; cell_type?: string }) =>
    request('PATCH', `${nb(id)}/cells/${cellId}`, patch),
  deleteCell: (id: string, cellId: string) => request('DELETE', `${nb(id)}/cells/${cellId}`),
  moveCell: (id: string, cellId: string, index: number) =>
    request('POST', `${nb(id)}/cells/${cellId}/move`, { index }),

  runCell: (id: string, cellId: string) => request('POST', `${nb(id)}/cells/${cellId}/run`),
  runAll: (id: string, from_cell_id?: string) => request('POST', `${nb(id)}/run-all`, { stop_on_error: true, from_cell_id }),
  connect: (id: string) => request('POST', `${nb(id)}/connect`),
  attach: (id: string, endpoint: string) => request<Notebook>('POST', `${nb(id)}/attach`, { endpoint }),
  disconnect: (id: string, stop_runtime: boolean) => request<Notebook>('POST', `${nb(id)}/disconnect`, { stop_runtime }),
  interrupt: (id: string) => request('POST', `${nb(id)}/interrupt`),
  restart: (id: string) => request('POST', `${nb(id)}/restart`),
  clearOutputs: (id: string) => request('POST', `${nb(id)}/clear-outputs`),
}

export const ACCELERATOR_LABELS: Record<string, string> = {
  NONE: 'CPU',
  T4: 'GPU T4',
  L4: 'GPU L4',
  G4: 'GPU G4',
  A100: 'GPU A100',
  H100: 'GPU H100',
  V5E1: 'TPU v5e',
  V6E1: 'TPU v6e',
}
