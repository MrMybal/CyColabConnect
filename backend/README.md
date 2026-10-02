# colabconnect (backend)

Serveur FastAPI + MCP de ColabConnect. Voir le [README principal](../README.md).

- `colabconnect serve` : interface web et MCP (`/mcp`) sur http://127.0.0.1:8420
- `colabconnect mcp-stdio` : proxy MCP stdio vers un `serve` lancé
- `uv run pytest` : tests de bout en bout (faux Colab + vrai Jupyter local)
