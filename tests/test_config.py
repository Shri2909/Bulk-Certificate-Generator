"""Settings.from_env: §15 defaults and ranges, §24.1 capacity floor, §24.6 public base URL rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from certgen.config import Settings


@pytest.mark.req("R11", "R13")
def test_defaults_match_blueprint() -> None:
    s = Settings.from_env({})
    assert s.data_dir == Path("data").resolve()
    assert (s.host, s.port) == ("127.0.0.1", 8000)
    assert s.embedded_worker is True
    assert s.worker_threads == 2
    assert (s.lease_seconds, s.max_attempts) == (30, 3)
    assert s.retry_delay_seconds == 2.0
    assert s.poll_interval_seconds == 0.5
    assert s.max_recipients == 10_000
    assert s.max_active_items == 50_000
    assert s.print_email is True
    assert s.public_base_url == "http://localhost:8000"
    assert s.log_level == "INFO"
    assert s.test_slow_render_ms == 0


@pytest.mark.req("R11")
def test_derived_values() -> None:
    s = Settings.from_env({})
    assert s.max_body_bytes == 26_065_536
    assert s.lease_ms == 30_000
    assert s.retry_delay_ms == 2_000
    assert Settings.from_env({"CERTGEN_MAX_RECIPIENTS": "100"}).max_body_bytes == 100 * 2_600 + 65_536


@pytest.mark.req("R11")
def test_data_dir_is_resolved_to_absolute_path(tmp_path: Path) -> None:
    s = Settings.from_env({"CERTGEN_DATA_DIR": str(tmp_path / "x" / ".." / "data")})
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.db_path == s.data_dir / "certgen.sqlite3"
    assert s.certificates_dir == s.data_dir / "certificates"


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    ("var", "good", "bad"),
    [
        ("CERTGEN_PORT", ["1", "65535"], ["0", "65536", "abc"]),
        ("CERTGEN_WORKER_THREADS", ["1", "16"], ["0", "17"]),
        ("CERTGEN_LEASE_SECONDS", ["1", "3600"], ["0", "3601"]),
        ("CERTGEN_MAX_ATTEMPTS", ["1", "10"], ["0", "11"]),
        ("CERTGEN_RETRY_DELAY_SECONDS", ["0", "0.5", "3600"], ["-0.1", "3600.5", "x"]),
        ("CERTGEN_POLL_INTERVAL_SECONDS", ["0.05", "10"], ["0.04", "10.1"]),
        ("CERTGEN_MAX_RECIPIENTS", ["1", "10000"], ["0", "100001"]),
        ("CERTGEN_LOG_LEVEL", ["DEBUG", "info", "WARNING", "ERROR"], ["TRACE", ""]),
        ("CERTGEN_TEST_SLOW_RENDER_MS", ["0", "60000"], ["-1", "60001"]),
        ("CERTGEN_EMBEDDED_WORKER", ["1", "0", "TRUE", "false"], ["yes", "2", ""]),
        ("CERTGEN_PRINT_EMAIL_ON_CERTIFICATE", ["1", "0", "True", "FALSE"], ["on", "no"]),
        ("CERTGEN_HOST", ["127.0.0.1", "0.0.0.0", "::1"], ["localhost", "999.1.1.1"]),
    ],
)
def test_every_variable_is_range_checked(var: str, good: list[str], bad: list[str]) -> None:
    for value in good:
        Settings.from_env({var: value})
    for value in bad:
        with pytest.raises(ValueError, match=var):
            Settings.from_env({var: value})


@pytest.mark.req("R11")
def test_booleans_parse_to_expected_values() -> None:
    assert Settings.from_env({"CERTGEN_PRINT_EMAIL_ON_CERTIFICATE": "0"}).print_email is False
    assert Settings.from_env({"CERTGEN_EMBEDDED_WORKER": "False"}).embedded_worker is False
    assert Settings.from_env({"CERTGEN_EMBEDDED_WORKER": "1"}).embedded_worker is True


@pytest.mark.req("R11")
def test_max_active_items_range_and_floor() -> None:
    assert Settings.from_env({"CERTGEN_MAX_ACTIVE_ITEMS": "1000000"}).max_active_items == 1_000_000
    with pytest.raises(ValueError, match="CERTGEN_MAX_ACTIVE_ITEMS"):
        Settings.from_env({"CERTGEN_MAX_ACTIVE_ITEMS": "1000001"})
    # Below MAX_RECIPIENTS a single maximal request could never be admitted.
    with pytest.raises(ValueError, match="CERTGEN_MAX_ACTIVE_ITEMS"):
        Settings.from_env({"CERTGEN_MAX_RECIPIENTS": "500", "CERTGEN_MAX_ACTIVE_ITEMS": "499"})
    assert Settings.from_env({"CERTGEN_MAX_RECIPIENTS": "500", "CERTGEN_MAX_ACTIVE_ITEMS": "500"})


@pytest.mark.req("R11")
def test_direct_construction_is_validated_too(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CERTGEN_WORKER_THREADS"):
        Settings(data_dir=tmp_path, worker_threads=0)


REJECT = None


@pytest.mark.req("R11")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://localhost:8000/", "http://localhost:8000"),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://certs.example.org", "https://certs.example.org"),
        ("https://example.org/certgen/", "https://example.org/certgen"),
        ("HTTPS://Example.org", "HTTPS://Example.org"),
        ("https://e.org/" + "a" * 86 + "/", "https://e.org/" + "a" * 86),  # 100 chars after strip
        ("", REJECT),
        ("localhost:8000", REJECT),  # no scheme
        ("ftp://example.org", REJECT),  # scheme
        ("http://example.org", REJECT),  # http only for loopback hosts
        ("http://localhost.evil.org", REJECT),  # loopback lookalike still needs https
        ("https://", REJECT),  # no host
        ("https://user:pw@example.org", REJECT),  # userinfo
        ("https://example.org/?q=1", REJECT),  # query
        ("https://example.org/?", REJECT),  # empty query marker
        ("https://example.org/#frag", REJECT),  # fragment
        ("https://ex\u00e4mple.org", REJECT),  # non-ASCII
        ("https://exa mple.org", REJECT),  # space
        ("https://e.org/" + "a" * 87, REJECT),  # 101 chars
        ("https://example.org:99999", REJECT),  # invalid port
    ],
)
def test_public_base_url_rules(value: str, expected: str | None) -> None:
    if expected is REJECT:
        with pytest.raises(ValueError, match="CERTGEN_PUBLIC_BASE_URL"):
            Settings.from_env({"CERTGEN_PUBLIC_BASE_URL": value})
    else:
        assert Settings.from_env({"CERTGEN_PUBLIC_BASE_URL": value}).public_base_url == expected
