"""Worker: claiming, processing, fenced transitions, loops and file handling (§7, §11)."""

from __future__ import annotations

import os
import stat
from typing import Any

import pytest
from conftest import job_payload, post_job
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.req("R9")
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX permissions")
def test_created_files_are_private(client: TestClient, app: FastAPI, drain: Any, settings: Any) -> None:
    post_job(client, job_payload(n=2))
    drain()
    root = settings.certificates_dir
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    job_dirs = list(root.iterdir())
    assert len(job_dirs) == 1
    assert stat.S_IMODE(job_dirs[0].stat().st_mode) == 0o700
    pdfs = list(job_dirs[0].iterdir())
    assert len(pdfs) == 2
    assert {stat.S_IMODE(p.stat().st_mode) for p in pdfs} == {0o600}
    assert stat.S_IMODE(settings.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.db_path.stat().st_mode) == 0o600
