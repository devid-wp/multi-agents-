"""routers/system.py — settings + health (legacy /chat удалён в 0.7.1, Vite /ui/ primary)."""
from __future__ import annotations

from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from core.config import UserCredentials
from core.models import AgentProviderConfig, SettingsPayload, SettingsResponse
from core.session import get_credentials, mask_key, save_credentials

router = APIRouter(tags=["system"])


def _validate_url(value: Optional[str]) -> Optional[str]:
    if value is None or value.strip() == "":
        return None
    v = value.strip()
    if not (v.startswith("http://") or v.startswith("https://")):
        raise HTTPException(status_code=400, detail=f"Invalid URL: {v!r}. Must start with http:// or https://")
    return v.rstrip("/") or v


@router.get("/api/settings", response_model=SettingsResponse)
async def read_settings(request: Request):
    creds = get_credentials(request)

    def _map_agent(cfg):
        if not cfg:
            return None
        return {
            "provider": cfg.provider,
            "has_key": bool(cfg.api_key and cfg.api_key.strip()),
            "key_masked": mask_key(cfg.api_key),
            "base_url": cfg.base_url,
            "model_name": cfg.model_name,
        }

    return SettingsResponse(
        planner=_map_agent(creds.planner),
        executor=_map_agent(creds.executor),
        critic=_map_agent(creds.critic),
    )


@router.post("/api/settings")
async def write_settings(request: Request, payload: SettingsPayload):
    current = get_credentials(request)

    def _merge(old, new):
        if not new:
            return old
        return AgentProviderConfig(
            provider=new.provider or (old.provider if old else "nvidia"),
            api_key=new.api_key if new.api_key not in (None, "") else (old.api_key if old else None),
            base_url=_validate_url(new.base_url) or (old.base_url if old else None),
            model_name=new.model_name or (old.model_name if old else None),
        )

    new_creds = UserCredentials(
        planner=_merge(current.planner, payload.planner),
        critic=_merge(current.critic, payload.critic),
        executor=_merge(current.executor, payload.executor),
    )
    signed = save_credentials(new_creds)

    def _resp(cfg):
        if not cfg:
            return None
        return {
            "provider": cfg.provider,
            "has_key": bool(cfg.api_key and cfg.api_key.strip()),
            "key_masked": mask_key(cfg.api_key),
            "base_url": cfg.base_url,
            "model_name": cfg.model_name,
        }

    resp = JSONResponse(
        {
            "ok": True,
            "planner": _resp(new_creds.planner),
            "critic": _resp(new_creds.critic),
            "executor": _resp(new_creds.executor),
        }
    )
    resp.set_cookie(key="trinity_session", value=signed, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@router.get("/api/health")
async def health():
    required_model = "qwen2.5-coder:1.5b"
    ollama = {
        "available": False,
        "model_installed": False,
        "required_model": required_model,
        "start_command": "ollama serve",
        "pull_command": f"ollama pull {required_model}",
    }
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            response = await client.get("http://localhost:11434/api/tags")
            response.raise_for_status()
            names = {item.get("name") for item in response.json().get("models", [])}
            ollama["available"] = True
            ollama["model_installed"] = required_model in names
    except (httpx.HTTPError, ValueError):
        pass
    return {"ok": True, "service": "trinity", "ollama": ollama}
