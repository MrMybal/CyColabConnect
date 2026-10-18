[English](README.md) · **Français**

# CyColabConnect

Application locale pour **créer, configurer et exécuter des notebooks sur Google Colab**
(CPU, GPU T4/L4/G4/A100/H100, TPU v5e/v6e), avec un **serveur MCP intégré** : un agent IA
(Claude Code, Gemini CLI…) pilote les mêmes notebooks et runtimes, et vous voyez ses actions
en direct dans l'interface.

```
Navigateur ──HTTP/WS──┐
                      ├── ColabConnect (FastAPI, 127.0.0.1:8420) ──► colab.research.google.com/tun/m  (allocation des VM)
Agent IA ───MCP /mcp──┘                                         └──► runtime Colab : Jupyter (kernel WS, fichiers)
```

## Démarrage

Prérequis : [uv](https://docs.astral.sh/uv/) (et Node.js 20+ seulement pour un clone git :
les archives de release contiennent l'interface déjà construite).

Téléchargez l'archive `CyColabConnect-X.Y.Z.zip` de la
[dernière release](https://github.com/MrMybal/CyColabConnect/releases), décompressez-la, puis :

```bash
ColabConnect.bat
```

(`./colabconnect.sh` sous Linux/macOS.)

ou manuellement :

```bash
cd frontend && npm install && npm run build
cd ../backend && uv run colabconnect serve
```

L'interface s'ouvre sur http://127.0.0.1:8420. Connexion Google : cliquez sur
« Se connecter avec Google », autorisez, puis collez le code affiché par Google
(même flux que `google-colab-cli`). Alternative : identifiants `gcloud` (ADC).

## Fonctionnalités

- **Notebooks** `.ipynb` locaux (`~/.colabconnect/notebooks`) : création, import/export,
  cellules code/markdown, exécution cellule par cellule ou complète, sorties en streaming
  (texte, images, HTML, tableaux pandas, erreurs).
- **Configuration par notebook** : accélérateur, High-RAM, paquets pip, variables
  d'environnement, code de setup — appliqués automatiquement au démarrage du runtime.
- **Runtimes** : allocation/libération, rattachement d'un runtime existant, interruption,
  redémarrage du kernel, ressources (RAM/VRAM/disque), solde d'unités de calcul, ouverture
  du runtime dans l'interface web de Colab.
- **Fichiers** du runtime : parcourir, envoyer (glisser-déposer), télécharger, supprimer.
- **Terminal** bash (tmux) du runtime dans l'interface, comme `colab console`.
- **Arrêt automatique** des runtimes inactifs (défaut 1 h, réglable dans Paramètres) pour
  économiser les unités de calcul ; un runtime récupéré par Colab est détecté et signalé.
- **Google Drive** : `drive.mount()` fonctionne ; l'app affiche le lien d'autorisation.
- **MCP** : 27 outils (notebooks, cellules, exécution, shell, pip, runtimes, fichiers,
  arrêt auto) + fil d'activité dans l'interface.

## Connecter un agent (MCP)

Claude Code :

```bash
claude mcp add --transport http colabconnect http://127.0.0.1:8420/mcp
```

Gemini CLI (`settings.json`) : `{"mcpServers": {"colabconnect": {"httpUrl": "http://127.0.0.1:8420/mcp"}}}`

Clients stdio uniquement (Claude Desktop…) — ColabConnect doit être lancé :

```json
{"mcpServers": {"colabconnect": {"command": "uv", "args": ["--directory", "<dossier CyColabConnect>/backend", "run", "colabconnect", "mcp-stdio"]}}}
```

## Mises à jour

ColabConnect vérifie les [releases GitHub](https://github.com/MrMybal/CyColabConnect/releases)
au démarrage puis toutes les 6 h ; un badge apparaît dans la barre du haut quand une version
est disponible. **Paramètres → Mises à jour → Installer et redémarrer** télécharge l'archive,
vérifie son SHA-256, remplace `backend/src` et `frontend/dist`, puis le lanceur redémarre l'app.
Vos données (`~/.colabconnect` : compte, notebooks, réglages) ne sont pas touchées.

- Tant que le dépôt est privé, renseignez un token GitHub *fine-grained* en lecture seule
  (Contents : Read) dans Paramètres, ou la variable `COLABCONNECT_GITHUB_TOKEN`.
- Depuis un clone git, l'app signale la nouvelle version mais n'écrase rien : `git pull`.

### Publier une version

```powershell
.\scripts\release.ps1 0.2.0
```

Le script met à jour la version, commit, crée le tag `v0.2.0` et le pousse ; la GitHub Action
`Release` lance les tests, construit l'interface et publie l'archive.

## Configuration

| Variable | Défaut |
|---|---|
| `COLABCONNECT_PORT` | `8420` |
| `COLABCONNECT_HOME` | `~/.colabconnect` (runtimes, notebooks, réglages ; le jeton Google est dans le Gestionnaire d'identifiants Windows / trousseau système) |
| `COLABCONNECT_NOTEBOOKS` | `$COLABCONNECT_HOME/notebooks` |
| `COLABCONNECT_CELL_TIMEOUT` | `3600` secondes |
| `COLABCONNECT_GITHUB_TOKEN` | token GitHub pour les mises à jour (dépôt privé) |
| `COLABCONNECT_UPDATE_REPO` | `MrMybal/CyColabConnect` |

## Développement

```bash
cd backend && uv run pytest          # tests de bout en bout (faux Colab + vrai Jupyter local)
cd frontend && npm run dev           # UI sur :5173, proxy vers :8420
```

## Limites

- Les endpoints `/tun/m/*` sont ceux de l'interface web de Colab (non officiels, rétro-ingéniés
  par `google-colab-cli`) : ils peuvent changer. L'API officielle `colaboratory.googleapis.com`
  est en bêta sur liste blanche.
- Le serveur n'écoute que sur `127.0.0.1` et refuse les requêtes d'autres origines : il peut
  exécuter du code sur Colab avec vos identifiants Google.
- Les GPU consomment des unités de calcul : pensez à libérer les runtimes.

## Licence

[GPL-3.0-or-later](LICENSE). Une partie du client Colab est adaptée de
[google-colab-cli](https://github.com/googlecolab/google-colab-cli) (Apache-2.0, © Google LLC) :
voir [NOTICE](NOTICE). Projet non affilié à Google.
