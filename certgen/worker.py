"""Background worker: claim one item, render outside any transaction, publish with a fenced update (§7)."""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import text

from certgen.clock import Clock
from certgen.config import Settings
from certgen.db import Engines
from certgen.rendering import CertificateData
from certgen.storage import Storage

log = logging.getLogger("certgen.worker")

Renderer = Callable[[CertificateData], bytes]


@dataclass(frozen=True)
class Claim:
    item_id: int
    token: str
    attempts: int


class Worker:
    def __init__(
        self,
        engines: Engines,
        storage: Storage,
        settings: Settings,
        clock: Clock,
        renderer: Renderer,
        worker_id: str | None = None,
    ) -> None:
        self.engines = engines
        self.storage = storage
        self.settings = settings
        self.clock = clock
        self.renderer = renderer
        self.worker_id = worker_id or uuid.uuid4().hex  # logs only; never part of a guard

    def run_until_idle(self) -> list[int]:
        """Process until nothing is claimable. Used by tests and the benchmark: no sleeping."""
        processed = []
        while (item_id := self.run_once()) is not None:
            processed.append(item_id)
        return processed

    def run_once(self) -> int | None:
        claim = self.claim_next()
        if claim is None:
            return None
        self.process(claim)
        return claim.item_id

    def process(self, claim: Claim) -> None:
        data = self.load_certificate_data(claim.item_id)
        pdf = self.renderer(data)  # no DB transaction is open here
        self.storage.save_atomic(data.job_id, data.certificate_id, pdf)
        self.record_success(claim)

    def claim_next(self) -> Claim | None:
        with self.engines.write.begin() as conn:
            now = self.clock()
            item_id = conn.execute(
                text(
                    "SELECT id FROM items WHERE status = 'pending' "
                    "AND (available_at IS NULL OR available_at <= :now) ORDER BY id LIMIT 1"
                ),
                {"now": now},
            ).scalar()
            if item_id is None:
                return None
            token = secrets.token_hex(16)
            row = conn.execute(
                text(
                    "UPDATE items SET status = 'processing', lease_owner = :worker_id, lease_token = :token, "
                    "lease_expires_at = :now + :lease_ms, attempts = attempts + 1 "
                    "WHERE id = :id RETURNING id, attempts"
                ),
                {
                    "worker_id": self.worker_id,
                    "token": token,
                    "now": now,
                    "lease_ms": self.settings.lease_ms,
                    "id": item_id,
                },
            ).one()
        return Claim(item_id=row.id, token=token, attempts=row.attempts)

    def load_certificate_data(self, item_id: int) -> CertificateData:
        with self.engines.read.begin() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT i.job_id, i.certificate_id, i.name, i.email, j.title, j.issuer, j.issued_on, "
                        "j.print_email FROM items AS i JOIN jobs AS j ON j.id = i.job_id WHERE i.id = :id"
                    ),
                    {"id": item_id},
                )
                .mappings()
                .one()
            )
        return CertificateData(
            job_id=row["job_id"],
            certificate_id=row["certificate_id"],
            name=row["name"],
            email=row["email"] if row["print_email"] else None,
            title=row["title"],
            issuer=row["issuer"],
            issued_on=row["issued_on"],
        )

    def record_success(self, claim: Claim) -> None:
        """T5. The token is the fence: after any reclaim it no longer matches, so nothing changes."""
        with self.engines.write.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE items SET status = 'succeeded', lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, errors = NULL, available_at = NULL, completed_at = :now "
                    "WHERE id = :id AND lease_token = :token AND status = 'processing'"
                ),
                {"now": self.clock(), "id": claim.item_id, "token": claim.token},
            )
        if result.rowcount == 0:
            log.warning("lease_lost item_id=%s worker_id=%s", claim.item_id, self.worker_id)
