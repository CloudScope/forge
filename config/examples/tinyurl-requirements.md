# TinyURL / Snipr — Product Requirements Document

## 1. Overview

Build a scalable **URL Shortener Platform** (working name: Snipr) similar to TinyURL / Bitly.

Users and API clients submit long URLs and receive short links. Visiting a short link redirects to the original target. The platform must support analytics, custom aliases, expiration, QR codes, rate limiting, and admin operations.

## 2. Business Goals

- Reduce friction when sharing long URLs
- Provide click analytics for campaigns
- Offer a secure developer API with API keys
- Operate at high redirect volume with low latency

## 3. Functional Requirements

### FR-01 Short URL generation
System shall generate a unique short code for a validated target URL.

### FR-02 Redirect
`GET /{code}` shall return HTTP 302 to the target URL on the cache-first path.

### FR-03 Custom aliases
Authenticated users may request a custom alias (org-scoped uniqueness).

### FR-04 Analytics
Record click events asynchronously and expose daily aggregates per link.

### FR-05 Expiration
Links may include `expires_at`. Expired links return HTTP 410.

### FR-06 QR code generation
Provide PNG/SVG QR codes for a short link.

### FR-07 Rate limiting
Enforce per-API-key and per-organization quotas on create/admin APIs.

### FR-08 Bulk URL creation
Support bulk create with per-row success/error results and idempotency keys.

### FR-09 URL validation & preview
Validate http(s) targets only; provide a safe preview endpoint with SSRF protections.

### FR-10 Admin APIs
Disable/enable links; create/rotate API keys; audit mutations.

### FR-11 Health & metrics
Expose `/healthz`, `/readyz`, and Prometheus `/metrics`.

## 4. Non-Functional Requirements

- Redirect p99 < 50ms at origin (cache hit path)
- Availability 99.99% for redirect service
- Horizontal scale to 100k redirect RPS per region
- Strong uniqueness of short codes
- Audit logging for all mutations
- Encryption in transit (TLS) and at rest

## 5. Constraints & Assumptions

- Redirect happy path must not synchronously wait on the primary database
- Analytics must not block redirects (outbox / async pipeline)
- API keys stored hashed only
- MVP is single primary region; multi-region is phase-2

## 6. Acceptance Criteria

- Create link returns a unique short URL
- Redirect resolves via cache-first strategy
- Invalid schemes (`javascript:`, `data:`) are rejected
- Load test: redirect smoke at high RPS without error budget breach
- Runbooks exist for latency spike and cache stampede

## 7. Out of Scope (MVP)

- Custom branded domains
- SSO / OIDC admin login (phase-2)
- Warehouse export connectors
