# Changelog

**Generated from the annotated git tags — do not edit by hand.**

The tag is the source of truth: it is written at release time and cannot drift
from the commit it names. Regenerate with `scripts/gen_changelog.py > CHANGELOG.md`.

Images are published to `ghcr.io/skibare87/muninn`. Only the full `X.Y.Z` tag is
immutable; `X.Y`, `latest` and `edge` all move.


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
