"""Pydantic schemas for per-user media-server account logins."""

from __future__ import annotations

from pydantic import BaseModel, Field

from trackseerr.api.response_models import ApiModel


class AccountMediaServerItem(ApiModel):
    server_id: str
    name: str
    username: str
    saved: bool


class AccountMediaServerPayload(BaseModel):
    username: str = Field(..., max_length=256)
    password: str = Field(..., max_length=1024)


class AccountMediaServerDeletedResponse(ApiModel):
    status: str
    server_id: str
