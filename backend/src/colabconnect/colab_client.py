"""Async client for Colab's runtime-allocation ("tunnel") API.

Port of google-colab-cli's ``client.py`` (Apache-2.0) to httpx/asyncio.
These endpoints are the ones the Colab web UI uses; they are not an official,
versioned API and may change.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from . import config
from .auth import AuthManager

XSSI_PREFIX = ")]}'\n"
TUN = "/tun/m"

GPU_ACCELERATORS = ("T4", "L4", "G4", "A100", "H100")
TPU_ACCELERATORS = ("V5E1", "V6E1")
ACCELERATORS = ("NONE",) + GPU_ACCELERATORS + TPU_ACCELERATORS
# These only exist in a single (high-memory) shape; shape=hm is ignored.
HIGH_MEM_ONLY = frozenset({"L4", "V5E1", "V6E1"})
_VARIANT_BY_INT = {0: "DEFAULT", 1: "GPU", 2: "TPU"}


class ColabApiError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def normalize_accelerator(value: str | None) -> str:
    acc = (value or "NONE").strip().upper()
    if acc in ("", "CPU"):
        acc = "NONE"
    if acc not in ACCELERATORS:
        raise ValueError(f"Accélérateur inconnu '{value}'. Valeurs : {', '.join(ACCELERATORS)}")
    return acc


def variant_for(accelerator: str) -> str:
    if accelerator in GPU_ACCELERATORS:
        return "GPU"
    if accelerator in TPU_ACCELERATORS:
        return "TPU"
    return "DEFAULT"


def _web_safe_nbh(u: uuid.UUID) -> str:
    s = str(u)
    return s.replace("-", "_") + "." * (44 - len(s))


@dataclass
class Assignment:
    endpoint: str
    accelerator: str
    variant: str
    high_mem: bool
    proxy_url: str
    proxy_token: str
    token_expires_at: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_api(cls, data: dict[str, Any], high_mem: bool | None = None) -> "Assignment":
        info = data["runtimeProxyInfo"]
        variant = data.get("variant", 0)
        if isinstance(variant, int):
            variant = _VARIANT_BY_INT.get(variant, "DEFAULT")
        shape = data.get("machineShape")
        if high_mem is None:
            high_mem = shape in (1, "HIGH_RAM")
        return cls(
            endpoint=data["endpoint"],
            accelerator=str(data.get("accelerator") or "NONE"),
            variant=str(variant),
            high_mem=bool(high_mem),
            proxy_url=info["url"],
            proxy_token=info["token"],
            token_expires_at=time.time() + int(info.get("tokenExpiresInSeconds", 3600)),
        )


class ColabClient:
    def __init__(self, auth: AuthManager, domain: str = config.COLAB_DOMAIN) -> None:
        self.auth = auth
        self.domain = domain
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))

    async def aclose(self) -> None:
        await self.http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> Any:
        token = await self.auth.access_token()
        url = path if path.startswith("http") else self.domain + path
        params = {**(params or {}), "authuser": "0"}
        hdrs = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Colab-Client-Agent": "colab-cli",
            **(headers or {}),
        }
        resp = await self.http.request(method, url, params=params, headers=hdrs, **kwargs)
        if resp.status_code >= 400:
            raise ColabApiError(
                f"{method} {path} → HTTP {resp.status_code}: {resp.reason_phrase}",
                status=resp.status_code,
                body=resp.text[:2000],
            )
        body = resp.text
        if body.startswith(XSSI_PREFIX):
            body = body[len(XSSI_PREFIX):]
        if not body.strip():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    # -- runtimes ----------------------------------------------------------

    async def assign(self, accelerator: str = "NONE", high_mem: bool = False) -> Assignment:
        accelerator = normalize_accelerator(accelerator)
        params: dict[str, str] = {"nbh": _web_safe_nbh(uuid.uuid4())}
        if accelerator != "NONE":
            params["variant"] = variant_for(accelerator)
            params["accelerator"] = accelerator
        if high_mem and accelerator not in HIGH_MEM_ONLY:
            params["shape"] = "hm"

        first = await self.request("GET", f"{TUN}/assign", params=params)
        if isinstance(first, dict) and "endpoint" in first:
            return Assignment.from_api(first, high_mem=high_mem)
        try:
            data = await self.request(
                "POST",
                f"{TUN}/assign",
                params=params,
                headers={"X-Goog-Colab-Token": first["token"]},
            )
        except ColabApiError as e:
            if e.status == 412:
                raise ColabApiError(
                    "Trop de runtimes actifs ou capacité indisponible (HTTP 412). "
                    "Arrêtez un runtime existant ou réessayez plus tard.",
                    412,
                    e.body,
                ) from e
            if e.status == 400 and accelerator != "NONE":
                raise ColabApiError(
                    f"L'accélérateur {accelerator} n'est pas disponible pour ce compte "
                    "(quota ou abonnement). Essayez T4 ou un runtime CPU.",
                    400,
                    e.body,
                ) from e
            raise
        return Assignment.from_api(data, high_mem=high_mem)

    async def list_assignments(self) -> list[Assignment]:
        data = await self.request("GET", f"{TUN}/assignments") or {}
        return [Assignment.from_api(a) for a in data.get("assignments", [])]

    async def unassign(self, endpoint: str) -> None:
        path = f"{TUN}/unassign/{endpoint}"
        first = await self.request("GET", path) or {}
        await self.request("POST", path, headers={"X-Goog-Colab-Token": first.get("token", "")})

    async def ccu_info(self) -> dict[str, Any]:
        data = await self.request("GET", f"{TUN}/ccu-info") or {}
        return {
            "balance": data.get("currentBalance"),
            "rate_per_hour": data.get("consumptionRateHourly"),
            "assignments": data.get("assignmentsCount"),
        }

    async def user_email(self) -> str | None:
        token = await self.auth.access_token()
        resp = await self.http.get(
            "https://oauth2.googleapis.com/tokeninfo", params={"access_token": token}
        )
        if resp.status_code == 200:
            return resp.json().get("email")
        return None

    # -- Google Drive credential propagation (drive.mount) ------------------

    async def propagate_drive_credentials(self, endpoint: str, dry_run: bool) -> dict[str, Any]:
        path = f"{TUN}/credentials-propagation/{endpoint}"
        params = {
            "authtype": "dfs_ephemeral",
            "version": "2",
            "dryrun": "true" if dry_run else "false",
            "propagate": "true",
            "record": "false",
        }
        first = await self.request("GET", path, params=params) or {}
        data = await self.request(
            "POST",
            path,
            params=params,
            headers={"x-goog-colab-token": first.get("token", "")},
            files={"file_id": (None, "empty.ipynb")},
        )
        return data if isinstance(data, dict) else {}

    def browser_url(self, endpoint: str) -> str:
        return (
            f"{self.domain}/notebooks/empty.ipynb?dbu=%2Ftun%2Fm%2F{endpoint}"
            f"#datalabBackendUrl={self.domain}/tun/m/{endpoint}"
        )
