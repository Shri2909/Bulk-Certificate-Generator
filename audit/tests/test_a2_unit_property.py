"""Unit + property-based tests (Hypothesis) for the pure modules: JSON decoding, normalization, charset
rules, text fitting, status derivation, CSV guard, id checks, settings parsing, error mapping.

Properties that the audited code is expected to satisfy are plain tests; properties that reproduce a
finding carry @pytest.mark.red("F-xx") and are expected to FAIL.
"""

from __future__ import annotations

import json
import string
import unicodedata
from datetime import date
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, example, given, settings, strategies as st
from pydantic import ValidationError

from certgen.api import iso_ms
from certgen.archive import FORMULA_PREFIXES, neutralize
from certgen.config import ENV_NAMES, Settings
from certgen.errors import body_errors_from_pydantic
from certgen.fonts import SUPPORTED_CHARS
from certgen.layout import EMAIL, ISSUER, MAX_WIDTH, NAME, TITLE, fit_text
from certgen.models import CreateJobRequest
from certgen.rendering import CertificateData, render_certificate
from certgen.status import derive_job_status
from certgen.storage import HEX32, _check_id
from certgen.validation import (
    JsonBodyError,
    _charset_error,
    _check_email,
    canonical_json,
    decode_json_body,
    norm,
    validate_request,
)

PROFILE = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
ALLOWED = sorted(c for c in SUPPORTED_CHARS if c != " " and _charset_error("f", c) is None)
BMP_ALLOWED = [c for c in ALLOWED if ord(c) <= 0xFFFF]

json_scalars = st.none() | st.booleans() | st.integers(-(10**20), 10**20) | st.floats(allow_nan=False, allow_infinity=False) | st.text(max_size=20)
json_values = st.recursive(json_scalars, lambda kids: st.lists(kids, max_size=4) | st.dictionaries(st.text(max_size=8), kids, max_size=4), max_leaves=20)


def depth(v: Any) -> int:
    if isinstance(v, dict):
        return 1 + max((depth(x) for x in v.values()), default=0)
    if isinstance(v, list):
        return 1 + max((depth(x) for x in v), default=0)
    return 1


# --- decode_json_body --------------------------------------------------------------------------


@PROFILE
@given(st.binary(max_size=200))
def test_decode_never_raises_anything_but_json_body_error(raw: bytes) -> None:
    try:
        decode_json_body(raw)
    except JsonBodyError:
        pass


@PROFILE
@given(json_values)
def test_decode_round_trips_valid_json(value: Any) -> None:
    assume(depth(value) <= 8)
    assert decode_json_body(json.dumps(value).encode()) == value


@pytest.mark.parametrize(
    "raw",
    [b'{"a":1,"a":2}', b'{"x":{"b":1,"b":1}}', b"NaN", b"[Infinity]", b"-Infinity", b"1e400", b"1" * 5000,
     b"[" * 9 + b"]" * 9, b"[" * 100_000, b"\xef\xbb\xbf{}", b"\xc0\xaf", b"\xed\xa0\x80", b"", b"{"],
)
def test_decode_rejects_hostile_inputs(raw: bytes) -> None:
    with pytest.raises(JsonBodyError):
        decode_json_body(raw)


@pytest.mark.red("F-03")
@PROFILE
@given(st.text(alphabet=st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF), min_size=1, max_size=3))
def test_canonical_form_of_any_accepted_body_is_utf8_encodable(s: str) -> None:
    """F-03 root cause: decode_json_body accepts \\uD800 escapes; the canonical form (stored + hashed) is
    then not UTF-8 encodable. Expected: either decoding rejects it, or the canonical form encodes."""
    raw = json.dumps({"k": s}).encode()  # ensure_ascii=True -> legal JSON with \\udXXX escapes
    try:
        value = decode_json_body(raw)
    except JsonBodyError:
        return
    canonical_json(value).encode("utf-8")


# --- norm + charset --------------------------------------------------------------------------


