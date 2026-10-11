# ADR-013: Certificate identity and verification (differentiator D1)

Status: Proposed (HMAC necessity → Gate Q3) · Requirements: R5 (ID printed on certificate), R8 · Evidence: RESEARCH §5, §6

## Context
A certificate PDF is trivially forgeable: edit the name and re-save. A third party (an employer) needs a way to check it. Separately, ADR-004 needs a stable per-item ID **before** rendering so retries are idempotent.

## Options
1. **DB integer item ID printed on certificate** (simplest). Enumerable: anyone can walk `/verify/1..N` and harvest every recipient's name.
2. **Random 128-bit certificate ID + DB lookup**. Unguessable, so not enumerable. The DB is the source of truth. A forged PDF with a real ID but an edited name is caught, because `/verify` returns the *stored* name and the verifier compares.
3. **Random ID + HMAC-SHA256 signature** over `(certificate_id, name, issued_on)`, with a truncated signature in the QR URL. Adds tamper evidence that doesn't depend on ID secrecy, and lets a revoked/rotated key invalidate a batch. Costs a secret to manage (`CERTGEN_SIGNING_KEY`), rotation story, constant-time compare.
4. **Asymmetric signatures (Ed25519) / PDF digital signatures**. Offline-verifiable without calling us. Heavy key/PKI story, overkill.

## Criteria
Real security value, explainability, operational cost.

## Decision (proposed; your call at Gate Q3)
**Option 2 as the baseline, plus Option 3's signature as defense in depth**, because the brief asks for HMAC and it's ~20 lines:
- `certificate_id = secrets.token_hex(16)`, assigned at item insert (valid rows only). `UNIQUE`.
- `sig = base32(HMAC-SHA256(key, f"{certificate_id}|{name}|{issued_on}"))[:16]`, computed at render time and printed as a QR of `{PUBLIC_BASE_URL}/v1/verify/{certificate_id}?sig={sig}` plus the human-readable ID.
- `GET /v1/verify/{certificate_id}?sig=…` → `200 {"valid": true, "certificate_id", "recipient_name", "title", "issuer", "issued_on"}`, or `200 {"valid": false}` for unknown ID / bad sig / not-succeeded. A single response shape doesn't reveal which check failed, so there's no oracle. Comparison via `hmac.compare_digest`.
- **Privacy:** the verify response never includes email (PII). Name, title, issuer and date are already printed on the document.
- **Key handling:** `CERTGEN_SIGNING_KEY` env. If unset in dev, a random key is generated **and persisted** to `data/signing.key` (so restarts don't invalidate certificates), with a startup log warning. README states: set it explicitly in production.

## Honest assessment
With 128-bit random IDs and the DB as the source of truth, the HMAC adds little **today**. Its value is (a) catching a tampered QR URL without a DB hit and (b) mass-invalidation by key rotation. If you prefer minimalism, drop to Option 2. It saves the key-management section of the README and one interview rabbit hole.

## Tests
`test_verify.py`: valid round trip; tampered sig → invalid; unknown ID → invalid; failed/pending item → invalid; response never contains email; QR payload decoded from the rendered PDF matches the verify URL (decode via pure-Python check of the URL string passed to the widget; no image decoding dependency).

## At 100× scale
Key ID prefix in the signature for rotation (`v1.` + sig). A CDN-cached verify endpoint. Ed25519 if offline verification by third parties becomes a requirement.
