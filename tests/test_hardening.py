"""Regression tests for the audit findings (audit/AUDIT_REPORT.md, audit/REMEDIATION_REPORT.md).

They live in the default suite so `pytest` alone fails if a later change reverts a fix (audit P8-N3).
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any

import pytest

from certgen import worker


class _StopMain(Exception):
    """Raised by the patched create_schema to leave worker.main() after the check."""


@pytest.mark.req("R9")
def test_worker_installs_signal_handlers_before_touching_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N-01: a SIGTERM that arrives while the worker creates its database must stop it gracefully."""
    seen: dict[str, Any] = {}

    def check_handlers(_engines: Any) -> None:
        seen["sigterm"] = signal.getsignal(signal.SIGTERM)
        seen["sigint"] = signal.getsignal(signal.SIGINT)
        raise _StopMain

    monkeypatch.setenv("CERTGEN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(worker, "create_schema", check_handlers)
    before = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(_StopMain):
            worker.main()
    finally:
        signal.signal(signal.SIGTERM, before[0])
        signal.signal(signal.SIGINT, before[1])
    assert seen["sigterm"] not in (signal.SIG_DFL, signal.SIG_IGN, None)
    assert seen["sigint"] is not signal.default_int_handler


# --- F-13: the test-only slow-render delay must stay below the lease ------------------------------


@pytest.mark.req("R11")
@pytest.mark.parametrize(("lease", "slow_ms"), [(1, 1000), (2, 2000), (30, 30_000), (30, 60_000)])
def test_slow_render_at_or_above_the_lease_is_refused(lease: int, slow_ms: int) -> None:
    from certgen.config import Settings

    with pytest.raises(ValueError, match="CERTGEN_TEST_SLOW_RENDER_MS"):
        Settings(lease_seconds=lease, test_slow_render_ms=slow_ms)


@pytest.mark.req("R11")
@pytest.mark.parametrize(("lease", "slow_ms"), [(1, 999), (2, 300), (3, 400), (30, 0), (61, 60_000)])
def test_slow_render_below_the_lease_is_accepted(lease: int, slow_ms: int) -> None:
    from certgen.config import Settings

    assert Settings(lease_seconds=lease, test_slow_render_ms=slow_ms).test_slow_render_ms == slow_ms


# --- F-15: a damaged certificate file is treated like a missing one ------------------------------


def _one_certificate(client: Any, app: Any, drain: Any) -> tuple[str, Path]:
    from conftest import job_payload, post_job

    job_id = post_job(client, job_payload(n=1)).json()["id"]
    drain()
    item = client.get(f"/v1/certificate-jobs/{job_id}/items").json()["items"][0]
    return item["certificate_id"], app.state.storage.path_for(job_id, item["certificate_id"])


@pytest.mark.req("R8", "R19")
@pytest.mark.parametrize(
    "damage",
    ["truncated", "no-header", "no-trailer", "empty", "oversized"],
)
def test_damaged_certificate_file_is_410(client: Any, app: Any, drain: Any, damage: str) -> None:
    cert, path = _one_certificate(client, app, drain)
    good = path.read_bytes()
    damaged = {
        "truncated": good[: len(good) // 2],
        "no-header": b"garbage" + good[7:],
        "no-trailer": good.replace(b"%%EOF", b"%%XXX"),
        "empty": b"",
        "oversized": good[:-7] + b" " * (2_097_152 + 1) + b"%%EOF\n",
    }[damage]
    path.write_bytes(damaged)
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 410
    assert resp.json()["code"] == "CERTIFICATE_FILE_MISSING"


@pytest.mark.req("R8", "R19")
def test_archive_marks_a_damaged_file_and_leaves_it_out(client: Any, app: Any, drain: Any) -> None:
    import csv
    import io
    import zipfile

    _cert, path = _one_certificate(client, app, drain)
    path.write_bytes(path.read_bytes()[:100])
    job_id = path.parent.name
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/v1/certificate-jobs/{job_id}/archive").content))
    assert [n for n in archive.namelist() if n.endswith(".pdf")] == []
    row = next(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode())))
    assert row["error_codes"] == "CERTIFICATE_FILE_MISSING"


@pytest.mark.req("R8", "R19")
def test_intact_certificate_is_still_served(client: Any, app: Any, drain: Any) -> None:
    cert, path = _one_certificate(client, app, drain)
    resp = client.get(f"/v1/certificates/{cert}")
    assert resp.status_code == 200 and resp.content == path.read_bytes()
