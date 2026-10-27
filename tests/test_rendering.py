"""The single predefined template (§10): content, Unicode, layout limits and byte determinism."""

from __future__ import annotations

import hashlib
import inspect
import io
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import (
    AUTH_HEADERS,
    TEST_API_TOKEN,
    TESTCLIENT_HOSTS,
    FakeClock,
    job_payload,
    post_job,
    recipient,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import text

from certgen.config import Settings
from certgen.main import create_app
from certgen.rendering import CertificateData, render_certificate

ROOT = Path(__file__).resolve().parent.parent
NAME_120 = (
    "Quintanilla Wadsworth Anna Quintanilla Wolfgang Hohenberg Montgomery Wadsworth "
    "Konstantin Whitmore Oluwaseun Worthington"
)
SAMPLE = CertificateData(
    job_id="3f2c0d7e9b1a4c6d8e0f1a2b3c4d5e6f",
    certificate_id="a1b2c3d4e5f60718293a4b5c6d7e8f90",
    name="Zoë Łukasz",
    email="zoe@example.com",
    title="Python for Data Engineering",
    issuer="Acme Academy",
    issued_on="2026-10-01",
)


def pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    return reader.pages[0].extract_text()


def flat(text_value: str) -> str:
    """Line breaks from wrapping become spaces, so wrapped fields can be searched as one string."""
    return " ".join(text_value.split())


@pytest.mark.req("R16")
def test_output_is_pdf() -> None:
    data = render_certificate(SAMPLE)
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    reader = PdfReader(io.BytesIO(data))
    page = reader.pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == pytest.approx(
        (841.89, 595.28), abs=0.01
    )
    meta = reader.metadata
    assert meta is not None
    assert meta.title == f"Certificate {SAMPLE.certificate_id}"
    assert meta.author == "Acme Academy"
    assert meta.creator == "certgen"


@pytest.mark.req("R5", "R16")
def test_pdf_text_contains_all_fields() -> None:
    content = flat(pdf_text(render_certificate(SAMPLE)))
    for expected in [
        "Certificate of Completion",
        "This is to certify that",
        "Zoë Łukasz",
        "zoe@example.com",
        "has successfully completed",
        "Python for Data Engineering",
        "Issued by",
        "Acme Academy",
        "Date of issue: 1 October 2026",
        f"Certificate ID: {SAMPLE.certificate_id}",
    ]:
        assert expected in content, expected


@pytest.mark.req("R5")
def test_email_printed_by_default_and_opt_out(tmp_path: Path, fake_clock: FakeClock) -> None:
    def generated_text(print_email: bool | None) -> str:
        overrides = {} if print_email is None else {"print_email": print_email}
        settings = Settings(
            data_dir=tmp_path / f"d{print_email}",
            embedded_worker=False,
            allowed_hosts=TESTCLIENT_HOSTS,
            api_tokens=(TEST_API_TOKEN,),
            **overrides,
        )
        app = create_app(settings, clock=fake_clock)
        with TestClient(app, headers=AUTH_HEADERS) as client:
            job = post_job(client, job_payload(recipients=[recipient(0, email="opt@example.com")])).json()
            assert job["print_email"] is (print_email is not False)
            from certgen.worker import Worker

            Worker(
                app.state.engines, app.state.storage, settings, fake_clock, render_certificate
            ).run_until_idle()
            with app.state.engines.read.connect() as conn:
                cert = conn.execute(text("SELECT certificate_id FROM items")).scalar_one()
            return flat(pdf_text(client.get(f"/v1/certificates/{cert}").content))

    assert "opt@example.com" in generated_text(None)  # default: printed
    assert "opt@example.com" in generated_text(True)
    opted_out = generated_text(False)
    assert "opt@example.com" not in opted_out
    assert "Recipient 0" in opted_out
    assert "@" not in pdf_text(render_certificate(replace(SAMPLE, email=None)))


@pytest.mark.req("R4")
def test_single_template_only_recipient_fields_differ() -> None:
    assert list(inspect.signature(render_certificate).parameters) == ["data"]  # no template parameter
    other = replace(SAMPLE, certificate_id="0" * 32, name="Bob Example", email="bob@example.org")

    def template_lines(data: CertificateData) -> list[str]:
        specific = (data.name, data.email or "", data.certificate_id)
        return [
            line
            for line in pdf_text(render_certificate(data)).splitlines()
            if not any(s in line for s in specific)
        ]

    assert template_lines(SAMPLE) == template_lines(other)
    assert len(template_lines(SAMPLE)) >= 6


@pytest.mark.req("R5")
@pytest.mark.parametrize(
    "name", ["Ελένη Παπαδοπούλου", "Дмитрий Иванов", "Zoë Łukasz Ngọc", "Ærøskøbing Œuvre"]
)
def test_unicode_name_round_trips(name: str) -> None:
    assert name in flat(pdf_text(render_certificate(replace(SAMPLE, name=name))))


@pytest.mark.req("R5", "R16")
def test_long_name_renders() -> None:
    data = replace(
        SAMPLE, name=NAME_120, email="a" * 64 + "@" + "b" * 63 + ".example.org", title="T " * 99 + "T"
    )
    content = pdf_text(render_certificate(data))
    assert NAME_120 in flat(content)
    assert data.email in "".join(content.split())  # emails wrap at any character, not at spaces
    # The 120-character name needs two lines; both halves are on the page.
    assert NAME_120.split(" ")[0] in content and NAME_120.split(" ")[-1] in content


@pytest.mark.req("R5")
def test_unfittable_text_raises_instead_of_overflowing() -> None:
    # Validation prevents this; the renderer still refuses rather than drawing outside the page.
    with pytest.raises(ValueError, match="name"):
        render_certificate(replace(SAMPLE, name="W" * 120))


@pytest.mark.req("R16")
def test_render_is_byte_deterministic(tmp_path: Path) -> None:
    first = render_certificate(SAMPLE)
    assert render_certificate(SAMPLE) == first
    assert render_certificate(replace(SAMPLE, name="Other")) != first

    script = (
        "import hashlib, sys\n"
        "from certgen.rendering import CertificateData, render_certificate\n"
        f"data = CertificateData(**{SAMPLE.__dict__!r})\n"
        "sys.stdout.write(hashlib.sha256(render_certificate(data)).hexdigest())\n"
    )
    digests = set()
    for seed in ("1", "777"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        digests.add(out.stdout)
    assert digests == {hashlib.sha256(first).hexdigest()}


@pytest.mark.req("R16")
def test_generated_file_matches_direct_render(client: TestClient, app: FastAPI, drain: Any) -> None:
    # The worker's output is exactly render_certificate() of the stored row: inputs come only from the DB.
    post_job(client, job_payload(recipients=[recipient(0, name="Zoë Łukasz", email="zoe@example.com")]))
    drain()
    with app.state.engines.read.connect() as conn:
        row = conn.execute(text("SELECT job_id, certificate_id FROM items")).one()
    expected = render_certificate(
        CertificateData(
            job_id=row.job_id,
            certificate_id=row.certificate_id,
            name="Zoë Łukasz",
            email="zoe@example.com",
            title="Python for Data Engineering",
            issuer="Acme Academy",
            issued_on="2026-10-01",
        )
    )
    assert client.get(f"/v1/certificates/{row.certificate_id}").content == expected
