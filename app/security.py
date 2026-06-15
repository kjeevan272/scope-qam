"""
Security and RBAC layer.

Design:
  - API keys are SHA-256 hashed before storage (never store plaintext)
  - Role hierarchy: admin > analyst > viewer
  - FastAPI dependency `require_role("analyst")` guards endpoints
  - Tenant resolution from X-Tenant-ID header + key validation

Role permissions:
  admin   — full access: read, write, delete, manage tenants/rules/keys
  analyst — read + write uploads, snapshots, quality issues
  viewer  — read-only on all endpoints
"""
from __future__ import annotations

import hashlib
import secrets
from functools import lru_cache

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.db import get_db
from sqlalchemy.orm import Session

ROLE_HIERARCHY = {"admin": 3, "analyst": 2, "viewer": 1}

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Returns (raw_key, hashed_key). Store only the hash."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_key(raw)


def get_current_key(
    api_key: str | None = Security(_api_key_header),
    db: Session = Depends(get_db),
):
    """
    Resolve the ApiKey record from the X-API-Key header.
    Returns None if no key header present (unauthenticated — allowed in dev mode).
    """
    if not api_key:
        return None   # open access; production should set REQUIRE_AUTH=true

    from app.models import ApiKey
    from app.core.config import settings

    hashed = hash_key(api_key)
    record = db.query(ApiKey).filter_by(key_hash=hashed, is_active=True).first()
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Update last_used_at without blocking the request
    from datetime import datetime, timezone
    record.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return record


def require_role(minimum_role: str):
    """FastAPI dependency factory. Usage: Depends(require_role('analyst'))"""
    def _check(key=Depends(get_current_key)):
        if key is None:
            return  # dev mode — no auth
        if ROLE_HIERARCHY.get(key.role, 0) < ROLE_HIERARCHY.get(minimum_role, 0):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{key.role}' insufficient — requires '{minimum_role}'",
            )
        return key
    return _check


def get_tenant_id(key=Depends(get_current_key)) -> int | None:
    """Extract tenant_id from the resolved API key. None = default/public tenant."""
    if key is None:
        return None
    return key.tenant_id
