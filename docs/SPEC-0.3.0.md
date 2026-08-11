# Spec: remaining Hub-feature gaps

Status: **approved**. Items 1–4 are the v0.3.0 scope; 5–7 are deferred to v0.4.0.

Ordered by what I'd build first. Each item states the problem, the design, the
config it introduces, how it fails, and what would have to be true for me to
call it verified.

---

## 1. Ref revalidation — the only correctness bug

### Problem

A cache hit is served entirely from disk. `resolve_local("main", …)` reads
`refs/main`, finds the file under that commit, and returns it. Upstream is never
consulted, and because HEAD is answered the same way, the client is told the old
commit *is* `main`.

If someone updates `main` on the Hub, every node keeps the stale bytes until the
repo is evicted. Commit-pinned requests are unaffected — a sha is immutable.

### Design

Revalidate the **ref**, not the file. Per-file revalidation would put an upstream
HEAD on every cache hit and destroy the hot path.

A process-local map `(repo_type, repo_id, ref) -> (commit, checked_at)`:

- Revision matching `^[0-9a-f]{40}$` → immutable, never revalidated.
- Otherwise, on a hit, if `now - checked_at < XHC_REF_TTL`, serve from cache
  unchanged (this is the common case and stays a pure disk read).
- If stale, resolve upstream once: `GET /api/{type}s/{repo}/revision/{ref}`,
  read `sha`.
  - same as local `refs/{ref}` → refresh `checked_at`, serve from cache
  - different → treat this request as a **miss under the new commit** and ingest;
    `hf_hub_download` writes the new `refs/{ref}` itself
  - upstream 404 / 403 / unreachable → **serve stale**, log at debug, and back
    off `checked_at` so we don't re-ask every request
- Revalidation is single-flight per `(repo, ref)`, so forty nodes rotating
  together cause one upstream call, not forty.

Fail-open on upstream trouble is deliberate and consistent with orphan
retention: a deleted repo 404s forever, and must keep serving.

### Config

| variable | default | meaning |
|---|---|---|
| `XHC_REF_TTL` | `300` | seconds a ref→commit mapping is trusted; `0` disables revalidation entirely (current behaviour, and the right setting for a pure archive) |

### Cost

One small Hub API call per *repo+ref actually requested* per TTL window. It is
lazy — repos nobody asks for cost nothing. 500 active repos at 300 s is under
2 req/s.

### Failure modes

| situation | behaviour |
|---|---|
| upstream moved `main` | next request after TTL ingests the new commit |
| upstream unreachable | serve stale, log, retry after TTL |
| repo deleted (orphan) | serve stale forever — required by retention |
| sha-pinned request | never revalidated, zero overhead |
| `XHC_REF_TTL=0` | today's behaviour exactly |

### Verified when

- stub upstream flips `sha`; a request inside the TTL serves old bytes, a
  request after it serves new bytes
- sha-pinned requests make zero upstream calls (assert on a request counter)
- unreachable upstream serves stale rather than erroring
- N concurrent requests across the TTL boundary produce exactly one upstream call
- hot-path latency on a fresh mapping is unchanged (no added syscalls)

**Effort:** ~120 lines + tests.

### Decided

`XHC_REF_TTL` defaults to **300**, settable at container start like every other
knob. This is a behaviour change on upgrade — mutable refs begin tracking
upstream — and `XHC_REF_TTL=0` restores today's serve-forever behaviour for
anyone running Muninn as a pure archive.

---

## 2. Repo allow/deny — the only one with a blast radius

### Problem

Any host that can reach the port can cause an ingest of any repo. Nothing stops
a typo pulling a 500 GB dataset onto the array, or a model you would rather not
have on the box. There is no per-client auth on the data path — the management
token only guards `/_cache/*`.

### Design

Policy evaluated **before an ingest is started**, on the miss path and on
`/_cache/prewarm`.

- Patterns are `fnmatch` globs over the repo key: `models/meta-llama/*`,
  `datasets/*`, `models/*/*-gguf`.
- `XHC_INGEST_POLICY=open` (default) — everything allowed unless denied.
- `XHC_INGEST_POLICY=allowlist` — nothing allowed unless it matches an allow
  pattern.
