"""slskd sharer chat challenges: list, answer (admin-typed text only) and dismiss. Admin only, core tier, native mode."""

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status

from trackseerr import slskd_challenges
from trackseerr.acquisition_worker import _research_after_failure
from trackseerr.api.dependencies import get_db, require_admin, require_core_tier
from trackseerr.api.routes.library._shared import native_only
from trackseerr.api.schemas.slskd_challenges import SlskdChallenge, SlskdChallengeList, SlskdChallengeReply
from trackseerr.storage import Database

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin), Depends(require_core_tier), Depends(native_only)])


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """Only the declared response fields; keeps ``reply_text`` and ``message_id`` off the wire."""
    return {name: row.get(name) for name in SlskdChallenge.model_fields}


@router.get("", response_model=SlskdChallengeList, summary="Sharers asking for a chat reply")
def list_challenges(
    status: Literal["open", "replied", "dismissed", "expired", "all"] = "open",
    db: Database = Depends(get_db),
) -> Any:
    return {"items": [_public(r) for r in db.list_slskd_challenges(None if status == "all" else status)]}


@router.post("/{challenge_id}/reply", response_model=SlskdChallenge, summary="Send the admin's reply to a sharer")
def reply_challenge(challenge_id: str, body: SlskdChallengeReply, db: Database = Depends(get_db)) -> Any:
    try:
        return _public(
            slskd_challenges.reply_to_challenge(
                db, challenge_id, body.text, datetime.now(timezone.utc), _research_after_failure
            )
        )
    except LookupError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.warning("slskd refused challenge reply %s: %s", challenge_id, exc)
        raise HTTPException(status_code=http_status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/{challenge_id}/dismiss", response_model=SlskdChallenge, summary="Dismiss a sharer's chat request")
def dismiss_challenge(challenge_id: str, db: Database = Depends(get_db)) -> Any:
    try:
        return _public(slskd_challenges.dismiss_challenge(db, challenge_id))
    except LookupError as exc:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