@PROFILE
@given(st.text(max_size=60))
def test_norm_is_idempotent_and_canonical(s: str) -> None:
    once = norm(s)
    assert norm(once) == once
    assert not once.startswith(" ") and not once.endswith(" ") and "  " not in once
    assert unicodedata.is_normalized("NFC", once)


@PROFILE
@given(st.text(max_size=40))
def test_charset_rule_matches_its_definition(s: str) -> None:
    err = _charset_error("f", s)
    bad_c1 = any(unicodedata.category(c)[0] == "C" or (unicodedata.category(c)[0] == "Z" and c != " ") for c in s)
    bad_c2 = any(
        unicodedata.category(c)[0] == "M" or unicodedata.bidirectional(c) in ("R", "AL", "AN") or c not in SUPPORTED_CHARS
        for c in s
    )
    if bad_c1:
        assert err is not None and err.code.value == "INVALID_CHARACTERS"
    elif bad_c2:
        assert err is not None and err.code.value == "UNSUPPORTED_CHARACTERS"
    else:
        assert err is None


def test_supported_scope_matches_readme_scripts() -> None:
    """Observation for F-05 (not a pass/fail contract): what the allowlist really admits beyond
    Latin/Greek/Cyrillic. Recorded in the report; asserts only that the census is computed."""
    scripts: dict[str, int] = {}
    for c in ALLOWED:
        name = unicodedata.name(c, "UNNAMED").split(" ")[0]
        scripts[name] = scripts.get(name, 0) + 1
    astral = [c for c in ALLOWED if ord(c) > 0xFFFF]
    print(f"\nallowed characters: {len(ALLOWED)}; non-BMP: {len(astral)}; e.g. {''.join(astral[:12])}")
    assert len(ALLOWED) > 0


# --- fit_text ----------------------------------------------------------------------------------

words = st.text(alphabet=st.sampled_from(BMP_ALLOWED), min_size=1, max_size=14)


@PROFILE
@given(st.lists(words, min_size=1, max_size=12), st.sampled_from([NAME, TITLE, ISSUER]))
def test_fit_text_never_loses_reorders_or_overflows(ws: list[str], block: Any) -> None:
    text = " ".join(ws)
    fit = fit_text(text, block)
    if fit is None:
        return
    assert " ".join(fit.lines) == text
    assert len(fit.lines) <= block.max_lines
    assert len(fit.lines) * 1.2 * fit.size <= block.box_height
    for line in fit.lines:
        assert block.line_width(line, fit.size) <= MAX_WIDTH + 1e-6
    assert block.min_size <= fit.size <= block.max_size


@PROFILE
@given(st.text(alphabet=st.sampled_from(string.ascii_letters + string.digits + "@.-_+="), min_size=1, max_size=254))
def test_email_block_fits_any_254_ascii_and_keeps_every_character(s: str) -> None:
    fit = fit_text(s, EMAIL)
    assert fit is not None
    assert "".join(fit.lines) == s


# --- email acceptance --------------------------------------------------------------------------


@PROFILE
@given(st.emails() | st.text(alphabet=st.characters(max_codepoint=0x7F), max_size=40))
def test_accepted_emails_are_printable_ascii_bounded(s: str) -> None:
    email, err = _check_email({"email": s}, print_email=True)
    if err is None:
        assert email is not None and email.isascii() and len(email) <= 254
        assert all(33 <= ord(c) <= 126 for c in email), repr(email)


# --- validate_request structure ----------------------------------------------------------------

row = st.fixed_dictionaries(
    {},
    optional={
        "name": st.none() | st.integers() | st.text(max_size=30),
        "email": st.none() | st.integers() | st.emails() | st.text(max_size=20),
        "extra": st.integers(),
    },
)


