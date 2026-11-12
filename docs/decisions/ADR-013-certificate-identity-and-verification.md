# ADR-013: Certificate identity and verification — unguessable ids now, public verification deferred

Status: Accepted (identity implemented; a public verification endpoint and QR code are **not** implemented) · Requirements: R5 (id printed on the certificate), R8

## Context
Two needs share one identifier:
- ADR-004 needs a stable per-item id **before** rendering, so retries and crash recovery write the same file at the same path.
- A third party (for example an employer) handed a certificate PDF may want to check that it was issued, to that person.

The second need is a feature beyond the assignment. This record explains the identity scheme that is implemented and how verification would be added without changing it.

## Options
1. **Database integer item id printed on the certificate** (simplest). Enumerable: anyone could walk `/verify/1..N` and harvest every recipient's name.
2. **Random 128-bit certificate id + database lookup** (chosen). Unguessable, so not enumerable. The database is the source of truth.
3. **Random id + HMAC-SHA256 signature** over `(certificate_id, name, issued_on)`, for example embedded in a QR URL.
4. **Asymmetric signatures (Ed25519) or PDF digital signatures (PAdES)**. Verifiable fully offline by third parties, but needs a key-distribution story.

## Criteria
Real verification value, explainability, operational cost (secrets to manage), privacy.

## Decision
**Option 2.**
- `certificate_id = secrets.token_hex(16)` (128 bits from the OS CSPRNG), assigned when the item row is inserted, for valid rows only. The column is `UNIQUE`. Job ids are likewise random (`uuid4`).
- The id is printed on the certificate as text ("Certificate ID: …") and is the only path to a PDF: `GET /v1/certificates/{certificate_id}` (ADR-009) and `{job_id}/{certificate_id}.pdf` on disk (ADR-008). Retry keeps the id, so links stay valid.
- Downloads require the API token, so the id is an unguessable handle, not a bearer credential for the public.

### If verification is added
A public `GET /v1/verify/{certificate_id}` would return `{"valid": true, "recipient_name", "title", "issuer", "issued_on"}` only for a known, `succeeded` certificate, and the same `{"valid": false}` for everything else (unknown, malformed, pending, failed, rejected), so it does not reveal why. It must never return the email, and a malformed id must never touch the file system. The claim would be that a certificate is **verifiable** against the issuer's records, not that the PDF is tamper-evident: anyone can edit a PDF, and what makes the edit detectable is comparing the document to the stored record.

### Why not HMAC (option 3) or signatures (option 4)
Verification here would be **online**: the verifier asks the service and the database answers. In that model the random 128-bit id already makes forging a *new* valid certificate infeasible, and editing the name on a *real* certificate is caught because the lookup returns the **stored** name. An HMAC over the same fields proves only what the lookup already proves, and it costs a secret (generation, persistence, rotation, leak response, constant-time compare) with no gain.

**Condition under which a signature would be added:** a requirement for **offline verification without database access**, for example certificates that must stay verifiable after the issuing database is retired. Then an asymmetric signature (option 4, Ed25519 with a published public key) beats HMAC, because HMAC verification needs the secret key, so every verifier could also forge.

## Consequences
- Unguessable ids mean a leaked id grants access only to holders of a valid API token, and public verification can be added later without migrating ids.
- Without a verification endpoint, a recipient's certificate cannot be checked by a third party today (README, "Known limitations").

## At 100× scale
Cache verification responses at a CDN, since they are immutable once a certificate has succeeded. Add revocation (a `revoked_at` column returning `valid: false`). Add Ed25519 signatures only on the offline-verification condition above.
