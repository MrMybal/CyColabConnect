"""Local .ipynb notebook store.

Notebooks are plain nbformat v4 files in ``NOTEBOOKS_DIR``. Per-notebook
runtime settings live in ``metadata.colabconnect``.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any

import nbformat

from . import config
from .events import bus

DEFAULT_SETTINGS: dict[str, Any] = {
    "accelerator": "NONE",
    "high_mem": False,
    "pip_packages": [],
    "env": {},
    "setup_code": "",
    "runtime_endpoint": None,
}

_SAFE_NAME = re.compile(r"[^\w\-. ]+", re.UNICODE)


class NotebookNotFound(Exception):
    pass


class CellNotFound(Exception):
    pass


def _new_cell_id() -> str:
    return uuid.uuid4().hex[:12]


def _make_cell(cell_type: str, source: str) -> Any:
    if cell_type == "markdown":
        cell = nbformat.v4.new_markdown_cell(source)
    elif cell_type == "code":
        cell = nbformat.v4.new_code_cell(source)
    else:
        raise ValueError("cell_type doit être 'code' ou 'markdown'")
    cell["id"] = _new_cell_id()
    return cell


def _source(cell: Any) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else src


class NotebookStore:
    def __init__(self, root: Path = config.NOTEBOOKS_DIR) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    # -- paths -----------------------------------------------------------------

    def _path(self, notebook_id: str) -> Path:
        name = notebook_id if notebook_id.endswith(".ipynb") else notebook_id + ".ipynb"
        path = (self.root / name).resolve()
        if path.parent != self.root.resolve():
            raise NotebookNotFound(f"Identifiant de notebook invalide : {notebook_id}")
        return path

    @staticmethod
    def _id(path: Path) -> str:
        return path.stem

    def lock(self, notebook_id: str) -> asyncio.Lock:
        return self._locks.setdefault(notebook_id, asyncio.Lock())

    # -- read / write ----------------------------------------------------------

    def load(self, notebook_id: str) -> Any:
        path = self._path(notebook_id)
        if not path.exists():
            raise NotebookNotFound(f"Notebook introuvable : {notebook_id}")
        nb = nbformat.read(str(path), as_version=4)
        changed = False
        for cell in nb.cells:
            if not cell.get("id"):
                cell["id"] = _new_cell_id()
                changed = True
        if changed:
            nb.nbformat_minor = max(nb.nbformat_minor, 5)
        return nb

    def save(self, notebook_id: str, nb: Any, *, source: str = "ui") -> None:
        path = self._path(notebook_id)
        nb.nbformat_minor = max(nb.nbformat_minor, 5)
        tmp = path.with_suffix(".ipynb.tmp")
        nbformat.write(nb, str(tmp))
        tmp.replace(path)
        bus.publish("notebook_changed", notebook_id=notebook_id, source=source)

    def settings(self, nb: Any) -> dict[str, Any]:
        stored = nb.metadata.get("colabconnect", {})
        return {**DEFAULT_SETTINGS, **stored}

    # -- notebook-level operations ---------------------------------------------

    def list(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.root.glob("*.ipynb"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                nb = nbformat.read(str(path), as_version=4)
            except Exception:
                continue
            out.append({
                "id": self._id(path),
                "name": path.stem,
                "modified": path.stat().st_mtime,
                "cells": len(nb.cells),
                "settings": self.settings(nb),
            })
        return out

    def create(
        self,
        name: str,
        cells: list[dict[str, str]] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> str:
        base = _SAFE_NAME.sub("_", name.strip().removesuffix(".ipynb")).strip(" .") or "notebook"
        notebook_id = base
        n = 1
        while self._path(notebook_id).exists():
            n += 1
            notebook_id = f"{base}_{n}"
        nb = nbformat.v4.new_notebook()
        nb.nbformat_minor = 5
        nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
        nb.metadata["language_info"] = {"name": "python"}
        nb.metadata["colabconnect"] = {**DEFAULT_SETTINGS, **(settings or {}), "runtime_endpoint": None}
        for c in cells or [{"cell_type": "code", "source": ""}]:
            nb.cells.append(_make_cell(c.get("cell_type", "code"), c.get("source", "")))
        self.save(notebook_id, nb, source="create")
        return notebook_id

    def import_notebook(self, name: str, content: bytes) -> str:
        nb = nbformat.reads(content.decode("utf-8"), as_version=4)
        notebook_id = self.create(name, cells=[])
        nb.metadata.setdefault("colabconnect", {})
        nb.metadata["colabconnect"] = {**DEFAULT_SETTINGS, **nb.metadata["colabconnect"], "runtime_endpoint": None}
        for cell in nb.cells:
            if not cell.get("id"):
                cell["id"] = _new_cell_id()
        self.save(notebook_id, nb, source="import")
        return notebook_id

    def delete(self, notebook_id: str) -> None:
        path = self._path(notebook_id)
        if not path.exists():
            raise NotebookNotFound(notebook_id)
        path.unlink()
        bus.publish("notebook_deleted", notebook_id=notebook_id)

    def rename(self, notebook_id: str, new_name: str) -> str:
        src = self._path(notebook_id)
        new_id = _SAFE_NAME.sub("_", new_name.strip().removesuffix(".ipynb")).strip(" .")
        if not new_id:
            raise ValueError("Nom invalide")
        dst = self._path(new_id)
        if dst.exists():
            raise ValueError(f"Un notebook '{new_id}' existe déjà")
        src.rename(dst)
        bus.publish("notebook_deleted", notebook_id=notebook_id)
        bus.publish("notebook_changed", notebook_id=new_id, source="rename")
        return new_id

    def update_settings(self, notebook_id: str, **changes: Any) -> dict[str, Any]:
        nb = self.load(notebook_id)
        current = self.settings(nb)
        current.update({k: v for k, v in changes.items() if k in DEFAULT_SETTINGS})
        nb.metadata["colabconnect"] = current
        self.save(notebook_id, nb, source="settings")
        return current

    def to_json(self, notebook_id: str) -> dict[str, Any]:
        nb = self.load(notebook_id)
        return {
            "id": notebook_id,
            "name": notebook_id,
            "settings": self.settings(nb),
            "cells": [
                {
                    "id": c["id"],
                    "cell_type": c["cell_type"],
                    "source": _source(c),
                    "outputs": c.get("outputs", []) if c["cell_type"] == "code" else [],
                    "execution_count": c.get("execution_count") if c["cell_type"] == "code" else None,
                }
                for c in nb.cells
            ],
        }

    def raw(self, notebook_id: str) -> str:
        return nbformat.writes(self.load(notebook_id))

    # -- cell operations ---------------------------------------------------------

    @staticmethod
    def _find(nb: Any, cell_id: str) -> int:
        for i, c in enumerate(nb.cells):
            if c.get("id") == cell_id:
                return i
        raise CellNotFound(f"Cellule introuvable : {cell_id}")

    def add_cell(
        self, notebook_id: str, source: str = "", cell_type: str = "code", index: int | None = None,
        *, origin: str = "ui",
    ) -> str:
        nb = self.load(notebook_id)
        cell = _make_cell(cell_type, source)
        if index is None or index >= len(nb.cells):
            nb.cells.append(cell)
        else:
            nb.cells.insert(max(0, index), cell)
        self.save(notebook_id, nb, source=origin)
        return cell["id"]

    def update_cell(
        self, notebook_id: str, cell_id: str, source: str | None = None,
        cell_type: str | None = None, *, origin: str = "ui",
    ) -> None:
        nb = self.load(notebook_id)
        i = self._find(nb, cell_id)
        cell = nb.cells[i]
        if cell_type and cell_type != cell["cell_type"]:
            new = _make_cell(cell_type, _source(cell) if source is None else source)
            new["id"] = cell_id
            nb.cells[i] = new
        elif source is not None:
            cell["source"] = source
        self.save(notebook_id, nb, source=origin)

    def delete_cell(self, notebook_id: str, cell_id: str, *, origin: str = "ui") -> None:
        nb = self.load(notebook_id)
        nb.cells.pop(self._find(nb, cell_id))
        self.save(notebook_id, nb, source=origin)

    def move_cell(self, notebook_id: str, cell_id: str, new_index: int, *, origin: str = "ui") -> None:
        nb = self.load(notebook_id)
        cell = nb.cells.pop(self._find(nb, cell_id))
        nb.cells.insert(max(0, min(new_index, len(nb.cells))), cell)
        self.save(notebook_id, nb, source=origin)

    def get_cell(self, notebook_id: str, cell_id: str) -> Any:
        nb = self.load(notebook_id)
        return nb.cells[self._find(nb, cell_id)]

    def set_outputs(
        self, notebook_id: str, cell_id: str, outputs: list[dict[str, Any]],
        execution_count: int | None,
    ) -> None:
        nb = self.load(notebook_id)
        cell = nb.cells[self._find(nb, cell_id)]
        cell["outputs"] = [nbformat.from_dict(o) for o in outputs]
        cell["execution_count"] = execution_count
        cell.setdefault("metadata", {})["colabconnect_last_run"] = time.time()
        self.save(notebook_id, nb, source="execution")

    def clear_outputs(self, notebook_id: str) -> None:
        nb = self.load(notebook_id)
        for c in nb.cells:
            if c["cell_type"] == "code":
                c["outputs"] = []
                c["execution_count"] = None
        self.save(notebook_id, nb, source="clear")
