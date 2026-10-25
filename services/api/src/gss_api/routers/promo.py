"""只读促销资格查询；session 仅在本次请求内传递。"""

from __future__ import annotations

import asyncio as _asyncio

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from gss_contracts.errors import DomainError, ErrorCode
from ..runtime import build_pipeline
from ..schemas import ErrorOut

router = APIRouter(prefix="/api", tags=["promo"])


class PromoEligibilityRequest(BaseModel):
    session: str = Field(min_length=1)


class PromoCampaignOut(BaseModel):
    key: str
    campaign_id: str
    discount_percentage: float | None = None
    duration_num_periods: int | None = None
    duration_period: str | None = None
    title: str | None = None
    summary: str | None = None
    promotion_type: str | None = None


class PromoEligibilityOut(BaseModel):
    account_id: str | None = None
    current_plan_type: str | None = None
    subscription_plan: str | None = None
    is_free: bool
    plus_eligible: bool
    campaigns: list[PromoCampaignOut]


def _error(exc: DomainError) -> HTTPException:
    code = exc.code
    status_code = status.HTTP_401_UNAUTHORIZED if code is ErrorCode.INVALID_SESSION else status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail=ErrorOut(code=str(code), message=exc.message).model_dump(),
    )


@router.post(
    "/promo/eligibility",
    response_model=PromoEligibilityOut,
    responses={400: {"model": ErrorOut}, 401: {"model": ErrorOut}},
)
async def promo_eligibility(payload: PromoEligibilityRequest) -> PromoEligibilityOut:
    pipeline = build_pipeline()
    result = None
    for _attempt in range(3):
        try:
            result = await pipeline.deps.account.promo_eligibility(payload.session.strip())
            break
        except DomainError as exc:
            raise _error(exc)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ErrorOut(code=str(ErrorCode.INVALID_SESSION), message=str(exc)).model_dump(),
            )
        except Exception as exc:  # noqa: BLE001
            if not any(k in str(exc) for k in (
                    "SSL", "EOF", "ConnectError", "Connection closed",
                    "CurlError", "TLS", "timed out", "reset by peer")):
                raise
            if _attempt == 2:
                raise
            await _asyncio.sleep(2)
    return PromoEligibilityOut(
        account_id=result.account_id,
        current_plan_type=result.current_plan_type,
        subscription_plan=result.subscription_plan,
        is_free=result.is_free,
        plus_eligible=result.plus_eligible,
        campaigns=[
            PromoCampaignOut(
                key=item.key,
                campaign_id=item.campaign_id,
                discount_percentage=item.discount_percentage,
                duration_num_periods=item.duration_num_periods,
                duration_period=item.duration_period,
                title=item.title,
                summary=item.summary,
                promotion_type=item.promotion_type,
            )
            for item in result.campaigns
        ],
    )


__all__ = ["router"]
