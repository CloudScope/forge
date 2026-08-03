# Reference HLD — URL Shortening Service (TinyURL-class)

Canonical high-level design reference for the Forge Architecture Agent.
Source patterns: Educative *Grokking Modern System Design* — System Design: TinyURL.
Adapt names/features to the uploaded PRD; keep the building blocks, estimation method, and NFR techniques.

---

## 1. Problem

A URL shortening service creates a short alias (short link) for a long URL. Clicking the short link redirects to the original address.

**Advantages:** easier to share/type; fits character limits; cleaner appearance.
**Disadvantages:** third-party domain dependency; branding dilution; reliability/security of the short domain reflects on the brand.

---

## 2. Functional requirements

| ID | Requirement |
|----|-------------|
| FR1 | **Short URL generation** — unique short alias for a given long URL |
| FR2 | **Redirection** — resolve short link → original URL (HTTP 301/302) |
| FR3 | **Custom short links** — optional user-chosen alias (validated, unique) |
| FR4 | **Deletion** — authorized delete of a short link |
| FR5 | **Update** — authorized update of the long URL behind a short link |
| FR6 | **Expiry** — default TTL; optional custom expiry; purge expired after retention (e.g. 5 years) |

Expired keys are not reused forever in search indexes — retain then delete to bound index growth and latency.

---

## 3. Non-functional requirements

| NFR | Meaning |
|-----|---------|
| **Availability** | Redirection must stay up; short domain is in every URL → high fault tolerance |
| **Scalability** | Horizontal scale for writes and (especially) reads |
| **Readability** | Short codes easy to read/type (no ambiguous glyphs) |
| **Latency** | Redirection very low latency |
| **Unpredictability** | Codes must not be guessable (avoid naive sequential IDs) |

Predictable sequential codes are a security risk for private links → prefer random selection from ID ranges (or salted encoding).

---

## 4. Back-of-the-envelope (reference assumptions)

Use the method below; recompute from PRD traffic numbers when available.

**Assumptions (reference):**
- Write:read = 1:100
- 200M new shortenings / month
- ~500 bytes / mapping entry
- 5-year retention
- 100M DAU (capacity proxy)

**Derived (reference):**
| Metric | Value |
|--------|-------|
| Entries in 5 years | 12B |
| Storage | ~6 TB |
| Write QPS | ~76 /s |
| Read QPS | ~7.6K /s |
| Ingress bandwidth | ~304 Kbps |
| Egress bandwidth | ~30.4 Mbps |
| Cache (80/20 of daily redirects) | ~66 GB |
| App servers (rough) | order of hundreds–thousands at peak |

Always publish `capacity_estimate` with assumptions, formulas, and results.

---

## 5. Building blocks

| Block | Role |
|-------|------|
| **Application servers** | Shorten / redirect / delete / update logic |
| **Database(s)** | Users + short↔long mappings (read-heavy, horizontal scale) |
| **Sequencer / unique ID generator** | 64-bit unique IDs for new codes |
| **Base-58 encoder** | Readable alphanumeric short codes |
| **Load balancers** | GSLB + local LBs; distribute across regions/AZs |
| **Cache** | Hot mappings (e.g. Memcached/Redis); prefer DC-local cache |
| **Rate limiter** | Per `api_dev_key` / tenant (e.g. fixed-window or token bucket) |

Optional product extensions (only if PRD asks): analytics pipeline, QR, CDN edge cache, object storage backups.

---

## 6. System APIs (abstract)

### Shorten
`shortURL(api_dev_key, original_url, custom_alias=None, expiry_date=None)` → short URL | error

### Redirect
`redirectURL(url_key)` → 302/301 to original URL | 404

### Delete
`deleteURL(api_dev_key, url_key)` → confirmation | error

Map these to concrete REST (e.g. `POST /v1/links`, `GET /{code}`, `DELETE /v1/links/{code}`) in API agent artifacts; Architecture must name the flows.

---

## 7. High-level components

### 7.1 Database

- Store: user/account metadata; short→long mappings; used/unused ID tracking for custom aliases.
- Mappings are independent → **NoSQL** (e.g. MongoDB) is a strong default for read-heavy scale:
  - Leader–follower replicas for reads
  - Atomic unique indexes / duplicate-key errors to prevent collisions
- Eventual consistency across geo replicas is acceptable for redirect (create→first-click delay).
- Alternatives (Postgres+sharding/Vitess) are fine when PRD needs strong relational/SQL — justify in ADRs.

### 7.2 Short URL Generator (SUG)

1. **Sequencer** — unique 64-bit base-10 ID  
2. **Base-58 encoder** — convert ID → short alphanumeric string  

**Why Base-58 (not Base-64):** drop ambiguous / URL-unsafe chars (`0`, `O`, `I`, `l`, `+`, `/`). Alphabet ≈ A–Z, a–z, 1–9 excluding lookalikes.

**Length:** default min ~6 chars; 64-bit ID → up to ~11 Base-58 chars. Start sequencer from ≥ 1e9 so encoded length ≥ 6.

