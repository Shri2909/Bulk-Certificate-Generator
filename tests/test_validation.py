"""Validation rules (§9), strict JSON body parsing (§24.5) and error ordering (§24.11)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from conftest import JOBS, certificate_info, job_payload, post_job, recipient
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from certgen.models import CreateJobRequest
from certgen.validation import ValidatedRequest, validate_request

TODAY = date(2026, 10, 11)  # the FakeClock's UTC date
NAME_120_FITS = (
    "Quintanilla Wadsworth Anna Quintanilla Wolfgang Hohenberg Montgomery Wadsworth "
    "Konstantin Whitmore Oluwaseun Worthington"
)


def validate(rows: list[Any], print_email: bool = True, **certificate: Any) -> ValidatedRequest:
    body = CreateJobRequest.model_validate(
        job_payload(recipients=rows, certificate=certificate_info(**certificate))
    )
    return validate_request(body, today=TODAY, print_email=print_email, max_recipients=10_000)


def codes(result: ValidatedRequest, row: int) -> list[tuple[str | None, str]]:
    return [(e.field, e.code.value) for e in result.rows[row].errors]


def request_codes(result: ValidatedRequest) -> list[tuple[str | None, str]]:
    return [(e.field, e.code.value) for e in result.request_errors]


def post_raw(client: TestClient, raw: bytes) -> Any:
    return client.post(JOBS, content=raw, headers={"Content-Type": "application/json"})


def flat_codes(resp: Any) -> list[tuple[int | None, str | None, str]]:
    return [(e["row_index"], e["field"], e["code"]) for e in resp.json()["errors"]]


@pytest.mark.req("R2", "R15")
def test_valid_row_normalized() -> None:
    result = validate([{"name": "  Zoe\u0308   Łukasz ", "email": "  Zoe@Example.com "}])
    row = result.rows[0]
    assert row.errors == ()
    assert row.name == "Zoë Łukasz"  # NFC, trimmed, inner runs of U+0020 collapsed
    assert row.email == "Zoe@Example.com"  # stripped, original case kept


@pytest.mark.req("R15")
@pytest.mark.parametrize("name", ["Seán O'Connor-Smith", "D'Angelo", "Anne-Marie Lévesque", "Jo"])
def test_apostrophe_and_hyphen_names_valid(name: str) -> None:
    assert codes(validate([recipient(0, name=name)]), 0) == []


@pytest.mark.req("R15")
def test_name_missing_required() -> None:
    result = validate([{"email": "a@example.com"}, {"name": None, "email": "b@example.com"}])
    assert codes(result, 0) == [("name", "REQUIRED")]
    assert codes(result, 1) == [("name", "REQUIRED")]
    assert result.rows[0].errors[0].message == "name is required."


@pytest.mark.req("R15")
@pytest.mark.parametrize("name", ["", "   "])
def test_name_blank_required(name: str) -> None:
    assert codes(validate([recipient(0, name=name)]), 0) == [("name", "REQUIRED")]


@pytest.mark.req("R2", "R15")
def test_name_non_string_rejected_not_500(client: TestClient, app: FastAPI) -> None:
    rows = [recipient(0), recipient(1, name=42), recipient(2, name=["x"]), recipient(3, name={"a": 1})]
    resp = post_job(client, job_payload(recipients=rows))
    assert resp.status_code == 202
    assert flat_codes(resp) == [
        (1, "name", "INVALID_TYPE"),
        (2, "name", "INVALID_TYPE"),
        (3, "name", "INVALID_TYPE"),
    ]
    with app.state.engines.read.connect() as conn:
        stored = conn.execute(text("SELECT row_index, name, status FROM items ORDER BY row_index")).all()
    assert [(r.name, r.status) for r in stored[1:]] == [(None, "rejected")] * 3


@pytest.mark.req("R15")
def test_name_120_ok_121_too_long() -> None:
    assert len(NAME_120_FITS) == 120
    result = validate([recipient(0, name=NAME_120_FITS), recipient(1, name=NAME_120_FITS + "x")])
    assert codes(result, 0) == []
    assert codes(result, 1) == [("name", "TOO_LONG")]


@pytest.mark.req("R15")
@pytest.mark.parametrize(("char", "label"), [("\t", "U+0009"), ("\u200d", "U+200D"), ("\u202e", "U+202E")])
def test_control_and_format_chars_invalid(char: str, label: str) -> None:
    result = validate([recipient(0, name=f"Ann{char}Lee")])
    assert codes(result, 0) == [("name", "INVALID_CHARACTERS")]
    assert result.rows[0].errors[0].message == f"Character {label} is not allowed."


@pytest.mark.req("R15")
@pytest.mark.parametrize("char", ["\u00a0", "\u2028", "\u3000"])
def test_non_ascii_spaces_invalid(char: str) -> None:
    assert codes(validate([recipient(0, name=f"Ann{char}Lee")]), 0) == [("name", "INVALID_CHARACTERS")]


@pytest.mark.req("R15")
def test_cjk_unsupported() -> None:
    result = validate([recipient(0, name="李小龙")])
    assert codes(result, 0) == [("name", "UNSUPPORTED_CHARACTERS")]
    assert result.rows[0].errors[0].message == "Character U+674E is not supported by the certificate font."


@pytest.mark.req("R15")
@pytest.mark.parametrize("name", ["דוד כהן", "محمد علي", "Ali ٣"])
def test_rtl_unsupported(name: str) -> None:
    # DejaVu has Hebrew and Arabic glyphs, but ReportLab cannot lay out right-to-left text.
    assert codes(validate([recipient(0, name=name)]), 0) == [("name", "UNSUPPORTED_CHARACTERS")]


@pytest.mark.req("R15")
def test_leftover_combining_mark_unsupported() -> None:
    result = validate([recipient(0, name="Rene\u0301"), recipient(1, name="q\u0301uinn")])
    assert codes(result, 0) == []  # e + U+0301 composes to é under NFC
    assert result.rows[0].name == "René"
    assert codes(result, 1) == [("name", "UNSUPPORTED_CHARACTERS")]  # no precomposed q-acute exists


@pytest.mark.req("R15")
@pytest.mark.parametrize("name", ["Zoë Łukasz Ngọc", "Ελένη", "Дмитрий"])
def test_supported_unicode_names_valid(name: str) -> None:
    assert codes(validate([recipient(0, name=name)]), 0) == []


@pytest.mark.req("R15")
def test_name_too_wide_does_not_fit() -> None:
    result = validate([recipient(0, name="W" * 120)])
    assert codes(result, 0) == [("name", "DOES_NOT_FIT")]


@pytest.mark.req("R15")
@pytest.mark.parametrize("email", ["not-an-email", "a@", "@example.com", "a b@example.com", "a@@example.com"])
def test_invalid_email(email: str) -> None:
    assert codes(validate([recipient(0, email=email)]), 0) == [("email", "INVALID_EMAIL")]


@pytest.mark.req("R15")
def test_email_over_254_invalid() -> None:
    local = "a" * 64
    domain = ".".join(["b" * 63, "c" * 63, "d" * 57]) + ".org"  # 189 chars
    ok = f"{local}@{domain}"
    assert len(ok) == 254
    result = validate([recipient(0, email=ok), recipient(1, email="a" + ok)])
    assert codes(result, 0) == []
    assert codes(result, 1) == [("email", "INVALID_EMAIL")]


@pytest.mark.req("R15")
@pytest.mark.parametrize("email", ["zoë@example.com", "zoe@exämple.com"])
def test_non_ascii_email_invalid(email: str) -> None:
    assert codes(validate([recipient(0, email=email)]), 0) == [("email", "INVALID_EMAIL")]


@pytest.mark.req("R15")
def test_email_missing_blank_and_non_string() -> None:
    result = validate([{"name": "A"}, recipient(1, email="   "), recipient(2, email=7)])
    assert codes(result, 0) == [("email", "REQUIRED")]
    assert codes(result, 1) == [("email", "REQUIRED")]
    assert codes(result, 2) == [("email", "INVALID_TYPE")]
    assert result.rows[2].email is None


@pytest.mark.req("R2", "R15")
def test_duplicate_email_case_and_whitespace() -> None:
    result = validate(
        [
            recipient(0, email="zoe@example.com"),
            recipient(1),
            recipient(2, email=" ZOE@example.com "),
        ]
    )
    assert codes(result, 2) == [("email", "DUPLICATE_RECIPIENT")]
    error = result.rows[2].errors[0]
    assert error.duplicate_of_row_index == 0
    assert error.message == "Duplicate of row 0 (same email, case-insensitive)."
    assert error.as_flat()["duplicate_of_row_index"] == 0


@pytest.mark.req("R15")
def test_duplicate_of_rejected_row_is_not_duplicate() -> None:
    result = validate([recipient(0, name="", email="x@example.com"), recipient(1, email="X@example.com")])
    assert codes(result, 0) == [("name", "REQUIRED")]
    assert codes(result, 1) == []


@pytest.mark.req("R15")
def test_duplicates_across_jobs_allowed(client: TestClient) -> None:
    payload = job_payload(recipients=[recipient(0, email="same@example.com")])
    assert post_job(client, payload).status_code == 202
    assert post_job(client, payload).status_code == 202


@pytest.mark.req("R2")
def test_multiple_field_errors_all_reported() -> None:
    result = validate([{"name": "李", "email": "nope", "zip": 1}])
    assert codes(result, 0) == [
        ("name", "UNSUPPORTED_CHARACTERS"),
        ("email", "INVALID_EMAIL"),
        ("zip", "UNKNOWN_FIELD"),
    ]


@pytest.mark.req("R15")
def test_unknown_row_field() -> None:
    result = validate([recipient(0, phone="1", age=3)])
    assert codes(result, 0) == [("age", "UNKNOWN_FIELD"), ("phone", "UNKNOWN_FIELD")]


@pytest.mark.req("R15")
def test_row_not_object_422(client: TestClient) -> None:
    resp = post_job(client, job_payload(recipients=["zoe@example.com", recipient(1)]))
    assert resp.status_code == 422
    assert flat_codes(resp) == [(0, "recipients", "INVALID_TYPE")]


@pytest.mark.req("R15")
@pytest.mark.parametrize("raw", [b"{bad json", b"[1,", b'{"certificate": }', b"\x00"])
def test_invalid_json_422(client: TestClient, raw: bytes) -> None:
    resp = post_raw(client, raw)
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_JSON"]


@pytest.mark.req("R15")
def test_empty_body_is_required(client: TestClient) -> None:
    resp = post_raw(client, b"")
    assert resp.status_code == 422
    assert flat_codes(resp) == [(None, None, "REQUIRED")]


@pytest.mark.req("R15")
def test_body_must_be_an_object(client: TestClient) -> None:
    resp = post_raw(client, b"[]")
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_TYPE"]


@pytest.mark.req("R15")
def test_unknown_top_level_field_422(client: TestClient) -> None:
    payload = {**job_payload(), "extra": 1}
    resp = post_job(client, payload)
    assert resp.status_code == 422
    assert flat_codes(resp) == [(None, "extra", "UNKNOWN_FIELD")]
    payload = job_payload(certificate={**certificate_info(), "logo": "x"})
    assert flat_codes(post_job(client, payload)) == [(None, "certificate.logo", "UNKNOWN_FIELD")]


@pytest.mark.req("R15")
def test_missing_and_mistyped_top_level_fields_422(client: TestClient) -> None:
    resp = post_job(client, {"certificate": {"title": 5, "issuer": "x"}})
    assert resp.status_code == 422
    assert sorted(flat_codes(resp), key=str) == sorted(
        [
            (None, "certificate.title", "INVALID_TYPE"),
            (None, "certificate.issued_on", "REQUIRED"),
            (None, "recipients", "REQUIRED"),
        ],
        key=str,
    )


@pytest.mark.req("R15")
def test_empty_recipients_422(client: TestClient) -> None:
    resp = post_job(client, job_payload(recipients=[]))
    assert resp.status_code == 422
    assert flat_codes(resp) == [(None, "recipients", "REQUIRED")]
    assert resp.json()["detail"] == "Request is invalid."


@pytest.mark.req("R15")
@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("", "REQUIRED"),
        ("   ", "REQUIRED"),
        ("T" * 201, "TOO_LONG"),
        ("Intro\tPython", "INVALID_CHARACTERS"),
        ("Python 入门", "UNSUPPORTED_CHARACTERS"),
        ("W" * 200, "DOES_NOT_FIT"),
    ],
)
def test_certificate_title_required_too_long_unsupported_does_not_fit(value: str, code: str) -> None:
    assert request_codes(validate([recipient(0)], title=value)) == [("certificate.title", code)]
    assert request_codes(validate([recipient(0)], issuer=value)) == [("certificate.issuer", code)]


@pytest.mark.req("R15")
def test_certificate_fields_are_normalized() -> None:
    result = validate([recipient(0)], title="  Python   for Data  ", issuer="Acme\u0301 Academy")
    assert result.request_errors == ()
    assert result.title == "Python for Data"
    assert result.issuer == "Acmé Academy"


@pytest.mark.req("R15")
@pytest.mark.parametrize(
    "value",
    ["2026-1-01", "2026-02-30", "01/10/2026", "2026-10-01T00:00", "\uff12\uff10\uff12\uff16-10-01", ""],
)
def test_issued_on_format(value: str) -> None:
    assert request_codes(validate([recipient(0)], issued_on=value)) == [
        ("certificate.issued_on", "INVALID_FORMAT")
    ]


@pytest.mark.req("R15")
def test_issued_on_tomorrow_ok_day_after_rejected(client: TestClient) -> None:
    assert request_codes(validate([recipient(0)], issued_on="2026-10-12")) == []
    assert request_codes(validate([recipient(0)], issued_on="1999-01-01")) == []
    assert request_codes(validate([recipient(0)], issued_on="2026-10-13")) == [
        ("certificate.issued_on", "DATE_IN_FUTURE")
    ]
    # Through the API, "today" comes from the injected clock (2026-10-11 UTC), not the host date.
    ok = post_job(client, job_payload(certificate=certificate_info(issued_on="2026-10-12")))
    assert ok.status_code == 202
    late = post_job(client, job_payload(certificate=certificate_info(issued_on="2026-10-13")))
    assert flat_codes(late) == [(None, "certificate.issued_on", "DATE_IN_FUTURE")]


@pytest.mark.req("R2", "R15")
def test_all_rows_invalid_422(client: TestClient, app: FastAPI) -> None:
    resp = post_job(client, job_payload(recipients=[{"email": "a@example.com"}, recipient(1, name="李")]))
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["detail"] == "2 of 2 recipients are invalid; no job was created."
    assert flat_codes(resp) == [(0, "name", "REQUIRED"), (1, "name", "UNSUPPORTED_CHARACTERS")]
    assert body["errors"][1]["message"] == "Character U+674E is not supported by the certificate font."
    with app.state.engines.read.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one() == 0


@pytest.mark.req("R2", "R3", "R10")
def test_partial_acceptance_three_row_trace(client: TestClient, app: FastAPI) -> None:
    payload = {
        "certificate": certificate_info(),
        "recipients": [
            {"name": "Zoë Łukasz", "email": "zoe@example.com"},
            {"name": "Bob", "email": "not-an-email"},
            {"name": "Zoe Again", "email": " ZOE@example.com "},
        ],
    }
    resp = post_job(client, payload)
    assert resp.status_code == 202
    body = resp.json()
    assert body["counts"] == {
        "total": 3,
        "rejected": 2,
        "pending": 1,
        "processing": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert flat_codes(resp) == [(1, "email", "INVALID_EMAIL"), (2, "email", "DUPLICATE_RECIPIENT")]
    assert body["errors"][1] == {
        "row_index": 2,
        "field": "email",
        "code": "DUPLICATE_RECIPIENT",
        "message": "Duplicate of row 0 (same email, case-insensitive).",
        "duplicate_of_row_index": 0,
    }
    with app.state.engines.read.connect() as conn:
        rows = conn.execute(text("SELECT row_index, status, errors FROM items ORDER BY row_index")).all()
    assert [r.status for r in rows] == ["pending", "rejected", "rejected"]
    assert rows[0].errors is None
    assert json.loads(rows[2].errors) == [
        {
            "field": "email",
            "code": "DUPLICATE_RECIPIENT",
            "message": "Duplicate of row 0 (same email, case-insensitive).",
            "duplicate_of_row_index": 0,
        }
    ]


@pytest.mark.req("R2")
def test_sql_meta_chars_stored_verbatim(client: TestClient, app: FastAPI) -> None:
    name = "Robert'); DROP TABLE items;--"
    resp = post_job(client, job_payload(recipients=[recipient(0, name=name)]))
    assert resp.status_code == 202
    with app.state.engines.read.connect() as conn:
        assert conn.execute(text("SELECT name FROM items")).scalar_one() == name


@pytest.mark.req("R15")
def test_duplicate_json_keys_422(client: TestClient) -> None:
    raw = (
        b'{"certificate": {"title": "A", "title": "B", "issuer": "I", "issued_on": "2026-10-01"},'
        b' "recipients": []}'
    )
    resp = post_raw(client, raw)
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert [e["code"] for e in errors] == ["INVALID_JSON"]
    assert "'title'" in errors[0]["message"]


@pytest.mark.req("R15")
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_nan_and_infinity_422(client: TestClient, literal: str) -> None:
    raw = json.dumps(job_payload(recipients=[recipient(0, name="__X__")])).replace('"__X__"', literal)
    resp = post_raw(client, raw.encode())
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_JSON"]


@pytest.mark.req("R15")
def test_nesting_depth_422(client: TestClient) -> None:
    def with_name(value: Any) -> bytes:
        return json.dumps(job_payload(recipients=[recipient(0), recipient(1, name=value)])).encode()

    # root object = 1, recipients list = 2, row object = 3, so 5 nested lists reach depth 8.
    at_limit = post_raw(client, with_name([[[[[]]]]]))
    assert at_limit.status_code == 202
    assert flat_codes(at_limit) == [(1, "name", "INVALID_TYPE")]
    too_deep = post_raw(client, with_name([[[[[[]]]]]]))
    assert too_deep.status_code == 422
    assert [e["code"] for e in too_deep.json()["errors"]] == ["INVALID_JSON"]
    very_deep = post_raw(client, b"[" * 100_000 + b"]" * 100_000)  # RecursionError territory
    assert very_deep.status_code == 422


@pytest.mark.req("R15")
def test_invalid_utf8_422(client: TestClient) -> None:
    raw = json.dumps(job_payload()).encode().replace(b"Recipient 0", b"Recipient \xff")
    resp = post_raw(client, raw)
    assert resp.status_code == 422
    assert [e["code"] for e in resp.json()["errors"]] == ["INVALID_JSON"]


@pytest.mark.req("R2", "R15")
def test_error_order_is_stable(client: TestClient) -> None:
    rows = [
        {"zeta": 1, "email": "bad", "alpha": 2, "name": ""},
        recipient(1),
        {"email": "x@example.com", "name": "李"},
        recipient(3, email="recipient1@example.com"),
    ]
    payload = job_payload(recipients=rows, certificate=certificate_info(issued_on="nope", title=""))
    resp = post_job(client, payload)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Request is invalid."
    assert flat_codes(resp) == [
        (None, "certificate.title", "REQUIRED"),
        (None, "certificate.issued_on", "INVALID_FORMAT"),
        (0, "name", "REQUIRED"),
        (0, "email", "INVALID_EMAIL"),
        (0, "alpha", "UNKNOWN_FIELD"),
        (0, "zeta", "UNKNOWN_FIELD"),
        (2, "name", "UNSUPPORTED_CHARACTERS"),
        (3, "email", "DUPLICATE_RECIPIENT"),
    ]
    assert post_job(client, payload).json() == resp.json()  # same input, same order every time
