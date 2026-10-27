"""API tokens (audit F-12). Every /v1 route needs `Authorization: Bearer <token>`.

Tokens come from CERTGEN_API_TOKENS, or one is generated on first start into `{data_dir}/api-token`
(mode 0600) and reused afterwards. Only SHA-256 digests are kept in memory; a request's client id
(the first 16 hex digits of its token's digest) scopes its Idempotency-Keys.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

from certgen.config import validate_token
from certgen.storage import O_NOFOLLOW

log = logging.getLogger("certgen.auth")

TOKEN_FILE_NAME = "api-token"
CLIENT_ID_LENGTH = 16
_READ_RETRIES = 50  # another process may have created the file and not written it yet


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """The accepted tokens' digests; filled at startup, before the first request is served."""

    def __init__(self) -> None:
        self.digests: tuple[str, ...] = ()

    def set_tokens(self, tokens: tuple[str, ...]) -> None:
        self.digests = tuple(digest(t) for t in tokens)

    def client_for(self, presented: str) -> str | None:
        """Client id for a valid token, else None. Compares every digest in constant time."""
        candidate = digest(presented)
        match = None
        for known in self.digests:
            if hmac.compare_digest(known, candidate):
                match = known
        return match[:CLIENT_ID_LENGTH] if match is not None else None


def load_or_create_token(path: Path) -> str:
    """The token in `path`, creating it (exclusively, 0600) with a random token if it does not exist."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW, 0o600)
    except FileExistsError:
        return _read_token(path)
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(token + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    log.warning("api_token_created path=%s (send it as 'Authorization: Bearer <token>')", path)
    return token


def _read_token(path: Path) -> str:
    for _ in range(_READ_RETRIES):
        fd = os.open(path, os.O_RDONLY | O_NOFOLLOW)
        with os.fdopen(fd, encoding="ascii") as fh:
            token = fh.read().strip()
        if token:
            return validate_token(token)
        time.sleep(0.02)
    raise RuntimeError(f"{path} is empty; delete it to generate a new API token")
