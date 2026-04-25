from typing import Annotated

from fastapi import APIRouter, Depends
from lap_schemas.v1 import PairingCreateResponse

from app.deps import get_current_user, get_pairing_store
from infra.orm import User as UserRow
from infra.pairing_store import PairingStore

router = APIRouter(prefix="/api/v1/auth/pairing", tags=["pairing"])


@router.post("/create", response_model=PairingCreateResponse)
async def create_pairing(
    user: Annotated[UserRow, Depends(get_current_user)],
    store: Annotated[PairingStore, Depends(get_pairing_store)],
) -> PairingCreateResponse:
    token = store.create(user.id)
    return PairingCreateResponse(pairing_token=token)
