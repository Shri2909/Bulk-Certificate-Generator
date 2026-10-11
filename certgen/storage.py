"""Certificate files on disk (§11, §24.8). Every path component is a server-generated 32-hex ID.

Files are created private (dirs 0700, files 0600) and are never read through a symlink, so nothing
placed in the data directory by someone else can make a download serve a file from elsewhere.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import uuid
from pathlib import Path

log = logging.getLogger("certgen.storage")

HEX32 = re.compile(r"^[0-9a-f]{32}$")
# O_NOFOLLOW makes open() itself refuse a symlink, closing the gap between the lstat check and open.
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _check_id(value: str) -> str:
    # fullmatch, not match: "$" would accept a trailing newline.
    if not HEX32.fullmatch(value):
        raise ValueError("path component must be 32 lowercase hex characters")
    return value


class Storage:
    def __init__(self, certificates_dir: Path) -> None:
        self.root = certificates_dir.resolve()

    def path_for(self, job_id: str, certificate_id: str) -> Path:
        return self.root / _check_id(job_id) / f"{_check_id(certificate_id)}.pdf"

    def _job_dir_is_safe(self, job_dir: Path) -> bool:
        """The job dir must be a real directory directly under the root, not a symlink."""
        try:
            info = os.lstat(job_dir)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            log.warning("unsafe_job_dir path_kind=%s", "symlink" if stat.S_ISLNK(info.st_mode) else "other")
            return False
        return job_dir.resolve().parent == self.root

    def save_atomic(self, job_id: str, certificate_id: str, data: bytes) -> None:
        """Write to a unique temp file, fsync, then rename: readers see the old file or the whole new one."""
        final = self.path_for(job_id, certificate_id)
        final.parent.mkdir(mode=0o700, exist_ok=True)
        if not self._job_dir_is_safe(final.parent):
            raise OSError("refusing to write into an unsafe job directory")
        # A unique name per attempt, so a stale worker and the current one never share a temp file.
        tmp = final.parent / f"{certificate_id}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW, 0o600)
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

    def _safe_file(self, job_id: str, certificate_id: str) -> Path | None:
        path = self.path_for(job_id, certificate_id)
        if not self._job_dir_is_safe(path.parent):
            return None
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):  # a symlink is not S_ISREG under lstat
            log.warning("unsafe_certificate_file certificate_id=%s", certificate_id)
            return None
        return path

    def exists(self, job_id: str, certificate_id: str) -> bool:
        return self._safe_file(job_id, certificate_id) is not None

    def read(self, job_id: str, certificate_id: str) -> bytes | None:
        """The PDF bytes, or None if the file is missing or not a plain file inside the root."""
        path = self._safe_file(job_id, certificate_id)
        if path is None:
            return None
        try:
            fd = os.open(path, os.O_RDONLY | O_NOFOLLOW)
        except OSError:
            return None  # replaced by a symlink or removed after the check
        with os.fdopen(fd, "rb") as fh:
            return fh.read()
