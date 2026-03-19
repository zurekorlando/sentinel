"""
Exclusion templates router.

Endpoints
---------
GET  /api/exclusion-templates   List built-in + custom templates.
POST /api/exclusion-templates   Save a custom template.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException

from api.schemas import ExclusionTemplate

router = APIRouter(prefix="/api/exclusion-templates", tags=["exclusions"])

# ── Built-in templates ────────────────────────────────────────────────────────

_BUILTIN: List[ExclusionTemplate] = [
    ExclusionTemplate(
        id="os_windows",
        name="Windows OS Artifacts",
        description="Temporary files, caches, and system artifacts on Windows",
        patterns=[
            "Thumbs.db", "desktop.ini", "$RECYCLE.BIN", "pagefile.sys",
            "hiberfil.sys", "swapfile.sys", "*.tmp", "*.temp",
            "AppData/Local/Temp/**", "Windows/Temp/**",
        ],
        builtin=True,
    ),
    ExclusionTemplate(
        id="os_linux",
        name="Linux OS Artifacts",
        description="Temporary files, caches, and system artifacts on Linux",
        patterns=[
            "/proc/**", "/sys/**", "/dev/**", "/run/**", "/tmp/**",
            "*.swp", "*.swo", ".DS_Store", "*~",
        ],
        builtin=True,
    ),
    ExclusionTemplate(
        id="dev_common",
        name="Developer Artifacts",
        description="Build directories, dependency caches, and IDE files",
        patterns=[
            "node_modules/**", ".git/**", "__pycache__/**", "*.pyc",
            ".pytest_cache/**", "dist/**", "build/**", "*.egg-info/**",
            ".venv/**", "venv/**", ".tox/**", "target/**",
            ".idea/**", ".vscode/**",
        ],
        builtin=True,
    ),
    ExclusionTemplate(
        id="media",
        name="Large Media Files",
        description="Video, audio, and image files over typical backup size",
        patterns=[
            "*.mp4", "*.mkv", "*.avi", "*.mov", "*.wmv",
            "*.mp3", "*.flac", "*.wav", "*.aac",
            "*.iso", "*.img",
        ],
        builtin=True,
    ),
    ExclusionTemplate(
        id="logs",
        name="Log Files",
        description="Application and system log files",
        patterns=[
            "*.log", "*.log.*", "logs/**", "log/**",
            "*.out", "*.err",
        ],
        builtin=True,
    ),
]

# ── Custom template storage ────────────────────────────────────────────────────

def _custom_templates_path() -> Path:
    base = os.getenv("SENTINEL_JOBS_DIR", "jobs")
    return Path(base) / "sentinel_templates.json"


def _load_custom() -> List[ExclusionTemplate]:
    path = _custom_templates_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
        return [ExclusionTemplate(**t) for t in raw]
    except Exception:
        return []


def _save_custom(templates: List[ExclusionTemplate]) -> None:
    path = _custom_templates_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([t.model_dump() for t in templates], indent=2))


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[ExclusionTemplate])
async def list_templates():
    """Return all built-in templates plus any custom templates."""
    return _BUILTIN + _load_custom()


@router.post("", response_model=ExclusionTemplate, status_code=201)
async def create_template(body: ExclusionTemplate):
    """
    Save a custom exclusion template.

    The ``id`` field must be unique and must not conflict with built-in IDs.
    Returns HTTP 409 if the ID already exists.
    """
    builtin_ids = {t.id for t in _BUILTIN}
    if body.id in builtin_ids:
        raise HTTPException(
            status_code=409,
            detail=f"Template id '{body.id}' conflicts with a built-in template",
        )

    custom = _load_custom()
    for i, t in enumerate(custom):
        if t.id == body.id:
            # Replace existing custom template
            custom[i] = body
            break
    else:
        custom.append(body)

    body = ExclusionTemplate(
        id=body.id,
        name=body.name,
        description=body.description,
        patterns=body.patterns,
        builtin=False,
    )
    _save_custom(custom)
    return body


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: str):
    """
    Delete a custom template by ID.

    Returns HTTP 404 if not found, HTTP 400 if it is a built-in template.
    """
    builtin_ids = {t.id for t in _BUILTIN}
    if template_id in builtin_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete built-in template '{template_id}'",
        )

    custom = _load_custom()
    new_custom = [t for t in custom if t.id != template_id]
    if len(new_custom) == len(custom):
        raise HTTPException(
            status_code=404,
            detail=f"Template '{template_id}' not found",
        )
    _save_custom(new_custom)
