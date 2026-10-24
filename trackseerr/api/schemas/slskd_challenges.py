"""Response/request models for ``/api/slskd-challenges`` (``api/routes/slskd_challenges.py``)."""

from typing import Optional

from pydantic import Field

from trackseerr.api.response_models import ApiModel


class SlskdChallenge(ApiModel):
    id: str
    client_id: str
    username: str
    message: str
    download_id: Optional[str] = None
    download_title: Optional[str] = None
    status: str
    received_at: str
    expires_at: Optional[str] = None
    replied_at: Optional[str] = None


class SlskdChallengeList(ApiModel):
    items: list[SlskdChallenge]


class SlskdChallengeReply(ApiModel):
    text: str = Field(min_length=1, max_length=200)
