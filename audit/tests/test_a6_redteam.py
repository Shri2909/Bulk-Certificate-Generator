"""Red-team attack catalogue (in-process, synthetic data). A PASS means the attack was blocked. Attacks
that succeed are recorded as findings and have red tests in test_a1/test_a4.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import CERT, JOBS, ROOT, payload, post_raw
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

TRAVERSAL = ["..%2F..%2Fetc%2Fpasswd", "%2e%2e%2f%2e%2e%2fetc%2fpasswd", "..%5C..%5Cwindows", "%c0%ae%c0%ae%2f",
             "a" * 31 + "%00", "a" * 32 + "%2F..", "A" * 32, "%E2%80%AE" + "a" * 29, "a" * 32 + ".pdf"]


@pytest.mark.parametrize("bad", TRAVERSAL)
@pytest.mark.parametrize("route", ["/v1/certificates/{}", JOBS + "/{}", JOBS + "/{}/items", JOBS + "/{}/archive"])
def test_path_traversal_and_malformed_ids(client: TestClient, bad: str, route: str) -> None:
    resp = client.get(route.format(bad))
    assert resp.status_code == 404
    assert b"root:" not in resp.content and b"%PDF" not in resp.content


def test_idor_requires_unguessable_ids(client: TestClient, drain: Any) -> None:
    """Sequential ids would allow enumeration; ids are 128-bit random hex. A neighbour id is unknown."""
    a = client.post(JOBS, json=payload(n=2)).json()["id"]
    drain()
    certs = [i["certificate_id"] for i in client.get(f"{JOBS}/{a}/items").json()["items"]]
    for cid in certs:
        neighbour = f"{(int(cid, 16) + 1) % 16**32:032x}"
        assert client.get(f"/v1/certificates/{neighbour}").status_code == 404


SQLI = ["Robert'); DROP TABLE items;--", "' OR 1=1 --", '" OR ""="', "1; ATTACH DATABASE '/tmp/x' AS x;--"]


@pytest.mark.parametrize("s", SQLI)
def test_sql_injection_is_stored_verbatim(client: TestClient, app: FastAPI, s: str) -> None:
    resp = client.post(JOBS, json=payload([{"name": s, "email": "a@example.com"}], certificate={**CERT, "title": s}))
    assert resp.status_code == 202
    with app.state.engines.read.connect() as conn:
        assert conn.execute(text("SELECT name FROM items")).scalar_one() == s
        assert conn.execute(text("SELECT title FROM jobs")).scalar_one() == s
        assert conn.execute(text("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")).scalar_one() == 2
    assert client.get(f"{JOBS}/{resp.json()['id']}/items", params={"status": "pending' OR '1'='1"}).status_code == 422


@pytest.mark.parametrize(
    "ctype", ["application/json; charset=utf-16", "text/json", "multipart/form-data; boundary=x", "application/json, text/plain", "application/json;charset=utf-8;x=1"]
)
def test_content_type_confusion(client: TestClient, ctype: str) -> None:
    resp = client.post(JOBS, content=json.dumps(payload(n=1)).encode(), headers={"Content-Type": ctype})
    assert resp.status_code == 415


def test_cross_site_simple_requests_cannot_create_jobs(client: TestClient) -> None:
    """CSRF: browsers can send text/plain / form bodies cross-site without preflight; both are 415."""
    body = json.dumps(payload(n=1)).encode()
    for ctype in ("text/plain", "application/x-www-form-urlencoded"):
        assert client.post(JOBS, content=body, headers={"Content-Type": ctype, "Origin": "https://evil.example"}).status_code == 415
    assert "access-control-allow-origin" not in client.options(JOBS, headers={"Origin": "https://evil.example"}).headers


@pytest.mark.parametrize("value", ["a\r\nX-Injected: 1", "k\x00", "k\x7f", "ключ"])
def test_idempotency_key_header_injection(client: TestClient, value: str) -> None:
    resp = client.post(JOBS, json=payload(n=1), headers={"Idempotency-Key": value.encode("utf-8")})
    assert resp.status_code == 422
    assert "x-injected" not in resp.headers


def test_idempotency_key_existence_oracle_and_squatting(client: TestClient) -> None:
    """Observation (documented limitation: global, never-expiring keys, no auth). Anyone who learns or
    guesses a key can (a) learn it exists (409 vs 202) and (b) pre-register it so the owner's later
    request is refused forever. Not a contract; recorded as INFO."""
    assert client.post(JOBS, json=payload(n=1), headers={"Idempotency-Key": "order-42"}).status_code == 202  # attacker squats
    victim = client.post(JOBS, json=payload(n=2), headers={"Idempotency-Key": "order-42"})
    assert victim.status_code == 409 and victim.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


@pytest.mark.parametrize(
    "params", [{"after": "-2"}, {"limit": "0"}, {"limit": "1001"}, {"limit": "1e3"}, {"after": "0x10"}, {"status": "PENDING"}, {"after": ""}]
)
def test_listing_parameter_abuse(client: TestClient, params: dict[str, str]) -> None:
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    resp = client.get(f"{JOBS}/{job_id}/items", params=params)
    assert resp.status_code == 422 and resp.json()["code"] == "VALIDATION_FAILED"


@pytest.mark.parametrize(
    "body",
    [b'{"certificate":{},"recipients":[],"certificate":{}}', b'{"certificate":' + b"[" * 50 + b"]" * 50 + b"}",
     b'{"recipients":[' + b"1," * 10 + b"1]}", b"\xff\xfe{", b'{"a":"\\ud83d\\ude00"}', b"123", b"null", b'"x"'],
)
def test_structural_json_attacks_are_422(client: TestClient, body: bytes) -> None:
    resp = post_raw(client, body)
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"


def test_type_confusion_in_rows_never_500(client: TestClient) -> None:
    rows: list[Any] = [{"name": ["x"], "email": {"a": 1}}, {"name": 1.5, "email": True}, {"name": None, "email": None}, {"name": "Ok", "email": "ok@example.com"}]
    resp = client.post(JOBS, json=payload(rows))
    assert resp.status_code == 202
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_TYPE", "INVALID_TYPE", "INVALID_TYPE", "INVALID_TYPE", "REQUIRED", "REQUIRED"]


@pytest.mark.parametrize(
    "name",
    ["‮evil", "a‍b", "Admin ", "العربية", "שלום", "é́", "李小龙", "﻿x", "\U000e0041"],
)
def test_bidi_spoofing_and_unrenderable_names_rejected(client: TestClient, name: str) -> None:
    resp = client.post(JOBS, json=payload([{"name": name, "email": "a@example.com"}, {"name": "B", "email": "b@example.com"}]))
    assert resp.status_code == 202
    codes = [e["code"] for e in resp.json()["errors"] if e["row_index"] == 0]
    assert codes and codes[0] in ("INVALID_CHARACTERS", "UNSUPPORTED_CHARACTERS")


def test_business_logic_abuse(client: TestClient, drain: Any) -> None:
    job_id = client.post(JOBS, json=payload([{"name": "A", "email": "a@example.com"}, {"name": "B", "email": "bad"}])).json()["id"]
    assert client.post(f"{JOBS}/{job_id}/retry").json()["code"] == "NOTHING_TO_RETRY"  # retry with nothing failed
    drain()
    assert client.post(f"{JOBS}/{job_id}/retry").json()["code"] == "NOTHING_TO_RETRY"  # rejected rows are not retryable
    assert client.post(f"{JOBS}/{'0' * 32}/retry").status_code == 404
    ancient = client.post(JOBS, json=payload(n=1, certificate={**CERT, "issued_on": "0001-01-01"}))
    assert ancient.status_code == 202  # documented: no lower bound on issued_on (README Limitations)
    for bad in ("2026-13-01", "2026-02-30", "２０２６-01-01", "2026-1-1", "+2026-01-01", "2026-01-01T00:00"):
        r = client.post(JOBS, json=payload(n=1, certificate={**CERT, "issued_on": bad}))
        assert r.status_code == 422 and r.json()["errors"][0]["code"] == "INVALID_FORMAT", bad


def test_errors_never_leak_paths_or_exception_text(client: TestClient, drain: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from certgen.storage import Storage

    def leak(*_: Any) -> None:
        raise OSError("/srv/secret/path/zoe@example.com permission denied")

    monkeypatch.setattr(Storage, "save_atomic", leak)
    job_id = client.post(JOBS, json=payload(n=1)).json()["id"]
    drain()
    body = client.get(f"{JOBS}/{job_id}/items").text
    assert "/srv/secret" not in body and "permission denied" not in body


def test_no_secrets_committed() -> None:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
    patterns = ("-----BEGIN", "AKIA", "ghp_", "xoxb-", "sk-ant-", "password=", "api_key=")
    hits = []
    this_file = Path(__file__).resolve()
    for f in files:
        p = Path(ROOT, f)
        # This file holds the pattern list itself, so it would always match; every other file is scanned.
        if p.suffix in (".ttf", ".pdf", ".png") or not p.is_file() or p.resolve() == this_file:
            continue
        content = p.read_text(errors="ignore")
        hits += [f"{f}:{pat}" for pat in patterns if pat in content]
    assert hits == []