- **Deny always wins** over allow.
- Refusal is `403` with `x-xhc-policy: denied` and a plain-text reason. It
  deliberately does **not** borrow an HF `X-Error-Code`: this is our policy, not
  the Hub's answer, and mislabelling it as `GatedRepo` would send people hunting
  for a token that would not help.

**Scope.** Policy gates *ingest*, not *serving*. A repo already in the cache
keeps serving even if a later policy change would forbid fetching it — so
tightening policy cannot break a running fleet. `XHC_POLICY_SCOPE=all` extends
enforcement to cache hits for anyone who wants the stricter reading.

**Size guard.** `XHC_MAX_FILE_BYTES` refuses an ingest when the upstream HEAD
reports a larger file. This uses metadata we already fetch on the miss path, so
it costs nothing extra. A whole-repo size cap is deliberately *not* specced: it
needs `?blobs=true` on every miss, which is a real cost for a guard that a
per-file cap mostly covers.

`/_cache/prewarm` honours policy unless called with `"force": true` — the
management API is already authenticated, so an operator can override
deliberately.

### Config

| variable | default | meaning |
|---|---|---|
| `XHC_INGEST_POLICY` | `open` | `open` \| `allowlist` |
| `XHC_ALLOW_REPOS` | unset | comma-separated globs |
| `XHC_DENY_REPOS` | unset | comma-separated globs; wins over allow |
| `XHC_POLICY_SCOPE` | `ingest` | `ingest` \| `all` |
| `XHC_MAX_FILE_BYTES` | unset | refuse ingest of a file larger than this |

Runtime edits via `GET`/`PUT /_cache/policy`, persisted to `.xhc/policy.json`
so they survive a restart. Env provides the initial value; the file wins once
written, same precedence as pins.

### Verified when

- allow/deny matching incl. deny-beating-allow, on all three repo types
- `allowlist` mode refuses an unlisted repo with `403` + `x-xhc-policy`
- a repo already cached still serves after being denied (scope=ingest)
- `scope=all` blocks the hit too
- `prewarm` refuses without `force`, succeeds with it
- `XHC_MAX_FILE_BYTES` refuses before any bytes are transferred

**Effort:** ~150 lines + tests.

---

## 3. `tree` synthesis — finishes the orphan story

### Problem

`repo_info` is synthesized, so `snapshot_download` works on an orphan. But
`list_repo_files` / `list_repo_tree` call
`/api/{type}s/{repo}/tree/{rev}` (confirmed in `HfApi.list_repo_tree`), which is
not, so enumerating a deleted repo *that* way still fails.

### Design

Same trigger and guard rails as repo-info synthesis: only on an upstream 404,
only when the snapshot is held, tagged `x-xhc-synthesized`.

Response is a JSON array of entries:

```json
[{"type": "file", "path": "config.json", "size": 807, "oid": "<etag>"},
 {"type": "directory", "path": "onnx"}]
```

- `oid` is the blob's git sha — which, in the HF cache layout, is the symlink
  target's filename, i.e. the value we already recover as the ETag.
- `size` from `stat()`.
- `recursive=true` walks the whole snapshot; `false` lists immediate children
  with directories collapsed.
- `expand=true` adds `lastCommit: null` and `securityFileStatus: null`; we
  cannot invent those, and null is honest.
- **Pagination:** return one page with no `Link` header. `huggingface_hub`'s
  `paginate()` stops when `Link` is absent, so this is correct rather than a
  shortcut.

### Verified when

- `list_repo_files` returns the full list for an orphan, matching the snapshot
- `recursive=false` returns directories, not a flattened tree
- `oid` equals the ETag the resolve path serves for the same file
- live repos still get the Hub's real tree, untagged

**Effort:** ~80 lines + tests.

---

## 4. Conditional requests (`304`)

### Problem

We never answer `304 Not Modified`. A client revalidating with `If-None-Match`
gets a full body. Costs bandwidth on the LAN leg and prevents any HTTP cache
placed in front of Muninn from doing its job.

### Design

On a cache hit, before building a body: if `If-None-Match` matches the ETag we
would return, respond `304` with the ETag and `x-repo-commit`, no body. `*` also
matches when we hold the file.

Deliberately **not** implementing `If-Modified-Since`: the HF cache layout has no
trustworthy mtime — blobs keep filesystem timestamps from ingest, not from the
Hub — so any answer would be a guess.

