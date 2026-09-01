# Changelog

**Generated from the annotated git tags — do not edit by hand.**

The tag is the source of truth: it is written at release time and cannot drift
from the commit it names. Regenerate with `scripts/gen_changelog.py > CHANGELOG.md`.

Images are published to `ghcr.io/skibare87/muninn`. Only the full `X.Y.Z` tag is
immutable; `X.Y`, `latest` and `edge` all move.


## v0.6.3 — 2026-09-01

An upstream auth failure no longer says "not found".

Upstream 401 and upstream 404 both rendered as 404 MANIFEST_UNKNOWN. Since 0.6.2
there are three states, not two, and they have three different fixes:

  upstream 401, no credentials configured  -> docker login on the CACHE host
  upstream 401, credentials rejected       -> wrong, expired or unscoped creds
  upstream 404                             -> genuinely not there

The middle state only became reachable when credentials started being sent, so
the fix that made the feature work also created a failure indistinguishable from
the other two.

The status code was chosen by measurement rather than by semantics, because the
docker CLI discards the body and the headers and prints only the status:

  404 -> "not found"                                <- the status is ERASED
  401 -> "unexpected status ...: 401 Unauthorized"
  502 -> "unexpected status ...: 502 Bad Gateway"
  403 -> "unexpected status ...: 403 Forbidden"

404 is the only status that hides itself, so an auth failure must not wear one.
An upstream auth failure is now 502 -- Muninn is a gateway that did not obtain a
valid response -- carrying x-xhc-upstream-status and x-xhc-upstream-auth
(unconfigured | rejected | n/a) for logs and curl. Failure is as fast as before:
404, 401 and 502 all fail in about 32ms, with no client retry.

401 is reserved for Muninn's own client-facing auth. "Authenticate to the cache"
and "the cache cannot authenticate upstream" are different actors with different
fixes.

ALSO FIXED: blobs and manifests disagreed. The blob path returned 401 for an
upstream 401 and FORWARDED UPSTREAM'S WWW-AUTHENTICATE, pointing a client at a
realm Muninn does not proxy -- a retry loop with no exit, present in every
release until now. Both paths now share one terminal answer and no challenge is
ever emitted.

Unchanged: the fail-open path. While a cached copy is held, an upstream 401 or
404 still serves it.


## v0.6.2 — 2026-09-01

XHC_REGISTRY_AUTH_FILE now actually works. It never had.

Muninn loaded the mounted docker credentials, logged them at startup, held them
in memory, and never sent them. `_basic_for()` -- the function that turns the
auth file into an Authorization header -- was reachable from exactly one place:
authenticating to a bearer TOKEN ENDPOINT. No code path put
`Authorization: Basic` on a registry request, so a registry speaking plain Basic
with no token endpoint could never be authenticated to at all.

0.6.1 stopped Basic challenges from wrongly entering the bearer dance and then
gave up, on the false premise that a Basic challenge was not satisfiable. It is:
the credentials are right there.

On a 401 whose scheme is Basic, Muninn now retries once with the credentials
configured for that upstream. Challenge-response rather than preemptive, so an
auth file cannot leak credentials to a registry that never asked for them. With
no credentials for that upstream, the registry's own 401 is handed back rather
than an invented answer.

Verified end-to-end against a real private registry: 401 on 0.6.1, 200 with this
change, same credential file.

WHAT THIS IS NOT. Muninn authenticates as ITSELF, using credentials the operator
mounts -- `docker login` on the cache host. It does not forward a client's
Authorization header upstream, and it never will: a cached hit consults no
credentials at all, so per-client authorization would be enforced on the miss
and silently absent on every hit after it. Anything that can reach the cache can
pull anything the cache holds. That is the trust model, deliberately.

Known: every request to a Basic upstream now costs a 401 and a retry.
Remembering the scheme per upstream is a separate change.


## v0.6.1 — 2026-09-01

A Basic auth challenge is no longer mistaken for a Bearer one.

Muninn returned 500 for a private registry whose challenge is
`Basic realm="Authorization Required"`, and a misleading 404 for another whose
realm happens to be a URL -- on identical inputs, both upstreams answering 401.

