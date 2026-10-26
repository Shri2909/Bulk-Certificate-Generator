"""Reproducible evidence probes for F-01, F-02, F-05, F-07, F-08 and the charset census.

    python audit/tools/probes.py <scratch-dir> [--skip-f02]

Run from the root of an isolated copy (certgen is imported from there). F-02 allocates ~2 GB:
run it under a memory limit, e.g. `ulimit -v 6000000`.
"""

from __future__ import annotations

import collections
import contextlib
import cProfile
import io
import json
import pstats
import resource
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import date
from pathlib import Path

from email_validator import EmailNotValidError, validate_email


def section(title: str) -> None:
    print(f"\n==== {title}", flush=True)


def f01_curve() -> None:
    section("F-01 email-validator time vs local-part length (standalone)")
    for n in (16_000, 32_000, 64_000, 128_000, 256_000, 512_000):
        email = "a" * n + "@example.com"
        started = time.perf_counter()
        with contextlib.suppress(EmailNotValidError):
            validate_email(email, check_deliverability=False, allow_smtputf8=False)
        print(f"local-part {n:>7} chars: {time.perf_counter() - started:7.3f} s", flush=True)
    prof = cProfile.Profile()
    email = "a" * 64_000 + "@example.com"
    prof.enable()
    with contextlib.suppress(EmailNotValidError):
        validate_email(email, check_deliverability=False, allow_smtputf8=False)
    prof.disable()
    out = io.StringIO()
    pstats.Stats(prof, stream=out).sort_stats("tottime").print_stats(3)
    print("\n".join(line for line in out.getvalue().splitlines() if "email_validator" in line))


def f02_amplification(scratch: Path) -> None:
    from fastapi.testclient import TestClient

    from certgen.config import Settings
    from certgen.main import create_app

    section("F-02 unknown-field amplification at the default body limit (single request each)")
    cert = {"title": "T", "issuer": "I", "issued_on": "2026-10-01"}
    for mode in ("all_invalid", "partial"):
        tmp = Path(tempfile.mkdtemp(dir=scratch))
        settings = Settings(data_dir=tmp / "d", embedded_worker=False)
        n = (settings.max_body_bytes - 300) // 13
        row = "{" + ",".join(f'"k{i:07d}":0' for i in range(n)) + "}"
        rows = row if mode == "all_invalid" else '{"name":"A","email":"a@example.com"},' + row
        body = ('{"certificate":' + json.dumps(cert) + ',"recipients":[' + rows + "]}").encode()
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            started = time.perf_counter()
            resp = client.post(
                "/v1/certificate-jobs", content=body, headers={"Content-Type": "application/json"}
            )
            took = time.perf_counter() - started
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(
            f"{mode}: request={len(body)} B status={resp.status_code} response={len(resp.content)} B "
            f"({len(resp.content) / len(body):.2f}x) {took:.1f} s peak_rss={peak / 1024:.0f} MB "
            f"(process high-water; before {before / 1024:.0f} MB)"
        )
        if resp.status_code == 202:
            import sqlite3

            stored = (
                sqlite3.connect(settings.db_path)
                .execute("SELECT SUM(length(errors)) FROM items")
                .fetchone()[0]
            )
            print(f"   persisted items.errors = {stored} B")
        del resp


