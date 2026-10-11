"""Certificate files on disk (§11). Every path component is a server-generated 32-hex ID, asserted here."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _check_id(value: str) -> str:
    # fullmatch, not match: "$" would accept a trailing newline.
    if not HEX32.fullmatch(value):
        raise ValueError("path component must be 32 lowercase hex characters")
    return value


class Storage:
    def __init__(self, certificates_dir: Path) -> None:
        self.root = certificates_dir

    def path_for(self, job_id: str, certificate_id: str) -> Path:
        return self.root / _check_id(job_id) / f"{_check_id(certificate_id)}.pdf"

    def save_atomic(self, job_id: str, certificate_id: str, data: bytes) -> None:
        """Write to a unique temp file, fsync, then rename: readers see the old file or the whole new one."""
        final = self.path_for(job_id, certificate_id)
        final.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A unique name per attempt, so a stale worker and the current one never share a temp file.
        tmp = final.parent / f"{certificate_id}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if os.name == "posix":
            # Persist the rename itself; without this a power loss can forget the directory entry.
            dir_fd = os.open(final.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def exists(self, job_id: str, certificate_id: str) -> bool:
        return self.path_for(job_id, certificate_id).is_file()
