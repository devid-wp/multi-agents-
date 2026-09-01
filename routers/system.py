"""routers/system.py — settings + health + legacy UI (вынесено из main.py 0.6.0)."""
from __future__ import annotations

import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from core.config import (
    DEFAULT_CRITIC_MODEL,
    DEFAULT_EXECUTOR_MODEL,
    DEFAULT_NVIDIA_URL,
    DEFAULT_OPENROUTER_URL,
    DEFAULT_PLANNER_MODEL,
    UserCredentials,
    settings,
)
from core.models import AgentProviderConfig, SettingsPayload, SettingsResponse
from core.session import get_credentials, mask_key, save_credentials

router = APIRouter(tags=["system"])

# templates needed for legacy /chat — initialized lazily to avoid circular import
_templates: Jinja2Templates | None = None


def _get_templates() -> Jinja2Templates:
    global _templates
    if _templates is None:
        from pathlib import Path

        base = Path(__file__).resolve().parent.parent
        _templates = Jinja2Templates(directory=str(base / "templates"))
    return _templates


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


# Legacy — deprecated, используйте /ui/ (Vite)
@router.get("/chat/", response_class=HTMLResponse, deprecated=True, include_in_schema=False)
@router.get("/chat", response_class=HTMLResponse, deprecated=True, include_in_schema=False)
async def legacy_chat(request: Request):
    creds = get_credentials(request)
    return _get_templates().TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "default_openrouter_url": DEFAULT_OPENROUTER_URL,
            "default_nvidia_url": DEFAULT_NVIDIA_URL,
            "default_ollama_url": "http://localhost:11434",
            "default_planner_model": (creds.planner and creds.planner.model_name) or DEFAULT_PLANNER_MODEL,
            "default_critic_model": (creds.critic and creds.critic.model_name) or DEFAULT_CRITIC_MODEL,
            "default_executor_model": (creds.executor and creds.executor.model_name) or DEFAULT_EXECUTOR_MODEL,
        },
    )
