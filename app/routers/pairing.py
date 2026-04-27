import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import (
    PairingCreateResponse,
    PairingRedeemRequest,
    PairingRedeemResponse,
)

from app.deps import get_current_user, get_pairing_store
from app.error_codes import ErrorCode
from infra.orm import User as UserRow
from infra.pairing_store import PairingStoreProtocol

router = APIRouter(prefix="/api/v1/auth/pairing", tags=["pairing"])


@router.post("/create", response_model=PairingCreateResponse)
async def create_pairing(
    user: Annotated[UserRow, Depends(get_current_user)],
    store: Annotated[PairingStoreProtocol, Depends(get_pairing_store)],
) -> PairingCreateResponse:
    token = store.create(user.id)
    return PairingCreateResponse(pairing_token=token)


@router.post("/redeem", response_model=PairingRedeemResponse)
async def redeem_pairing(
    payload: PairingRedeemRequest,
    store: Annotated[PairingStoreProtocol, Depends(get_pairing_store)],
) -> PairingRedeemResponse:
    user_id = store.consume(payload.pairing_token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ErrorCode.INVALID_PAIRING_TOKEN,
        )
    refresh_token = secrets.token_urlsafe(32)
    return PairingRedeemResponse(refresh_token=refresh_token)
