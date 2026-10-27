"""Settings: the only place that reads environment variables (§15, §24.1, §24.6).

Every value is validated when a Settings object is built, whether from the environment or directly
in tests, so an invalid deployment fails at startup with the variable named instead of misbehaving later.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

# Worst legal row: a 120-character name sent decomposed (NFD; up to 4 code points per accepted
# character) with every code point \uXXXX-escaped = 2,880 B, a 254-character email fully escaped =
# 1,524 B, keys and punctuation escaped = 71 B: 4,475 B (amendment A-12, audit F-09). 4,500 per row
# plus 64 KiB of envelope means a request within field limits is never 413'd (§12).
BODY_BYTES_PER_RECIPIENT = 4_500
BODY_BYTES_ENVELOPE = 65_536
ARCHIVE_CONCURRENCY = 2  # archive builds per process (§6.7); fixed, not an env var
# Requests in progress per process (§24.12, amended A-08): counted in the app, not as uvicorn connections.
MAX_CONCURRENT_REQUESTS = 64
# A request body must arrive completely within this time, else 408 (audit P8-N2).
BODY_TIMEOUT_SECONDS = 30
# Every piece of a response must be accepted by the client within this time, else it is aborted.
SEND_TIMEOUT_SECONDS = 30
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
MAX_BASE_URL_LENGTH = 100

ENV_NAMES = {
    "data_dir": "CERTGEN_DATA_DIR",
    "host": "CERTGEN_HOST",
    "port": "CERTGEN_PORT",
    "embedded_worker": "CERTGEN_EMBEDDED_WORKER",
    "worker_threads": "CERTGEN_WORKER_THREADS",
    "lease_seconds": "CERTGEN_LEASE_SECONDS",
    "max_attempts": "CERTGEN_MAX_ATTEMPTS",
    "retry_delay_seconds": "CERTGEN_RETRY_DELAY_SECONDS",
    "poll_interval_seconds": "CERTGEN_POLL_INTERVAL_SECONDS",
    "max_recipients": "CERTGEN_MAX_RECIPIENTS",
    "max_active_items": "CERTGEN_MAX_ACTIVE_ITEMS",
    "print_email": "CERTGEN_PRINT_EMAIL_ON_CERTIFICATE",
    "public_base_url": "CERTGEN_PUBLIC_BASE_URL",
    "log_level": "CERTGEN_LOG_LEVEL",
    "test_slow_render_ms": "CERTGEN_TEST_SLOW_RENDER_MS",
    "allowed_hosts": "CERTGEN_ALLOWED_HOSTS",
    "min_free_disk_mb": "CERTGEN_MIN_FREE_DISK_MB",
    "max_concurrent_submissions": "CERTGEN_MAX_CONCURRENT_SUBMISSIONS",
    "api_tokens": "CERTGEN_API_TOKENS",
}
TOKEN = re.compile(r"[A-Za-z0-9._~+/-]{16,256}=*")  # RFC 6750 token68, at least 16 characters
HOSTNAME = re.compile(
    r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*"
)

# Inclusive numeric ranges from §15 (max_active_items' floor depends on max_recipients; see below).
RANGES: dict[str, tuple[float, float]] = {
    "port": (1, 65_535),
    "worker_threads": (1, 16),
    "lease_seconds": (1, 3_600),
    "max_attempts": (1, 10),
    "retry_delay_seconds": (0, 3_600),
    "poll_interval_seconds": (0.05, 10),
    "max_recipients": (1, 100_000),
    "max_active_items": (1, 1_000_000),
    "test_slow_render_ms": (0, 60_000),
    "min_free_disk_mb": (0, 1_000_000),
    "max_concurrent_submissions": (1, 64),
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000
    embedded_worker: bool = True
    worker_threads: int = 2
    lease_seconds: int = 30
    max_attempts: int = 3
    retry_delay_seconds: float = 2.0
    poll_interval_seconds: float = 0.5
    max_recipients: int = 10_000
    max_active_items: int = 50_000
    print_email: bool = True
    public_base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    test_slow_render_ms: int = 0
    # Host header names accepted in addition to the loopback names (DNS-rebinding defence, audit F-10).
    allowed_hosts: tuple[str, ...] = ()
    # New jobs and retries are refused below this much free disk on the data dir; 0 disables (audit P8-N1).
    min_free_disk_mb: int = 1024
    # Job submissions (incl. dry runs) parsed and validated at once per process (audit P8-N2).
    max_concurrent_submissions: int = 2
    # Bearer tokens accepted on /v1 routes; empty = use (or create) {data_dir}/api-token (audit F-12).
    api_tokens: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, (low, high) in RANGES.items():
            value = getattr(self, name)
            if not low <= value <= high:
                raise ValueError(f"{ENV_NAMES[name]} must be between {low} and {high}, got {value!r}")
        if self.max_active_items < self.max_recipients:
            raise ValueError(
                f"CERTGEN_MAX_ACTIVE_ITEMS ({self.max_active_items}) must be at least "
                f"CERTGEN_MAX_RECIPIENTS ({self.max_recipients}); else a maximal request is never admitted"
            )
        if self.test_slow_render_ms >= self.lease_seconds * 1000:
            # Every render would outlive its lease and end as WORKER_LOST (audit F-13).
            raise ValueError(
                f"CERTGEN_TEST_SLOW_RENDER_MS ({self.test_slow_render_ms}) must be shorter than "
                f"CERTGEN_LEASE_SECONDS ({self.lease_seconds} s)"
            )
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            raise ValueError(f"CERTGEN_HOST must be an IP address, got {self.host!r}") from None
        if self.log_level.upper() not in LOG_LEVELS:
            raise ValueError(
                f"CERTGEN_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {self.log_level!r}"
            )
        # Frozen dataclass: normalized values are written once here, never mutated afterwards.
        # The data dir is resolved once so later chdir calls or relative paths cannot move it (§24.8).
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        object.__setattr__(self, "log_level", self.log_level.upper())
        object.__setattr__(self, "public_base_url", _validate_base_url(self.public_base_url))
        object.__setattr__(self, "allowed_hosts", tuple(_validate_host(h) for h in self.allowed_hosts))
        object.__setattr__(self, "api_tokens", tuple(validate_token(t) for t in self.api_tokens))

    @property
    def trusted_hosts(self) -> frozenset[str]:
        """Every name the Host header may carry: loopback always, plus CERTGEN_ALLOWED_HOSTS."""
        return LOOPBACK_HOSTS | frozenset(self.allowed_hosts)

    @property
    def max_body_bytes(self) -> int:
        return self.max_recipients * BODY_BYTES_PER_RECIPIENT + BODY_BYTES_ENVELOPE

    @property
    def lease_ms(self) -> int:
        return self.lease_seconds * 1000

    @property
    def retry_delay_ms(self) -> int:
        return round(self.retry_delay_seconds * 1000)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "certgen.sqlite3"

    @property
    def certificates_dir(self) -> Path:
        return self.data_dir / "certificates"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "api-token"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        if environ is None and ENV_NAMES["public_base_url"] not in env:
            logging.getLogger("certgen.config").warning(
                "CERTGEN_PUBLIC_BASE_URL is not set; certificate links will use http://localhost:8000"
            )
        kwargs: dict[str, object] = {}
        for field in fields(cls):
            raw = env.get(ENV_NAMES[field.name])
            if raw is not None:
                kwargs[field.name] = _parse(field.name, field.type, raw)
        return cls(**kwargs)  # type: ignore[arg-type]


def _parse(name: str, type_name: object, raw: str) -> object:
    var = ENV_NAMES[name]
    try:
        if type_name == "bool":
            lowered = raw.strip().lower()
            if lowered not in ("1", "0", "true", "false"):
                raise ValueError
            return lowered in ("1", "true")
        if type_name == "int":
            return int(raw)
        if type_name == "float":
            return float(raw)
    except ValueError:
        raise ValueError(f"{var} has an invalid value {raw!r}") from None
    if type_name == "Path":
        return Path(raw)
    if type_name == "tuple[str, ...]":
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return raw


def normalize_host(value: str) -> str:
    """Lowercase a host name; IP literals (IPv6 with or without brackets) in their canonical form."""
    name = value.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return name


def validate_token(value: str) -> str:
    token = value.strip()
    if not TOKEN.fullmatch(token):
        var = ENV_NAMES["api_tokens"]
        raise ValueError(f"{var} entries must be 16-256 token characters (A-Z a-z 0-9 . _ ~ + / -)")
    return token


def _validate_host(value: str) -> str:
    host = normalize_host(value)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOSTNAME.fullmatch(host):
            var = ENV_NAMES["allowed_hosts"]
            raise ValueError(f"{var} must list host names or IP addresses, got {value!r}") from None
    return host


def _validate_base_url(value: str) -> str:
    """§24.6: output-only URL printed on certificates; never fetched, so only its shape matters."""
    var = ENV_NAMES["public_base_url"]
    url = value[:-1] if value.endswith("/") else value
    if not url.isascii() or any(not ("!" <= ch <= "~") for ch in url):
        raise ValueError(f"{var} must be printable ASCII without spaces")
    if len(url) > MAX_BASE_URL_LENGTH:
        raise ValueError(f"{var} must be at most {MAX_BASE_URL_LENGTH} characters")
    if "?" in url or "#" in url:
        raise ValueError(f"{var} must not contain a query or fragment")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{var} must be an absolute http(s) URL with a host")
    if "@" in parts.netloc:
        raise ValueError(f"{var} must not contain user information")
    try:
        parts.port  # noqa: B018 - property access validates the port number
    except ValueError:
        raise ValueError(f"{var} has an invalid port") from None
    if scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError(f"{var} must use https for non-loopback hosts")
    return url
