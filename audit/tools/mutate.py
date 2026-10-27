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
    mutants = (
        MUTANTS
        + (STAGE2_MUTANTS if "--stage2" in sys.argv else [])
        + (ROUND2_MUTANTS if "--round2" in sys.argv else [])
    )
    if "--fixes-only" in sys.argv:  # only the mutants that undo an audit fix
        mutants = [m for m in mutants if m[0].startswith(("S", "R"))]
    only = [a.removeprefix("--only=").split(",") for a in sys.argv if a.startswith("--only=")]
    if only:  # e.g. --only=R12,R13
        mutants = [m for m in mutants if m[0] in only[0]]
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
            if verdict == "SURVIVED" and with_audit:
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


# Each undoes one round-2 fix (audit F-12, F-13, F-15, N-01, P8-N1, P8-N2).
ROUND2_MUTANTS: list[tuple[str, str, str, str, str]] = [
    (
        "R01",
        "certgen/worker.py",
        "    signal.signal(signal.SIGTERM, request_stop)\n    check_sqlite_version()\n",
        "    check_sqlite_version()\n",
        "N-01 SIGTERM handler not installed",
    ),
    (
        "R02",
        "certgen/config.py",
        "        if self.test_slow_render_ms >= self.lease_seconds * 1000:\n",
        "        if False:\n",
        "F-13 check removed",
    ),
    (
        "R03",
        "certgen/storage.py",
        "    return data.startswith(PDF_HEADER) and data.rstrip().endswith(PDF_TRAILER)\n",
        "    return True\n",
        "F-15 header/trailer check removed",
    ),
    (
        "R04",
        "certgen/storage.py",
        "        if len(data) > MAX_PDF_BYTES or not _looks_like_pdf(data):\n",
        "        if not _looks_like_pdf(data):\n",
        "F-15 size limit removed",
    ),
    (
        "R05",
        "certgen/validation.py",
        "        if size > BODY_BYTES_PER_RECIPIENT:\n",
        "        if False:\n",
        "P8-N1 row cap removed",
    ),
    (
        "R06",
        "certgen/service.py",
        "    if minimum and shutil.disk_usage(settings.data_dir).free < minimum:\n",
        "    if False:\n",
        "P8-N1 disk guard removed",
    ),
    (
        "R07",
        "certgen/middleware.py",
        "admitted = self.in_progress < self.max_submissions",
        "admitted = True",
        "P8-N2 submission limit off",
    ),
    (
        "R08",
        "certgen/middleware.py",
        "timeout=max(deadline - loop.time(), 0)",
        "timeout=None",
        "P8-N2 body deadline off",
    ),
    (
        "R09",
        "certgen/api.py",
        "    if not dry_run and service.queue_is_full(state.engines, state.settings):",
        "    if False:",
        "P8-N2 queue pre-check removed",
    ),
    (
        "R10",
        "certgen/auth.py",
        "        return match[:CLIENT_ID_LENGTH] if match is not None else None\n",
        '        return (match or "anonymous-client")[:CLIENT_ID_LENGTH]\n',
        "F-12 any token accepted",
    ),
    (
        "R11",
        "certgen/api.py",
        '        key = f"{request.state.client_id}:{key}"\n',
        "        key = key\n",
        "F-12 keys not scoped",
    ),
    (
        "R12",
        "certgen/middleware.py",
        "                await asyncio.wait_for(send(message), timeout=self.send_timeout_seconds)\n",
        "                await send(message)\n",
        "P8-N2 no send deadline",
    ),
    (
        "R13",
        "certgen/middleware.py",
        '            log.warning("slow_client_aborted")',
        "            raise  # ",
        "P8-N2 abort not handled",
    ),
    (
        "R14",
        "certgen/main.py",
        "    app.add_middleware(SendDeadlineMiddleware, send_timeout_seconds=SEND_TIMEOUT_SECONDS)\n",
        "",
        "P8-N2 send deadline not installed",
    ),
    (
        "R15",
        "certgen/config.py",
        "SEND_TIMEOUT_SECONDS = 30\n",
        "SEND_TIMEOUT_SECONDS = 3600\n",
        "P8-N2 send deadline 1 h",
    ),
]


if __name__ == "__main__":
    raise SystemExit(main())