`If-Range` is out of scope; we do not need it while ETags are strong and
immutable per blob.

**Effort:** ~30 lines + tests. Verified by asserting a `304` with no body on a
matching ETag, a `200` on a differing one, and correct interaction with `Range`.

---

## 5. Dataset viewer caching

### Problem

`/api/datasets/{id}/splits`, `/parquet`, `/rows` are proxied. Streaming a
dataset works (it resolves through paths we cache, now with ranges), but
anything that *browses* needs the Hub live — and on an orphaned dataset it is a
dead end.

### Design

Cache only the endpoints that are small and stable per revision:

- `/api/datasets/{id}/splits`
- `/api/datasets/{id}/parquet`

Store under `.xhc/viewer/{repo}/{endpoint}.json` with a fetch timestamp. Serve
from cache when fresh, or when upstream 404s and we hold a copy (tagged
`x-xhc-synthesized`, same as repo info).

**`/rows` is explicitly not cached.** It is query-dependent, paginated and
unbounded; caching it well is a different project, and caching it badly means
serving wrong rows.

Note the auto-converted parquet branch (`refs/convert/parquet`) is just a
revision, so it already works through the normal resolve path — no work needed.

| variable | default | meaning |
|---|---|---|
| `XHC_VIEWER_CACHE_TTL` | `3600` | seconds; `0` disables |

**Effort:** ~100 lines + tests.

---

## 6. Metrics endpoint

`/_cache/status` is JSON only. For a fleet service that is a gap — there is no
way to alert on hit rate collapsing or the array filling.

`GET /metrics`, Prometheus text format, no new dependency (the format is
trivial to emit by hand):

```
muninn_cache_hits_total{result="hit|miss|synthesized|denied"}
muninn_ingest_bytes_total
muninn_ingest_jobs{state="running|pending"}
muninn_cache_bytes / muninn_cache_capacity_bytes
muninn_orphans / muninn_orphan_bytes
muninn_scan_duration_seconds
muninn_upstream_requests_total{status="..."}
```

Counters live in the process, so they reset on restart — acceptable for rates,
and the gauges are all derived from disk anyway.

**Effort:** ~80 lines + tests.

---

## 7. Per-user attribution

One org token collapses every edge node into a single identity, so "who pulled
Llama" is unanswerable. Two options:

**a. Client header (recommended).** Log an `X-Muninn-Client` header, set per
node via `HF_HUB_USER_AGENT` or a wrapper. Trivial, no auth surface, but it is
self-reported and therefore not an audit trail.

**b. Client-supplied tokens.** Require each node to send its own
`Authorization`, resolve it via `/api/whoami-v2` (cached), and log the real user.
That is a genuine audit trail, but it re-introduces exactly the Hub credentials
on edge nodes that this design removed, and adds a token-validation path.

I would do (a) unless the requirement is compliance-grade, in which case neither
is really the answer and Model Gateway is.

**Effort:** (a) ~20 lines. (b) ~120 lines plus a security review.

---

## 8. Not proposed, and why

- **Capturing uploads.** Would mean intercepting `create_commit` plus the LFS
  and Xet upload paths and writing into the cache layout as if we were the Hub.
  Large, and every bug is a corrupted push. Uploads should keep passing through.
- **Gated-licence acceptance.** Requires a per-user consent flow; the cache is
  the wrong place for it. Accept once with the org token, as now.
- **Symlink-free filesystems.** The ETag-from-symlink recovery fails, falling
  back to an upstream HEAD — which on an orphan yields no ETag and a client
  refusal. The fix is an ETag sidecar written at ingest. Real, but only bites on
  filesystems nobody runs an 80 T NVMe array on; happy to add if it matters.
- **Multi-node / HA.** A different architecture, not a feature.

---

## Suggested cut

**v0.3.0** — items 1, 2, 3, 4. Correctness, safety, and the finished orphan
story. The two behaviour changes on upgrade are `XHC_REF_TTL` defaulting to
`300` (if we choose that) and `403`s appearing once a policy is set; both are
opt-out.

**v0.4.0** — items 5, 6, 7a. Observability and dataset breadth, once datasets
are actually in use and the shape of the need is clearer.