The guard meant to tell the two schemes apart could never do so. It tested
`challenge.get("Bearer") is None`, but the challenge parser matched only
key="value" pairs and the scheme token has no ="value", so it was never present
and the condition collapsed to "has a realm". Every Basic challenge entered the
bearer token dance. Where the realm was a URL that merely wasted a request;
where it was free text, httpx read it as a relative URL and urllib raised
ValueError from inside httpx's cookie handling -- which `except httpx.HTTPError`
does not catch.

The scheme is now parsed and matched case-insensitively per RFC 7235, and a
realm that is not an absolute http(s) URL is refused before it reaches httpx
rather than caught afterwards.

Unchanged and deliberate: an upstream 401 with nothing cached still renders as
404 MANIFEST_UNKNOWN. That is the fail-open behaviour orphan retention depends
on, and altering it is client-visible.

Muninn still cannot pull from registries requiring per-client credentials, by
design. This is about answering correctly when it cannot.

Found by a colleague while testing whether Muninn could front the private registries;
that question is settled separately as no.


## v0.6.0 — 2026-09-01

X-Muninn-Prewarm: warm the cache without receiving the bytes. 202 + job id, or
204 if already cached.

X-Muninn-Local-Only: answer from disk or 404, never contacting the Hub. All
three upstream call sites on the resolve path are suppressed, including ref
revalidation and the etag backfill on a hit -- not just the obvious miss.

Both together are a 400. They compose across requests: prewarm a set, then poll
with local-only to see what landed, with no management token and no upstream
request in the polling loop.


## v0.5.3 — 2026-09-01

Docs-only; no behaviour change from 0.5.2.

The v0.5.2 tag shipped a README whose digest-pinning example printed a concrete
digest belonging to 0.5.1. Two fixes for that landed after the tag, so the
release was wrong while main was right. A published tag must not be moved.

Adds three metrics caveats that were on internal documentation and not in the
public README: counters are volatile while cache gauges are durable; every
bytes_served_total figure before 0.5.2 is inflated by HEAD requests and must not
be compared across the boundary; and amplification is a derived ratio that needs
its raw counters published beside it.


## v0.5.2 — 2026-08-29

bytes_served_total counted HEAD requests. A HEAD carries the full
content-length and transfers no body; huggingface_hub HEADs every file in a
repo before downloading any of it. Measured: 5 HEADs of a 10,985-byte file
added exactly 54,925. Every historical served figure from this cache is
inflated by the metadata traffic that preceded the transfers. Ingested was
always correct.

HEALTHCHECK now exercises /metrics, not only /healthz.

