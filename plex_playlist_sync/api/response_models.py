"""Shared base for typed API response models.

``ApiModel`` doubles as an output filter and a test-time completeness check:

* Production (``extra="ignore"``): keys not declared on a response model are dropped, so an
  undeclared key added to a handler's dict by accident is not serialised. This does not protect
  declared fields: a secret held in a declared field is still sent, so each route must keep
  redacting or masking those itself.
* Tests (``TRACKSEERR_STRICT_RESPONSES`` truthy, set in ``tests/conftest.py``):
  ``extra="forbid"`` makes any undeclared key raise, so an incomplete model fails the
  suite instead of silently dropping data.
"""

import os

from pydantic import BaseModel, ConfigDict

_TRUTHY = {"1", "true", "yes", "on"}


def _strict_enabled() -> bool:
    return os.environ.get("TRACKSEERR_STRICT_RESPONSES", "").strip().lower() in _TRUTHY


_EXTRA = "forbid" if _strict_enabled() else "ignore"


class ApiModel(BaseModel):
    """Base for every response model."""

    model_config = ConfigDict(extra=_EXTRA, from_attributes=True)