@PROFILE
@given(st.lists(row, min_size=1, max_size=15))
def test_validate_request_invariants(rows: list[dict[str, Any]]) -> None:
    body = CreateJobRequest.model_validate({"certificate": {"title": "T", "issuer": "I", "issued_on": "2026-10-01"}, "recipients": rows})
    v = validate_request(body, today=date(2026, 10, 11), print_email=True, max_recipients=100)
    assert [r.row_index for r in v.rows] == list(range(len(rows)))
    errs = v.row_errors
    assert [e.row_index for e in errs] == sorted(e.row_index for e in errs)
    for r in v.rows:
        codes = [e.code.value for e in r.errors]
        if "DUPLICATE_RECIPIENT" in codes:
            assert codes == ["DUPLICATE_RECIPIENT"]
        if r.valid:
            assert r.name and r.email
            assert fit_text(r.name, NAME) is not None
    valid_emails = [r.email.lower() for r in v.rows if r.valid and r.email]
    assert len(valid_emails) == len(set(valid_emails))


@PROFILE
@given(json_values)
def test_pydantic_error_mapping_never_raises(value: Any) -> None:
    try:
        CreateJobRequest.model_validate(value)
    except ValidationError as exc:
        for e in body_errors_from_pydantic(exc.errors()):
            assert e.code.value


# --- rendering of accepted names ---------------------------------------------------------------


@settings(max_examples=60, deadline=None)
@given(st.lists(st.text(alphabet=st.sampled_from(BMP_ALLOWED), min_size=1, max_size=12), min_size=1, max_size=6))
def test_any_accepted_bmp_name_renders(ws: list[str]) -> None:
    name = norm(" ".join(ws))
    assume(len(name) <= 120 and _charset_error("name", name) is None and fit_text(name, NAME) is not None)
    pdf = render_certificate(CertificateData("a" * 32, "b" * 32, name, "z@example.com", "T", "I", "2026-10-01"))
    assert pdf.startswith(b"%PDF-")


# --- status derivation -------------------------------------------------------------------------

counts = st.fixed_dictionaries(
    {s: st.integers(0, 120_000) for s in ("rejected", "pending", "processing", "succeeded", "failed")}
)


@PROFILE
@given(counts, st.integers(0, 600_000))
def test_status_invariants(c: dict[str, int], attempted: int) -> None:
    status, progress = derive_job_status(c, attempted)
    valid = sum(c.values()) - c["rejected"]
    done = c["succeeded"] + c["failed"]
    assert 0.0 <= progress <= 1.0
    if valid > 0:
        assert (status in ("completed", "completed_with_errors", "failed")) == (done == valid)
        if status == "completed":
            assert c["failed"] == 0 and c["rejected"] == 0


@pytest.mark.red("F-08")
@PROFILE
@given(counts, st.integers(0, 600_000))
@example({"rejected": 0, "pending": 1, "processing": 0, "succeeded": 99_999, "failed": 0}, 99_999)
def test_progress_one_only_when_terminal(c: dict[str, int], attempted: int) -> None:
    status, progress = derive_job_status(c, attempted)
    if progress == 1.0:
        assert status in ("completed", "completed_with_errors", "failed")


# --- CSV guard, ids, timestamps, settings --------------------------------------------------------


@PROFILE
@given(st.text(max_size=20))
def test_neutralized_cell_never_starts_with_a_formula_prefix(s: str) -> None:
    out = neutralize(s)
    assert not out.startswith(FORMULA_PREFIXES)


@PROFILE
@given(st.text(max_size=40))
def test_check_id_accepts_only_32_lower_hex(s: str) -> None:
    if HEX32.fullmatch(s):
        assert _check_id(s) == s
    else:
        with pytest.raises(ValueError):
            _check_id(s)


@PROFILE
@given(st.integers(0, 253_402_300_799_999))
def test_iso_ms_round_trips(ms: int) -> None:
    from datetime import datetime

    parsed = datetime.strptime(iso_ms(ms), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=__import__("datetime").UTC)
    assert round(parsed.timestamp() * 1000) == ms


@PROFILE
@given(st.sampled_from(sorted(ENV_NAMES.values())), st.text(max_size=12))
def test_settings_from_env_fails_only_with_value_error(var: str, raw: str) -> None:
    try:
        Settings.from_env({var: raw, "CERTGEN_DATA_DIR": "/tmp/certgen-audit-unused"} if var != "CERTGEN_DATA_DIR" else {var: raw or "x"})
    except ValueError:
        pass
