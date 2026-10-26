"""Targeted mutation testing of the PROJECT's own suite (tests/), run in a disposable copy.

    python audit/tools/mutate.py <copy-of-repo> <output.tsv>

For each mutant: assert the original snippet occurs exactly once, apply it, run the fast suite
(`pytest tests -m "not slow" -x`), record KILLED/SURVIVED, restore the file byte-for-byte.
Never point this at the original checkout.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

MUTANTS: list[tuple[str, str, str, str]] = [
    (
        "M01",
        "certgen/worker.py",
        (
            '"errors = NULL, available_at = NULL, completed_at = :now "\n'
            "    \"WHERE id = :id AND lease_token = :token AND status = 'processing'\""
        ),
        (
            '"errors = NULL, available_at = NULL, completed_at = :now "\n'
            "    \"WHERE id = :id AND status = 'processing'\""
        ),
        "success update loses the token fence",
    ),
    (
        "M02",
        "certgen/worker.py",
        "status = 'processing' AND lease_expires_at < :now \"\n    \"AND attempts < :max_attempts",
        "status = 'processing' AND lease_expires_at <= :now \"\n    \"AND attempts < :max_attempts",
        "reclaim at lease boundary (< -> <=)",
    ),
    (
        "M03",
        "certgen/worker.py",
        "WHERE status = 'pending' AND (available_at IS NULL OR available_at <= :now) ",
        "WHERE status = 'pending' ",
        "retry delay ignored",
    ),
    (
        "M04",
        "certgen/service.py",
        "if active + failed > settings.max_active_items:",
        "if False:",
        "retry skips capacity check",
    ),
    (
        "M05",
        "certgen/service.py",
        "if active + validated.valid_count > settings.max_active_items:",
        "if False:",
        "create skips capacity check",
    ),
    ("M06", "certgen/archive.py", '("=", "+", "-", "@",', '("=", "+", "@",', "CSV guard misses '-'"),
    (
        "M07",
        "certgen/archive.py",
        'neutralize(row.email or ""),',
        'row.email or "",',
        "CSV guard not applied to email",
    ),
    (
        "M08",
        "certgen/validation.py",
        "if not email.isascii():",
        "if False:",
        "non-ASCII (IDN) emails accepted",
    ),
    ("M09", "certgen/validation.py", "key = email.lower()", "key = email", "duplicate check case-sensitive"),
    (
        "M10",
        "certgen/storage.py",
        "if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):",
        "if False:",
        "job-dir symlink check removed",
    ),
    (
        "M11",
        "certgen/storage.py",
        "if not stat.S_ISREG(info.st_mode):  # a symlink is not S_ISREG under lstat",
        "if False:",
        "file symlink lstat check removed (O_NOFOLLOW remains)",
    ),
    ("M12", "certgen/validation.py", "MAX_JSON_DEPTH = 8", "MAX_JSON_DEPTH = 9", "nesting limit off by one"),
    (
        "M13",
        "certgen/validation.py",
        "if issued > today + timedelta(days=1):",
        "if issued > today + timedelta(days=2):",
        "future-date slack 1 -> 2 days",
    ),
    (
        "M14",
        "certgen/service.py",
        "next_after=page[-1].row_index if has_more else None",
        "next_after=page[-1].row_index if page else None",
        "pagination never ends",
    ),
    (
        "M15",
        "certgen/status.py",
        "if pending == valid and attempted == 0:",
        "if pending == valid:",
        "job regresses to queued during retry delay",
    ),
    ("M16", "certgen/worker.py", "if len(pdf) > MAX_PDF_BYTES:", "if False:", "PDF size cap removed"),
    ("M17", "certgen/config.py", 'if "@" in parts.netloc:', "if False:", "base URL userinfo allowed"),
    ("M18", "certgen/errors.py", 'out_headers["Retry-After"] = "1"', "pass", "503 without Retry-After"),
    (
        "M19",
        "certgen/api.py",
        'if record.status == "failed":',
        "if False:",
        "failed certificate treated as succeeded",
    ),
    (
        "M20",
        "certgen/validation.py",
        "or unicodedata.bidirectional(ch) in RTL_BIDI_CLASSES\n",
        "\n",
        "RTL scripts accepted",
    ),
    (
        "M21",
        "certgen/validation.py",
        "MAX_NAME_LENGTH = 120",
        "MAX_NAME_LENGTH = 121",
        "name length limit off by one",
    ),
    (
        "M22",
        "certgen/service.py",
        "\"UPDATE items SET status = 'pending', attempts = 0, errors = NULL, ",
        "\"UPDATE items SET status = 'pending', errors = NULL, ",
        "retry keeps old attempt count",
    ),
    (
        "M23",
        "certgen/service.py",
        "if row.request_hash != fingerprint:",
        "if False:",
        "idempotency ignores body changes",
    ),
    (
        "M24",
        "certgen/middleware.py",
        "if self._declared_length(scope) > self.max_body_bytes:",
        "if False:",
        "Content-Length pre-check removed",
    ),
    (
        "M25",
        "certgen/middleware.py",
        "if len(body) > self.max_body_bytes:",
        "if False:",
        "streamed body not counted",
    ),
    (
        "M26",
        "certgen/worker.py",
        "if claim.attempts < self.settings.max_attempts:",
        "if claim.attempts <= self.settings.max_attempts:",
        "one extra attempt",
    ),
    (
        "M27",
        "certgen/api.py",
        'r"[\\x21-\\x7e]{1,255}"',
        'r"[\\x21-\\x7e]{1,256}"',
        "idempotency key length off by one",
    ),
    (
        "M28",
        "certgen/validation.py",
        '(category[0] == "Z" and ch != " ")',
        "(False)",
        "non-ASCII whitespace accepted",
    ),
    ("M29", "certgen/storage.py", "and info.st_mtime < cutoff_s:", ":", "orphan sweep ignores age"),
    ("M30", "certgen/db.py", '"PRAGMA busy_timeout=5000"', '"PRAGMA busy_timeout=0"', "no lock wait"),
    (
        "M31",
        "certgen/worker.py",
        '"ORDER BY id LIMIT 1"',
        '"ORDER BY id DESC LIMIT 1"',
        "LIFO instead of FIFO claims",
    ),
    (
        "M32",
        "certgen/api.py",
        "would_create_job=validated.valid_count > 0,",
        "would_create_job=True,",
        "dry run always says a job would be created",
    ),
    ("M33", "certgen/validation.py", 'email = value.strip(" ")', "email = value", "email not trimmed"),
    (
        "M34",
        "certgen/worker.py",
        "token = secrets.token_hex(16)  # fresh per claim",
        "token = self.worker_id  # fresh per claim",
        "token = worker id (reused across reclaims)",
    ),
]


def main() -> int:
    copy, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
    if (copy / ".git").exists() and "scratchpad" not in str(copy):
        raise SystemExit("refusing to mutate a path outside the scratchpad")
    # --with-audit: a mutant the project suite misses is re-run against audit/tests.
    # --stage2: also run the STAGE2_MUTANTS, each of which undoes one remediation fix.
    with_audit = "--with-audit" in sys.argv
    mutants = MUTANTS + (STAGE2_MUTANTS if "--stage2" in sys.argv else [])
    rows = ["id\tfile\tdescription\tverdict\tseconds\tfirst_failure"]
    for mid, rel, old, new, desc in mutants:
        path = copy / rel
        original = path.read_bytes()
        source = original.decode()
        count = source.count(old)
        if count != 1:
            rows.append(f"{mid}\t{rel}\t{desc}\tNOT-APPLIED (snippet found {count}x)\t0\t")
            continue
        path.write_text(source.replace(old, new), encoding="utf-8")
        started = time.monotonic()
        try:
            verdict, first = _run_suite(copy, ["tests", "-m", "not slow"], "KILLED (tests)")
            if verdict == "SURVIVED" and (with_audit or mid.startswith("S")):
                verdict, first = _run_suite(copy, ["audit/tests"], "KILLED (audit)")
        finally:
            path.write_bytes(original)
        assert path.read_bytes() == original
        rows.append(f"{mid}\t{rel}\t{desc}\t{verdict}\t{time.monotonic() - started:.1f}\t{first}")
        print(rows[-1], flush=True)
    out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return 0


def _run_suite(copy: Path, args: list[str], killed: str) -> tuple[str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-x", "-q", "-p", "no:cacheprovider", "--timeout=200"],
        cwd=copy,
        capture_output=True,
        text=True,
        timeout=900,
    )
    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    return ("SURVIVED", "") if proc.returncode == 0 else (killed, failed[0][7:] if failed else "")


# Each undoes one Stage 2 fix; the regression tests must kill every one of them.
STAGE2_MUTANTS: list[tuple[str, str, str, str, str]] = [
    (
        "S01",
        "certgen/validation.py",
        "    if len(email) > MAX_EMAIL_LENGTH:\n",
        "    if False:\n",
        "F-01 length pre-check removed",
    ),
    (
        "S02",
        "certgen/validation.py",
        "MAX_UNKNOWN_FIELD_ERRORS = 10 ",
        "MAX_UNKNOWN_FIELD_ERRORS = 10**9 ",
        "F-02 cap removed",
    ),
    (
        "S03",
        "certgen/validation.py",
        "    if _has_lone_surrogate(value):\n",
        "    if False:\n",
        "F-03 surrogate check removed",
    ),
    (
        "S04",
        "certgen/api.py",
        "Query(ge=-1, le=SQLITE_MAX_INTEGER)",
        "Query(ge=-1)",
        "F-04 upper bound removed",
    ),
    ("S05", "certgen/fonts.py", " if cp <= BMP_MAX\n", "\n", "F-05 BMP filter removed"),
    (
        "S06",
        "certgen/service.py",
        'return str(exc.orig) == f"UNIQUE constraint failed: {column}"',
        "return column in str(exc.orig)",
        "F-07 substring match back",
    ),
    (
        "S07",
        "certgen/status.py",
        "        progress = min(progress, MAX_UNFINISHED_PROGRESS)\n",
        "        pass\n",
        "F-08 progress cap removed",
    ),
    (
        "S08",
        "certgen/config.py",
        "BODY_BYTES_PER_RECIPIENT = 4_500\n",
        "BODY_BYTES_PER_RECIPIENT = 2_600\n",
        "F-09 old budget",
    ),
    (
        "S09",
        "certgen/middleware.py",
        "if host is None or normalize_host(_strip_port(host)) not in self.allowed_hosts:",
        "if False:",
        "F-10 host check off",
    ),
    (
        "S10",
        "certgen/middleware.py",
        "admitted = self.in_flight < self.max_requests",
        "admitted = True",
        "F-11 request limit off",
    ),
    (
        "S11",
        "certgen/middleware.py",
        "match = BRACKETED_HOST.fullmatch(value)",
        "match = BRACKETED_HOST.match(value)",
        "F-10 '[::1].evil' prefix bypass",
    ),
]


if __name__ == "__main__":
    raise SystemExit(main())
