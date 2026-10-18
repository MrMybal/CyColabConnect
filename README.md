# CyColabConnect

**English** · [Français](README.fr.md)

A local application to **create, configure and run Google Colab notebooks**
(CPU, T4/L4/G4/A100/H100 GPUs, v5e/v6e TPUs), with a **built-in MCP server**: an AI agent
(Claude Code, Gemini CLI and others) controls the same notebooks and runtimes while its actions
appear live in the interface.

```
Browser ───HTTP/WS──┐
                    ├── ColabConnect (FastAPI, 127.0.0.1:8420) ──► colab.research.google.com/tun/m  (VM allocation)
AI agent ──MCP /mcp─┘                                         └──► Colab runtime: Jupyter (kernel WS, files)
```

<a id="démarrage"></a>

## Getting started

Requirement: [uv](https://docs.astral.sh/uv/) (and Node.js 20+ only for a Git clone:
release archives include the built interface).

Download `CyColabConnect-X.Y.Z.zip` from the
[latest release](https://github.com/MrMybal/CyColabConnect/releases), extract it, then run:

```bash
ColabConnect.bat
```

Use `./colabconnect.sh` on Linux/macOS.

Or start it manually:

```bash
cd frontend && npm install && npm run build
cd ../backend && uv run colabconnect serve
```

The interface opens at http://127.0.0.1:8420. To sign in to Google, click
“Sign in with Google”, authorize access, then paste the code displayed by Google
(the same flow as `google-colab-cli`). Alternatively, use `gcloud` credentials (ADC).

<a id="fonctionnalités"></a>

## Features

- **Local `.ipynb` notebooks** (`~/.colabconnect/notebooks`): create, import/export,
  code/Markdown cells, run individual cells or a full notebook, and stream outputs
  (text, images, HTML, pandas tables and errors).
- **Per-notebook configuration**: accelerator, High-RAM, pip packages, environment
  variables and setup code, applied automatically when the runtime starts.
- **Runtimes**: allocate/release, attach to an existing runtime, interrupt,
  restart the kernel, inspect resources (RAM/VRAM/disk), view compute-unit balance,
  and open the runtime in Colab's Web interface.
- **Runtime files**: browse, upload through drag and drop, download and delete.
- **Bash terminal** (tmux) in the interface, like `colab console`.
- **Automatic shutdown** of idle runtimes (default: 1 hour, configurable in Settings)
  to save compute units; runtimes reclaimed by Colab are detected and reported.
- **Google Drive**: `drive.mount()` works; the application displays the authorization link.
- **MCP**: 27 tools (notebooks, cells, execution, shell, pip, runtimes, files and
  automatic shutdown), plus an activity feed in the interface.

<a id="connecter-un-agent-mcp"></a>

## Connect an agent (MCP)

Claude Code:

```bash
claude mcp add --transport http colabconnect http://127.0.0.1:8420/mcp
```

Gemini CLI (`settings.json`): `{"mcpServers": {"colabconnect": {"httpUrl": "http://127.0.0.1:8420/mcp"}}}`

For stdio-only clients (Claude Desktop and others), ColabConnect must already be running:

```json
{"mcpServers": {"colabconnect": {"command": "uv", "args": ["--directory", "<CyColabConnect folder>/backend", "run", "colabconnect", "mcp-stdio"]}}}
```

<a id="mises-à-jour"></a>

## Updates

ColabConnect checks [GitHub releases](https://github.com/MrMybal/CyColabConnect/releases)
at startup and every 6 hours. A badge appears in the top bar when an update is available.
**Settings → Updates → Install and restart** downloads the archive, verifies its SHA-256,
replaces `backend/src` and `frontend/dist`, then the launcher restarts the application.
Your data (`~/.colabconnect`: account, notebooks and settings) is preserved.

- For a private repository, provide a read-only, fine-grained GitHub token
  (Contents: Read) in Settings or through `COLABCONNECT_GITHUB_TOKEN`.
- From a Git clone, the application reports new versions but does not overwrite files: use `git pull`.

<a id="publier-une-version"></a>

### Publish a version

```powershell
.\scripts\release.ps1 0.2.0
```

The script updates the version, commits, creates the `v0.2.0` tag and pushes it.
The `Release` GitHub Action runs tests, builds the interface and publishes the archive.

## Configuration

| Variable | Default |
|---|---|
| `COLABCONNECT_PORT` | `8420` |
| `COLABCONNECT_HOME` | `~/.colabconnect` (runtimes, notebooks and settings; the Google token is stored in Windows Credential Manager / the system keychain) |
| `COLABCONNECT_NOTEBOOKS` | `$COLABCONNECT_HOME/notebooks` |
| `COLABCONNECT_CELL_TIMEOUT` | `3600` seconds |
| `COLABCONNECT_GITHUB_TOKEN` | GitHub token for updates from a private repository |
| `COLABCONNECT_UPDATE_REPO` | `MrMybal/CyColabConnect` |

<a id="développement"></a>

## Development

```bash
cd backend && uv run pytest          # end-to-end tests (fake Colab + real local Jupyter)
cd frontend && npm run dev           # UI on :5173, proxy to :8420
```

<a id="limites"></a>

## Limitations

- The `/tun/m/*` endpoints belong to Colab's Web interface (unofficial, reverse-engineered
  by `google-colab-cli`) and may change. The official `colaboratory.googleapis.com`
  API is in an allowlisted beta.
- The server listens only on `127.0.0.1` and rejects requests from other origins:
  it can execute code on Colab using your Google credentials.
- GPUs consume compute units: remember to release runtimes.

<a id="licence"></a>

## License

[GPL-3.0-or-later](LICENSE). Part of the Colab client is adapted from
[google-colab-cli](https://github.com/googlecolab/google-colab-cli) (Apache-2.0, © Google LLC):
see [NOTICE](NOTICE). This project is not affiliated with Google.
