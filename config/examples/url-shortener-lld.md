# Reference LLD — URL Shortener (Bitly clone)

Canonical low-level design reference for the Forge Architecture Agent.
Source patterns: CodeToDeploy *Low-Level Design Masterclass: Building a URL Shortener (Bitly Clone)*.
Adapt names to the PRD; keep class responsibilities, ID strategy, APIs, cache, and redirect semantics.

---

## 1. Clarifying scope (LLD interview checklist)

| Question | Typical answer |
|----------|----------------|
| Custom aliases? | Yes, optional; uniqueness check |
| Expiry? | Default TTL (e.g. 1 year); custom expiry optional |
| Analytics? | Async only — never on redirect hot path |
| Auth? | `api_dev_key` / API key for create/delete |
| Code length? | ~7 Base62 chars (or 6–11 Base-58) |
| Redirect code? | Prefer **302** if analytics/expiry matter; **301 + short Cache-Control** if browser-cache OK |

---

## 2. Short-code generation strategies

### Reject / interview trap: Hashing (MD5 / SHA)

- Truncate hash → collisions under scale  
- Full hash → too long for short links  
- Collision resolution adds write latency  

### Industry standard: Distributed unique ID + Base62

1. Allocate a globally unique 64-bit ID (Snowflake / range allocator)  
2. Encode ID → Base62 (`0-9a-zA-Z`) compact alphanumeric string  
3. Persist `{short_code → long_url, …}`  

**Snowflake (preferred):** timestamp | worker | sequence — no central counter hotspot, roughly time-ordered.

**Base-58 alternative:** drop ambiguous chars (`0/O/I/l`) for readability (HLD readability NFR). Document choice in ADR.

---

## 3. Core classes / modules

| Module | Responsibility |
|--------|----------------|
| `UrlController` / `RedirectController` | HTTP: shorten, redirect, delete |
| `UrlShorteningService` | Orchestrate create + lookup |
| `IdGenerator` (Snowflake) | Unique 64-bit IDs |
| `Base62Encoder` / `Base58Encoder` | ID ↔ short code |
| `UrlRepository` | Persist / load mappings |
| `CacheService` (Redis) | Hot short→long lookups |
| `RateLimiter` | Per API key limits on writes |
| `UrlEntity` | Persistence model |

---

## 4. Data model (entity)

```
UrlMapping
  id              BIGINT / ULID PK (Snowflake)
  short_code      VARCHAR(16) UNIQUE NOT NULL
  original_url    TEXT NOT NULL
  user_id         VARCHAR / FK nullable
  custom_alias    BOOLEAN
  created_at      TIMESTAMP
  expires_at      TIMESTAMP nullable
  status          ENUM(active, disabled, expired)
  click_count     BIGINT optional (or separate analytics store)
```

Indexes: unique(`short_code`); optional (`user_id`, `created_at`); TTL job on `expires_at`.

---

## 5. API contracts

### Shorten
`POST /api/v1/shorten`  
Body: `{ "url": "...", "customAlias"?: "...", "expiresAt"?: "..." }`  
Auth: API key  
Response `201`: `{ "shortUrl": "https://host/abc123", "code": "abc123", "expiresAt": ... }`

### Redirect
`GET /{code}` → `302 Found` + `Location: original_url` (or 301 with max-age policy)  
`404` if missing/expired/disabled

### Delete / disable
`DELETE /api/v1/links/{code}` — AuthZ required; invalidate cache

---

## 6. Write path (LLD)

1. Validate URL + rate limit  
2. If custom alias → uniqueness check; else Snowflake ID → encode Base62  
3. Persist mapping (unique index on `short_code`)  
4. Optionally warm cache  
5. Return short URL  
6. Side effects (analytics outbox) async only  

---

## 7. Read path (LLD)

1. Parse `code` from path  
2. **Redis GET** `url:{code}`  
3. Hit → 302 Location  
4. Miss → DB by `short_code` → if found, SET cache (TTL) → 302  
5. Missing/expired → 404  
6. Emit click event to queue **after** response scheduled (never block)  

---

## 8. 301 vs 302 (interview trap)

| Code | Effect | Use when |
|------|--------|----------|
| **302** | Browser re-hits origin every click | Analytics, expiry, disable must work |
| **301** uncapped | Browser may cache forever | Avoid for Bitly-class analytics |
| **301 + Cache-Control: max-age=N** | Bounded browser cache | Performance with delayed analytics |

Default for analytics products: **302**.

---

## 9. Caching

- Redis key: `url:{short_code}` → original URL (and optional metadata)  
- LRU / TTL aligned with expiry  
- Invalidate on delete/update/expire  
- Cache-aside on redirect miss  

---

## 10. Scaling notes (LLD-relevant)

- Stateless app servers behind LB  
- Redis cluster for hot keys  
- Shard/partition DB by `short_code` hash at extreme scale  
- Snowflake worker IDs unique per instance  
- Async analytics (Kafka/MSK) off redirect path  

---

## Architecture Agent output expectations

Populate `lld` with:

- `services` (handlers, deps, invariants, methods)  
- `classes` / modules list  
- `entities` / data model  
- `apis` (shorten, redirect, delete) with request/response  
- `id_generation` (Snowflake + encoding)  
- `encoding` (Base62 or Base-58 + alphabet)  
- `cache` (Redis keys, TTL, invalidation)  
- `redirect_policy` (302 vs 301)  
- `write_flow` / `read_flow` step lists  
- `strategies_considered` (hash vs distributed ID)  
- `consistency`  

LLD HTML must render these as a masterclass-style page, not a thin service card list.