def f05_text_layer(scratch: Path) -> None:
    from pypdf import PdfReader

    from certgen.rendering import CertificateData, render_certificate

    section("F-05 non-BMP name: ToUnicode entry, pdftotext, pypdf")
    name = "Zoë \U0001f63c Smith"
    pdf = render_certificate(
        CertificateData("a" * 32, "b" * 32, name, "zoe@example.com", "T", "I", "2026-10-01")
    )
    path = scratch / "f05.pdf"
    path.write_bytes(pdf)
    reader = PdfReader(path)
    for key, ref in reader.pages[0]["/Resources"]["/Font"].items():
        font = ref.get_object()
        if "/ToUnicode" in font:
            for line in font["/ToUnicode"].get_object().get_data().splitlines():
                if b"1F63" in line.upper():
                    print(f"{key} {font['/BaseFont']}: {line.decode()}")
    print("pypdf text line:", repr(reader.pages[0].extract_text().splitlines()[2]))
    try:
        txt = subprocess.run(["pdftotext", str(path), "-"], capture_output=True, text=True, check=True).stdout
        print("pdftotext line:", repr(next(ln for ln in txt.splitlines() if "Smith" in ln)))
    except (OSError, subprocess.CalledProcessError) as exc:
        print("pdftotext unavailable:", exc)


def census() -> None:
    from certgen.fonts import SUPPORTED_CHARS
    from certgen.validation import _charset_error

    section("Charset census: characters accepted in names/titles/issuers")
    allowed = [c for c in SUPPORTED_CHARS if _charset_error("f", c) is None]
    keys = (
        "LATIN",
        "GREEK",
        "CYRILLIC",
        "ARMENIAN",
        "GEORGIAN",
        "OLD ITALIC",
        "TIFINAGH",
        "LAO",
        "BRAILLE",
        "BOX DRAWING",
        "ARROW",
        "DOMINO",
        "PLAYING CARD",
        "MATHEMATICAL",
    )

    def bucket(c: str) -> str:
        name = unicodedata.name(c, "?")
        for k in keys:
            if k in name:
                return k
        return "EMOJI/PICTOGRAPHS" if 0x1F300 <= ord(c) <= 0x1FAFF else f"other ({unicodedata.category(c)})"

    print(f"accepted: {len(allowed)}; non-BMP: {sum(ord(c) > 0xFFFF for c in allowed)}")
    for k, v in collections.Counter(bucket(c) for c in allowed).most_common(20):
        print(f"  {v:5d}  {k}")


def f07_f08() -> None:
    from certgen import service
    from certgen.config import Settings
    from certgen.db import create_engines, create_schema
    from certgen.models import CreateJobRequest
    from certgen.status import derive_job_status
    from certgen.validation import validate_request

    section("F-07 _insert_job with an existing Idempotency-Key")
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(data_dir=tmp, embedded_worker=False)
    engines = create_engines(settings.db_path)
    create_schema(engines)
    body = CreateJobRequest.model_validate(
        {
            "certificate": {"title": "T", "issuer": "I", "issued_on": "2026-10-01"},
            "recipients": [{"name": "A", "email": "a@example.com"}],
        }
    )
    validated = validate_request(body, today=date(2026, 10, 11), print_email=True, max_recipients=10)
    service.create_job(engines, settings, lambda: 1, validated, "{}", "K")
    drawn = {"n": 0}
    real = service.new_job_id

    def counting() -> str:
        drawn["n"] += 1
        return real()

    service.new_job_id = counting
    try:
        with engines.write.begin() as conn:
            service._insert_job(conn, settings, validated, "{}", 1, "K", "other-hash")
    except Exception as exc:
        print(f"raised {type(exc).__name__}: {exc} (job ids drawn: {drawn['n']})")
    print(
        "'jobs.id' in 'UNIQUE constraint failed: jobs.idempotency_key' ->",
        "jobs.id" in "UNIQUE constraint failed: jobs.idempotency_key",
    )
    section("F-08 progress")
    for valid in (10_000, 20_000, 20_001, 100_000):
        print(valid, derive_job_status({"succeeded": valid - 1, "pending": 1}, valid - 1))


def main() -> None:
    scratch = Path(sys.argv[1])
    scratch.mkdir(parents=True, exist_ok=True)
    census()
    f01_curve()
    f05_text_layer(scratch)
    f07_f08()
    if "--skip-f02" not in sys.argv:
        f02_amplification(scratch)


if __name__ == "__main__":
    main()