Documents three limits found in production: the 46.57 GiB plain-HTTP ceiling
(decimal, not the 50 the constant's name implies), HF_HUB_DOWNLOAD_TIMEOUT's
10s default timing out the first node to want a model, and that only /healthz
is a health endpoint.


## v0.5.1 — 2026-08-29

Fix /metrics 500 during ingest (an internal issue).

muninn_ingest_bytes_inflight summed j.downloaded_bytes without calling it. An
uncalled method is truthy, so 'or 0' never fired and sum() raised
TypeError: int + method.

The generator is guarded by 'if j.state == "running"'. Idle, it is empty and
the endpoint is fine -- so the fault was unreachable in every test and every
quiet scrape, and fired for the whole life of any ingest. Observability was down
behind a green /healthz and 'Up 2 days (healthy)'.

Two further sites assigned an int over the same method, shadowing it on the
instance and making its type depend on whether the snapshot watcher had ticked.
Replaced with a real final_bytes field.

Three regression tests, one of which pins the old expression raising.


## v0.5.0 — 2026-08-26

Docker/OCI pull-through caching, sharing the array, policy and metrics with the
Hugging Face side but not its storage root.

Highlights:
- /v2/* pull surface for any upstream registry, addressed by path prefix
- content-addressed blobs verified on ingest; verbatim manifests
- mark-and-sweep GC with whole-closure pins; fails closed on unreadable state
- management API with prewarm, pins, evict and on-demand GC
- honest build provenance: source fingerprint, not a claimed image digest


## v0.4.0 — 2026-08-11

Observability and dataset breadth.

- GET /metrics: Prometheus exposition, hand-rolled, no new dependency. Counters
  for request results, upstream status, bytes served and ingested; gauges for
  cache size, capacity, orphans, active ingests, scan cost.
- Attribution via an optional X-Muninn-Client header, surfaced as a metric
  label. Self-reported, so attribution rather than an audit trail. Label
  cardinality capped at 200 with overflow to __other__.
- Dataset metadata cache: /api/datasets/{id}/parquet and /croissant, held with a
  TTL and still served when upstream 404s, so a deleted dataset stays
  describable.
- Opt-in datasets-server proxy at /datasets-server/*. That host is separate,
  huggingface_hub never calls it, and there is no HF_ENDPOINT equivalent, so
  nothing can be redirected there by config. Exposed under our own prefix
  instead of intercepting a public hostname. rows is proxied but never cached.

No breaking changes; every addition is off unless used.


## v0.3.0 — 2026-08-11

Correctness, safety, and the finished orphan story.

- XHC_REF_TTL (default 300): mutable refs are revalidated against upstream, so a
  moved `main` is no longer served forever. Ref-level with a TTL, not per file;
  sha-pinned requests cost nothing; single-flight per repo-and-ref; fails open so
  an unreachable or deleted upstream keeps serving.
- Ingest policy (XHC_INGEST_POLICY, XHC_ALLOW_REPOS, XHC_DENY_REPOS,
  XHC_POLICY_SCOPE, XHC_MAX_FILE_BYTES, and PUT /_cache/policy). Deny beats
  allow; gates ingest rather than serving; refusals are 403 + x-xhc-policy.
- Tree synthesis: list_repo_files now works on a repo deleted upstream, with oid
  equal to the ETag the resolve path serves.
- Conditional requests: 304 on a matching If-None-Match.
- Multi-range requests (multipart/byteranges) and Range on a cache miss, which
  matter for dataset workloads.

Upgrade notes: ref revalidation is on by default (XHC_REF_TTL=0 restores the old
serve-forever behaviour), and 403s appear once a policy is set.


## v0.2.0 — 2026-08-11

Retention and archive behaviour for repos that disappear upstream.

- XHC_ORPHAN_POLICY (retain|evict, default retain): repos deleted or gated
  upstream are exempt from LRU eviction. A live repo can be re-fetched; an
  orphan cannot, so evicting one is irreversible.
- Upstream liveness sweep on XHC_ORPHAN_CHECK_INTERVAL (6h). Classification is
  biased toward keeping data: only an unambiguous 200 un-marks a repo, so an
  outage or rate limit cannot turn an archive into eviction fodder.
- Orphans stay fully usable: when upstream 404s a repo we hold, the repo-info
  listing is rebuilt from the cached snapshot so snapshot_download still works.
  Tagged x-xhc-synthesized / xhcSynthesized so an archived answer is never
  mistaken for a live one.
- DELETE /_cache/repos takes an optional revision and clears the orphan mark.
  Pins stay absolute; the explicit unpin is the acceptance step.
- GET /_cache/orphans, POST /_cache/orphans/check.

Also in this release: upstream 404/403 are passed through with X-Error-Code
instead of being reported as 502, plus a short-TTL negative cache for absent
optional files.


## v0.1.0 — 2026-08-08

v0.1.0 - first release

Hugging Face edge cache with split WAN/LAN protocol paths: ingests from the Hub
over the WAN using native Xet (parallel range GETs), serves the LAN as plain
whole-file HTTP off NVMe.

- single-flight coalescing; N concurrent cold clients share one upstream fetch
- stream/redirect/wait miss policies, stream by default
- client Xet negotiation blocked so bytes cannot bypass the cache
- upstream 404/403 passed through with X-Error-Code, plus a short-TTL negative
  cache for absent optional files
- LRU eviction with absolute repo-level pinning, adaptive scan TTL
- management API for prewarm, pins, jobs, eviction
