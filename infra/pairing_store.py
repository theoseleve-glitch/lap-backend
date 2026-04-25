"""In-memory pairing token store.

T089 ships a process-local store that satisfies the create-and-return-token
contract. T091 will replace the implementation with a Redis-backed store
that enforces the 5-minute TTL and single-use semantics across processes.
The public method shape is the contract; storage is an implementation
detail callers should not rely on.
"""

import secrets
from uuid import UUID


class PairingStore:
    def __init__(self) -> None:
        self._tokens: dict[str, UUID] = {}

    def create(self, user_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = user_id
        return token

    def consume(self, token: str) -> UUID | None:
        return self._tokens.pop(token, None)