**Unpredictability:** assign ID *ranges* to app servers; pick IDs **randomly** from the unused pool (not sequential issue). Optional: salt before encode.

**Custom aliases:** validate format (max length, charset); check uniqueness; decode Base-58 → mark underlying ID as used so the sequencer never reissues it.

**Sequencer lifetime:** at ~2.4B IDs/year, 64-bit range lasts effectively forever for planning purposes.

### 7.3 Load balancing

- GSLB for multi-region failover + local LBs per DC.
- App tier and DB shard with consistent hashing where needed.

### 7.4 Cache

- Cache hot short→long mappings (80/20).
- DC-local cache (not one global cache) for latency.
- On miss: DB → populate cache → redirect.
- Invalidate / TTL on delete, update, expire.

### 7.5 Rate limiter

- Keyed by `api_dev_key` (or org/API key).
- Protect create/delete APIs; optional softer limits on redirect abuse.

---

## 8. Workflows

### Shorten
Client → LB → App → Rate limit → SUG (ID + Base-58) → persist mapping (+ used ID) → return short URL.  
Custom path: validate alias → uniqueness check → mark ID used → persist.

### Redirect
Client → LB → App → **Cache lookup** → (hit) 302; (miss) DB → populate cache → 302.  
Never block redirect on analytics or secondary side-effects.

### Delete / Expire
AuthZ → delete mapping (+ cache invalidate). Background job purges expired rows after retention.

### Update
AuthZ → update long URL → invalidate cache.

---

## 9. Design diagram (logical + AWS mapping)

Forge System context must render this as a **layered** diagram (not a 3-box sketch).

```
[Clients: Web / Mobile / API (api_dev_key) / Operators (IAM)]
        │
        ▼
[Edge]  Amazon Route 53 (DNS/GSLB) → Amazon CloudFront (CDN) → AWS WAF
        │
        ▼
[Ingress]  Application Load Balancer → Rate limiter (API GW usage plans / token bucket)
           TLS via AWS Certificate Manager
        │
        ▼
[App — EKS/ECS multi-AZ]
   Link API (shorten/update/delete) │ Redirect API (cache-first) │ SUG (sequencer + Base-58)
        │
        ├─► Amazon ElastiCache (Redis/Memcached) — DC-local hot mappings
        ├─► Amazon DocumentDB (Mongo) / DynamoDB — mappings, users, used IDs
        └─► (async) Amazon MSK / Kinesis → analytics (never on redirect path)
        │
        ▼
[Platform]  Amazon S3 backups │ Secrets Manager/SSM │ CloudWatch │ Route 53 health failover
```

| Building block | AWS service |
|----------------|-------------|
| DNS / GSLB | Amazon Route 53 |
| CDN | Amazon CloudFront |
| WAF | AWS WAF |
| Local LB | Application Load Balancer |
| App servers | Amazon EKS or ECS (Auto Scaling) |
| Cache | Amazon ElastiCache (Redis/Memcached) |
| Database | Amazon DocumentDB or DynamoDB |
| Backups | Amazon S3 |
| Observability | Amazon CloudWatch |
| Analytics bus (optional) | Amazon MSK / Kinesis |

Regions: primary + DR (e.g. us-east-1 / us-west-2). Eventual geo consistency OK after create.

---

## 10. NFR compliance techniques

| NFR | Techniques |
|-----|------------|
| Availability | Replication (DB/cache/app); GSLB; rate limits vs DoS; S3-style backups |
| Scalability | Horizontal app scale; DB sharding + consistent hashing; NoSQL or sharded SQL; vast 64-bit ID space |
| Readability | Base-58; no ambiguous/non-URL-safe chars |
| Latency | Cache-first redirect; fast encode; async replication lag OK after create; avoid DB on hot path |
| Unpredictability | Random ID from range (not sequential); optional salt |

---

## 11. Architecture Agent output expectations

When the PRD is a URL shortener (or similar alias service), HLD/LLD/ADRs **must** cover:

1. Requirements (FR/NFR) traced to PRD  
2. Capacity estimate (storage, QPS, bandwidth, cache memory, servers) with assumptions  
3. Building blocks list matching §5  
4. APIs for shorten / redirect / delete (/ update)  
5. SUG: sequencer + encoding (Base-58 preferred for readability; Base62 acceptable if ADR justifies)  
6. Cache-first redirect sequence  
7. Custom alias + used-ID collision avoidance  
8. Rate limiting + multi-region consistency stance  
9. ADRs for DB choice, encoding, cache, ID strategy, outbox/async side-effects  
10. Perf budget: redirect p99, cache hit ratio, create p99  

Tenets that must appear for URL shorteners:
- cache-first redirect path  
- redirect happy path must not wait on DB (or only on cache miss)  
- outbox / async for analytics and non-critical mutations side-effects  
- unpredictable short codes  
- readable encoding (Base-58 or justified alternative)

Do **not** invent unrelated products. Scale estimates to the PRD; use this document as the methodology and component checklist.
