"""Google ID-token verification for the public read-only workspace."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Header, HTTPException

from src.config import Config, get_config
from src.workspace.service import WorkspaceAccessError, WorkspaceRepository


class WorkspaceAuth:
    def __init__(self, config: Optional[Config] = None):
        self._config = config or get_config()
        self._repo = WorkspaceRepository(self._config)

    def identity(self, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Google sign-in is required.")
        client_id = self._config.secrets.google_oauth_client_id
        if not client_id:
            raise HTTPException(status_code=503, detail="Workspace Google sign-in is not configured.")
        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            token = authorization.removeprefix("Bearer ").strip()
            payload = id_token.verify_oauth2_token(token, google_requests.Request(), client_id)
            if not payload.get("email_verified") or not payload.get("email"):
                raise ValueError("A verified Google email is required.")
            return self._repo.user_for_email(
                str(payload["email"]),
                display_name=str(payload.get("name") or ""),
                avatar_url=str(payload.get("picture") or ""),
            )
        except WorkspaceAccessError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Google sign-in could not be verified.") from exc
