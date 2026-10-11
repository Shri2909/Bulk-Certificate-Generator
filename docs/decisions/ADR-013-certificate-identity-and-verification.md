# ADR-013: Certificate identity and verification — "Verifiable certificates" (differentiator D1)

Status: Accepted (Gate 1 Q3) · Requirements: R5 (ID printed on certificate), R8 · Evidence: RESEARCH §5, §6

## Context
A third party (e.g. an employer) handed a certificate PDF needs to check that we issued it, to that person. Separately, ADR-004 needs a stable per-item ID **before** rendering so retries are idempotent. Both needs are met by one identifier.

Scope of the claim: certificates are **verifiable**. A holder or third party can look a certificate up against our records. We make **no claim that the PDF itself is tamper-evident.** Anyone can edit a PDF. What we offer is that the edit is detectable by comparing the document to the record returned by `/v1/verify/{id}`.

## Options
1. **DB integer item ID printed on certificate** (simplest). Enumerable: anyone can walk `/verify/1..N` and harvest every recipient's name.
2. **Random 128-bit certificate ID + DB lookup** (chosen). Unguessable, so not enumerable. The DB is the source of truth.
3. **Random ID + HMAC-SHA256 signature** over `(certificate_id, name, issued_on)` embedded in the QR URL.
4. **Asymmetric signatures (Ed25519) / PDF digital signatures (PAdES)**. Verifiable fully offline by third parties. Needs a PKI/key-distribution story.

## Criteria
Real verification value, explainability, operational cost (secrets to manage).

## Decision
**Option 2.**
- `certificate_id = secrets.token_hex(16)` (128 bits from the OS CSPRNG), assigned at item insert for valid rows only. Column is `UNIQUE`.
- Printed on the certificate as text and as a QR code (ReportLab built-in widget) encoding `{PUBLIC_BASE_URL}/v1/verify/{certificate_id}`. `PUBLIC_BASE_URL` defaults to `http://localhost:8000`.
- `GET /v1/verify/{certificate_id}` is public:
  - known **and** `succeeded` → `200 {"valid": true, "certificate_id", "recipient_name", "title", "issuer", "issued_on"}`
  - anything else (unknown, malformed, pending, failed, rejected) → `200 {"valid": false}`. One uniform shape, so the endpoint doesn't reveal *why*. A lookup of a malformed ID never touches the filesystem.
- **Privacy:** the verify response never includes email. Every field it does return is already printed on the document the verifier holds.

## Why HMAC (option 3) was rejected
Verification in this system is **online**: the verifier calls `/v1/verify/{id}` and the DB answers. In that model:
- The random 128-bit ID already makes forging a *new* valid certificate infeasible (2^128 guesses).
- Editing the name on a *real* certificate is caught because `/verify` returns the **stored** name, and the verifier compares it with the PDF.
- An HMAC over the same fields proves only what the DB lookup already proves, and it costs a secret (`SIGNING_KEY`): generation, persistence across restarts, rotation, leak response, and a constant-time compare. That's operational surface with no gain in security.

**Condition under which HMAC (or option 4) would be added:** a requirement for **offline verification without DB access**. Examples: a verifier app that must work with no network to our API, or certificates that must remain verifiable after the issuing database is retired. Then the signature, not the DB, carries the proof. Even then, an asymmetric signature (option 4) beats HMAC, because HMAC verification needs the secret key, so every verifier could also forge. We'd choose Ed25519 with a published public key.

## Tests
`test_verify.py`: valid round trip for a succeeded item; unknown ID → `{"valid": false}`; malformed ID (`../x`, 1,000 chars) → `{"valid": false}`; pending/failed/rejected → `{"valid": false}`; response body never contains the recipient's email; the URL passed to the QR widget equals `{PUBLIC_BASE_URL}/v1/verify/{certificate_id}` (asserted at the renderer seam, no image decoding); certificate ID text is extractable from the PDF.

## At 100× scale
Cache verify responses at a CDN, since they're immutable once succeeded. Add revocation (`revoked_at` column → `valid: false, reason: "revoked"`). Add Ed25519 only on the offline-verification condition above.
