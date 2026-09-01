<p align="center">
  <img src="brand/muninn-banner.png" alt="Muninn — Hugging Face edge cache" width="820">
</p>

<p align="center">
  <a href="https://github.com/skibare87/muninn/actions/workflows/ci.yml"><img src="https://github.com/skibare87/muninn/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="license: MIT"></a>
  <a href="https://github.com/skibare87/muninn/pkgs/container/muninn"><img src="https://img.shields.io/badge/ghcr.io-muninn-2496ED?logo=docker&logoColor=white" alt="ghcr.io/skibare87/muninn"></a>
</p>

A Hugging Face edge cache for a fleet of GPU hosts backed by a large NVMe array.

*In the Norse telling, Odin's raven Muninn — "memory" — flies out each day and
returns with what it found. Same job here: fetch it once, remember it, and let
everyone else read from memory.*

**The one idea:** the WAN leg and the LAN leg want different protocols, so don't
serve both with one reverse proxy.

| leg | protocol | why |
|---|---|---|
| NAS ← Hugging Face | native **Xet**, parallel range GETs | 16–64 concurrent streams against the CDN. This is where Xet's speed actually comes from. |
| edge node ← NAS | plain HTTP, whole file | no chunk reassembly, no second chunk cache on the node. Just bytes off NVMe. |

A conventional caching reverse proxy (nginx, olah, dingospeed, Artifactory)
can't do this, because its upstream leg inherits whatever protocol the client
asked for. Disable Xet on the client to make the proxy cacheable and your WAN
pull collapses to a **single stream off the LFS bridge** — the well-known
~3 MB/s failure mode. This service sidesteps that by *ingesting* with the real
`huggingface_hub` client and *serving* with a dumb file server.

Edge nodes keep a small, disposable local cache and delete models freely: a
re-pull is a LAN-speed stream from the array.

## Quick start

A prebuilt multi-arch image (`linux/amd64` + `linux/arm64`) is published, so the
NAS does not need a toolchain:

```bash
docker pull ghcr.io/skibare87/muninn:0.6.0

docker run -d --name muninn -p 8080:8080 \
  -v /mnt/nvme/hf-cache:/cache \
  -v /var/lib/muninn/xet:/xet \
  -e HF_TOKEN=hf_xxx \
  -e XHC_CACHE_MAX_SIZE=70T \
  ghcr.io/skibare87/muninn:0.6.0
```

Or from source, which is also how you get the compose file's full env set:

```bash
cp .env.example .env      # set HF_TOKEN and XHC_CACHE_PATH
docker compose up -d --build
curl -s localhost:8080/_cache/status | jq
```

To run the published image under compose instead of building, replace the
`build: .` line in `docker-compose.yml` with
`image: ghcr.io/skibare87/muninn:0.6.0`.

Release notes for every version are in [CHANGELOG.md](CHANGELOG.md) and on the
[Releases page](https://github.com/skibare87/muninn/releases). Both are rendered
from the annotated git tag, which is written at release time and is the source of
truth — the changelog is regenerated, never hand-edited.

**Published tags** (multi-arch, `linux/amd64` + `linux/arm64`), built by
GitHub Actions on every version tag:

| tag | meaning |
|---|---|
| `0.6.0` | immutable — **pin this on a fleet** |
| `0.6` | latest patch in the 0.6 line; **moves** |
| `latest` | most recent tagged release; **moves** |
| `edge` | tracks `main`; expect breakage |

**Only the full `X.Y.Z` tag is immutable.** `X.Y` is not — publishing a new
patch re-points it, as `v0.5.1` did to `0.5` and `v0.5.2` did again an hour
later. `latest`, `X.Y` and `edge` all give every node whatever was pushed last,
with nothing to roll back to when a push goes wrong.

For a deployment you genuinely cannot have move under you, pin the **manifest
digest of the image you actually deployed**:

```
ghcr.io/skibare87/muninn@sha256:<digest you pulled>
```

Read it back from the running deployment rather than copying it from here:

```bash
docker inspect --format '{{index .RepoDigests 0}}' muninn
```

**This README deliberately does not print a concrete digest.** One was here
briefly and was stale one release later, in the paragraph warning about exactly
this. A digest in prose is a version pin wearing different clothes — and unlike
a digest in a compose file, which fails loudly on the next pull, **nothing ever
validates a digest in documentation.** It rots unread.

Keep concrete digests where they get exercised.

Point edge nodes at it:

```bash
export HF_ENDPOINT=http://nas.internal:8080
export HF_HUB_DISABLE_XET=1        # correct HERE (LAN side), wrong in the container
export HF_HUB_CACHE=/local/nvme/hf # small, disposable
hf download meta-llama/Llama-3.1-70B-Instruct
```

`HF_HUB_DISABLE_XET=1` belongs on the **edge nodes only**. On the LAN leg Xet
buys nothing and costs CPU plus a redundant `~/.cache/huggingface/xet` chunk
cache on every node — exactly the space you're trying to reclaim. The container
logs a warning if it sees this variable set on itself.

## How requests are handled

Metadata (`/api/...`) is proxied straight to the Hub — small, latency-bound, and
any divergence from the real API breaks clients subtly. Only `/…/resolve/…`
file bytes are intercepted:

```
GET /org/model/resolve/main/model.safetensors
  ├─ cached?  → 200, stream from NVMe            (x-xhc-cache: HIT)
  └─ miss     → start/join single-flight ingest  (x-xhc-cache: MISS)
                 └─ per XHC_MISS_POLICY: stream | redirect | wait
```

Responses carry `x-xhc-cache`, `x-xhc-job`, and `x-xhc-miss-policy` so you can
see what happened from the client side.

**Single-flight coalescing** is the feature that matters most for a fleet that
rotates models in lockstep. Forty nodes asking for the same 140 GB blob within
seconds of each other produce exactly one upstream fetch.

**Client Xet negotiation is blocked.** Requests to
`/api/.../xet-{read,write}-token/...` return 404, so clients can't obtain a real
`casUrl` and pull bytes straight from HF, silently bypassing the cache. Clients
fall back to the resolve path automatically — a client that leaves Xet enabled
still works, it just gets served from cache. Disable with
`XHC_BLOCK_CLIENT_XET=0`.

### Mutable refs are revalidated

A cache hit is a disk read, which is the point — but it means a moved `main`
upstream would otherwise never be noticed, and the client would be told the old
commit *is* `main`.

Muninn revalidates the **ref**, not the file. A `ref → commit` mapping is
trusted for `XHC_REF_TTL` seconds (default 300); inside that window a hit costs
nothing extra, and a commit-pinned request costs nothing ever, since a sha
cannot move. When the mapping expires and upstream has moved, the request is
treated as a miss under the new commit.

Revalidation is single-flight per repo-and-ref: forty nodes rotating together
produce **one** upstream lookup, verified by counting.

It fails open. If upstream is unreachable, rate-limiting, or has deleted the
repo, we keep serving what we hold — required for orphan retention, where 404
is permanent and correct. Only a positive, different commit triggers a refetch.

Set `XHC_REF_TTL=0` to disable revalidation entirely; mutable refs then serve
whatever was first cached, which is what a pure archive wants.

### What this cache will fetch

Any host that can reach the port can otherwise cause an ingest of any repo —
a typo can pull a 500 GB dataset onto the array.

```bash
curl -X PUT localhost:8080/_cache/policy -H 'content-type: application/json' -d '{
  "mode": "allowlist",
  "allow": ["models/meta-llama/*", "datasets/my-org/*"],
  "deny":  ["models/*/*-gguf"],
  "max_file_bytes": 214748364800
}'
```

- Patterns are globs over `models/org/name`, `datasets/org/name`, `spaces/…`.
- **Deny always wins** over allow, so an explicit block cannot be undone by a
  broad allow someone adds later.
- Policy gates **ingest, not serving**. A repo already cached keeps serving even
  after a policy change, so tightening policy cannot break a rollout in flight.
  `"scope": "all"` enforces on cache hits too.
- `max_file_bytes` is checked against the upstream HEAD, so an oversized file is
  refused before any bytes move.
- Refusals are `403` with `x-xhc-policy: denied`. They deliberately do **not**
  borrow an HF `X-Error-Code` — this is a local rule, not the Hub's answer, and
  labelling it `GatedRepo` would send people hunting for a token that would not
  help.
- `/_cache/prewarm` honours policy unless called with `"force": true`; the
  management API is already authenticated.

Env (`XHC_INGEST_POLICY`, `XHC_ALLOW_REPOS`, `XHC_DENY_REPOS`) seeds the policy;
a `PUT` persists to `.xhc/policy.json` and wins from then on, same precedence as
pins.

### Conditional requests

A cache hit answers `304 Not Modified` when `If-None-Match` matches the ETag it
would return (`*` matches too). `If-Modified-Since` is deliberately **not**
implemented: blob mtimes come from our ingest, not from the Hub, so any answer
would be a guess.

### Missing files are answers, not failures

Clients probe for **optional** files on every model load — `processor_config.json`,
`chat_template.jinja`, preprocessor variants — and most repos have none of them.
The cache passes the Hub's own answer straight through: the real status code
*and* the `X-Error-Code` header.

That header is load-bearing. `huggingface_hub` reads it to decide which
exception to raise, and a bare 404 without it becomes a generic
`HfHubHTTPError` rather than the `EntryNotFoundError` that callers catch to mean
"optional file absent":

| upstream | cache returns | client raises |
|---|---|---|
| 404 `EntryNotFound` | 404 + `X-Error-Code` | `EntryNotFoundError` — handled instantly |
| 404 `RepoNotFound` / `RevisionNotFound` | same, passed through | the matching error |
| 403 `GatedRepo` | 403 + `X-Error-Code` | `GatedRepoError` — fix a token, don't retry |
| 5xx | passed through unchanged | retryable, as intended |
| unreachable (DNS/TLS/reset/timeout) | 502 | genuinely a bad gateway |

Reporting a missing file as 502 is not a cosmetic wrong code — it tells the
client the mirror is broken. Absent files are then also negative-cached for
`XHC_NEGATIVE_TTL` seconds (default 60), so a fleet rotating onto one model
pays one WAN round-trip for each absent file instead of one per node per load.
Measured: 94 ms cold, 1.2 ms from the negative cache.

### Range requests

Both single and multi-range `Range` headers are honoured, on cache hits **and**
on cache misses.

| request | response |
|---|---|
| `bytes=100-199` | `206`, single body |
| `bytes=0-99,500-599` | `206`, `multipart/byteranges` with exact `Content-Length` |
| `bytes=0-500,400-800` | `206`, **coalesced** to one part `0-800` |
| `bytes=0-99,9e9-9e9` | `206` for the satisfiable member; unsatisfiable ones dropped |
| every range past EOF | `416` with `Content-Range: bytes */<size>` |
| malformed, or more than `XHC_MAX_RANGES` parts | header ignored, `200` whole file |

Multi-range matters most for **datasets**: parquet readers (fsspec, DuckDB,
pyarrow) batch column-chunk reads into a single request. Model weights are
fetched whole, so this rarely fires for them.

**Ranges work on a cold cache too.** A range request that misses waits only
until the ingest has written past its start offset, then streams just that
span — verified serving a 100-byte range out of a 988 MB file in 6.4 s, from
one upstream fetch, instead of transferring 988 MB. That relies on ingest
writing sequentially, the same property `stream` depends on. A *multi*-range
miss instead waits for the ingest to finish and then serves from the completed
file, because seeking backwards into a partially-written file is not safe.

Overlapping ranges are coalesced before anything is read. That is the real
defence against multi-range amplification (CVE-2011-3192, "killapache"): a
thousand overlapping copies of the same span collapse to one, so the body can
never exceed the file size. `XHC_MAX_RANGES` only bounds per-part bookkeeping.

### Miss policies

| policy | concurrent cold clients | edge node needs Hub token? | notes |
|---|---|---|---|
| `stream` **(default)** | share **one** WAN fetch, all served at ingest speed | no | Tail-follows the partial file. Depends on sequential writes — see below. |
| `redirect` | each pulls from the WAN **independently** | yes | Coalesces the background ingest but not the clients. Use if you can't rely on sequential writes. |
| `wait` | share one WAN fetch, but each waits for it to finish first | no | Always correct. Client pays ingest latency *then* transfer latency. |

`redirect` was the original default; measurement changed the recommendation.
It only coalesces the *ingest* — the clients themselves still each hit the WAN,
which is the exact traffic multiplication the cache exists to prevent.

If you prewarm properly, misses are rare and this choice barely matters.

## Two request headers: prewarm, and local-only

Both are opt-in headers on the ordinary resolve path. **No management token, no
rewritten URL** — a caller that can already pull through the cache can use them.

Deliberately headers rather than query parameters: a query string changes the
URL, and the URL is the cache key the client and every proxy between you and the
cache agree on. These ask for different *handling* of the same resource.

### `X-Muninn-Prewarm: 1` — ingest it, do not send it to me

```bash
curl -sS -D- -o /dev/null -H 'X-Muninn-Prewarm: 1' \
  http://cache:8080/org/model/resolve/main/model-00001-of-00004.safetensors
```

| response | meaning |
|---|---|
| `202` + `x-xhc-cache: MISS-PREWARM` + `x-xhc-job` | ingest started (or joined an in-flight one); poll the job |
| `204` + `x-xhc-cache: HIT` | already cached, nothing to do |

The body is always empty. This is the only branch that ignores `XHC_MISS_POLICY`,
because the miss policy answers *"how do we serve this request"* and prewarm has
already said *"do not serve it to me"*.

It joins the existing single-flight, so a prewarm racing a real pull for the same
file starts one ingest, not two.

### `X-Muninn-Local-Only: 1` — answer from disk, or not at all

```bash
curl -sS -o /dev/null -w '%{http_code} %header{x-xhc-cache}\n' \
  -H 'X-Muninn-Local-Only: 1' http://cache:8080/org/model/resolve/main/config.json
```

| response | meaning |
|---|---|
| `200` + `x-xhc-cache: HIT-LOCAL` + `x-xhc-local-only: 1` | served from disk, **not revalidated** |
| `404` + `x-xhc-cache: MISS-LOCAL` | not on disk. **The Hub was never asked** |

**It never contacts upstream, on any path.** There are three upstream calls the
resolve path can make and all three are suppressed: ref revalidation
(`refs.is_stale`), the etag backfill when a cached blob has no symlink, and the
metadata fetch on a miss. A local-only check that still asks the Hub is not
local-only — it is a slower miss with a promise attached, and it fails when the
Hub is unreachable, which is exactly when you most want to know what is on disk.

**The trade is explicit: a certainly-local answer rather than a certainly-current
one.** A mutable ref may have moved upstream and this will not tell you. The
response says so — `HIT-LOCAL` rather than `HIT`, plus `x-xhc-local-only: 1` — so
an answer cannot be mistaken for a revalidated one later. If you need currency,
do not use this header.

`404` here means *not cached*, not *does not exist*. It is `cache-control:
no-store`, because the answer is true of this cache at this instant and of
nothing else.

### Together

`X-Muninn-Local-Only` + `X-Muninn-Prewarm` is a **`400`**. A prewarm needs
upstream metadata and local-only forbids it, so it is a contradiction rather than
a precedence question — and guessing which one wins would be a silent choice
about network access.

**They compose across requests, which is the point:** prewarm a set of files,
then poll each with local-only to find out what actually landed, without a
management token and without a single upstream request in the polling loop.

**Scope:** the Hugging Face resolve path. The `/v2/*` OCI surface does not
implement these; use `POST /_cache/docker/prewarm`.

## Verifying sequential writes

`stream` tail-follows a partial file, which is only correct if bytes land
front-to-back. `hf_xet` reconstructs a file from terms that can be written at
parallel file offsets, in which case a partial file is *not* a valid prefix and
streaming it would serve holes as real data.

**Measured on `hf_xet` via `huggingface_hub` 0.34.4**, against a 3.95 GB
Xet-backed file (`Qwen/Qwen2.5-7B-Instruct` shard 1):

| config | result |
|---|---|
| `HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY` unset | **PASS** — 17/17 samples valid prefixes, file grew from 0 monotonically, no preallocation |
| `HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY=1` | **PASS** — 9/9 samples, no measurable throughput penalty |

So on this version writes are already sequential. That is *not* a documented
guarantee, so the image sets the flag anyway and you should re-verify after any
`hf_xet` upgrade:

```bash
docker compose exec muninn python scripts/verify_sequential_writes.py \
    --repo-id Qwen/Qwen2.5-7B-Instruct \
    --filename model-00001-of-00004.safetensors \
    --cache-dir /tmp/verify
```

The script watches the `.incomplete` file and reads each byte **once, in order,
at the moment it first becomes available** — exactly what the streaming path
does — then compares that byte stream against the finished file. If a region
was a hole when read and filled in later, the script captured the hole, just as
a client would have. It reports `INCONCLUSIVE` rather than a false `PASS` if the
download finished too fast to sample (use a multi-GB file).

The service also degrades safely: if it can't find the partial file (the
`.incomplete` naming is a `huggingface_hub` implementation detail that shifts
between versions), it falls back to `wait` semantics rather than serving
garbage.

### Only `/healthz` is a health endpoint

Muninn is a transparent pull-through proxy: **everything that is not `/v2/*` or
`/_cache/*` is forwarded to Hugging Face.** So any other plausible-looking health
path returns whatever the Hub returns for it — and the Hub returns `200` for a
lot of things:

```
/healthz       200   {"ok":true,"free_bytes":...}   <- ours
/health        404   52 KB of Hub HTML
/healthcheck   200   74 KB of Hub HTML              <- reads as healthy, always
/status        200   73 KB of Hub HTML              <- reads as healthy, always
```

**A monitor pointed at `/healthcheck` or `/status` will report green forever**,
including through a total Muninn failure, because it is measuring the Hub's
availability rather than the cache's. This cannot be fixed by shadowing those
paths — the set of URLs the Hub answers is not enumerable, and shadowing them
would break the transparency the cache depends on.

**Check the body, not the status code.** `/healthz` returns JSON with `ok` and
`free_bytes`; a check that asserts `ok == true` cannot be satisfied by a proxied
HTML page. A status-code-only probe against this service is meaningless on any
path but `/healthz`.

## Metrics

`GET /metrics` exposes Prometheus text format. Unauthenticated by design: it
carries counts only — no repo names, no file paths — so it is safe to scrape
from the LAN.

```
muninn_requests_total{result="HIT"}            2
muninn_requests_total{result="MISS-STREAM"}    1
muninn_client_requests_total{client="gpu-01"}  2
muninn_upstream_requests_total{status="404"}   1
muninn_bytes_served_total                   2421
muninn_bytes_ingested_total                  807
```

Plus gauges for cache bytes, files, repos, capacity, free disk, active ingests,
orphan count and bytes, scan duration, and ref lookups.

`muninn_ingest_bytes_inflight` is the one worth knowing about: bytes fetched so
far by ingests that are still running. Without it a healthy prewarm and a
stalled one look identical on this endpoint — the count of active jobs stays 1
either way. It rises while an ingest is making progress and goes flat when it
is not.

The `client` label comes from an optional `X-Muninn-Client` header a node can
set. It is **attribution, not an audit trail** — it is self-reported, and a node
can claim to be anything. Label cardinality is capped (200, overflowing to
`__other__`) so a client sending a unique value per request cannot blow up a
scrape.

### Counters are volatile; cache gauges are not

`muninn_bytes_served_total`, `muninn_bytes_ingested_total` and
`muninn_requests_total` are process counters and **reset on every restart**.
`muninn_cache_bytes`, `muninn_cache_repos` and `muninn_cache_files` are read
from the filesystem and survive.

**"Zero since process start" and "never" are different claims.** A cache holding
terabytes will report zero bytes served immediately after a restart, and that is
not a fault. If you need to know whether it has ever worked, ask the durable
gauges — a non-zero `muninn_cache_bytes` cannot exist without ingestion.

### Every `bytes_served_total` figure from before 0.5.2 is inflated

Before 0.5.2 the served counter incremented on **`HEAD` as well as `GET`**, using
the response `content-length`. A `HEAD` carries the full size and transfers no
body — and `huggingface_hub` **issues a `HEAD` for every file in a repo before
downloading any of it**, so a snapshot booked the entire repo as *served* before
a byte left the disk.

Fixed in 0.5.2. `206` responses are still counted, deliberately: there
`content-length` is the range length, which is exactly what was sent.

**Do not compare a served figure across the 0.5.2 boundary.** Post-fix readings
read *lower* for identical traffic — that is the fix, not a regression.
`bytes_ingested_total` was never affected.

### Amplification is a derived ratio

Served-per-byte-ingested is the number this cache exists to justify, which is
exactly why it deserves the most scrutiny and not the least. **Publish the raw
counters beside it** so a reader can re-derive it when an input is retracted, and
**do not quote it off a small sample** — a handful of large cached objects
produces any ratio you like.

For a figure that is immune to this whole class, use `muninn_cache_bytes`: read
off the filesystem, durable across restarts, and not inflatable by request
accounting.

## Dataset metadata

`/api/datasets/{id}/parquet` and `/croissant` are cached for
`XHC_VIEWER_CACHE_TTL` seconds, and — like repo info — keep being served when
upstream 404s, so a deleted dataset stays describable. Those responses carry
`x-xhc-cache: VIEWER-SYNTHESIZED` and `x-xhc-synthesized: true`.

### Reaching datasets-server

`splits`, `rows` and `first-rows` live on `datasets-server.huggingface.co` — a
separate host. `huggingface_hub` never calls it, and there is **no `HF_ENDPOINT`
equivalent** for it, so no client can be redirected here by configuration.

Rather than MITM a public hostname with internal DNS and a private CA, Muninn
exposes it under its own prefix:

```bash
curl "http://nas.internal:8080/datasets-server/splits?dataset=org/ds"
```

Point tooling that accepts a base URL at that. Nothing is intercepted, so a node
that knows nothing about this cannot be broken by it — which is also why it does
not help the fleet's normal workload: `load_dataset` resolves files through
paths already cached, and the web viewer talks to the Hub directly.

Small stable endpoints (`splits`, `first-rows`, `info`, `size`, `is-valid`,
`parquet`) are cached and survive a deleted dataset. `rows` is proxied but
**never cached** — it is query-dependent and unbounded. Cache keys include the
sorted query string, since `dataset`/`config`/`split` arrive as parameters
there. Set `XHC_DATASETS_SERVER=` (empty) to disable the route entirely.

Two deliberate exclusions:

- **`/rows` is never cached.** It is query-dependent and unbounded; caching it
  badly means serving wrong rows.
- **`/splits`, `/rows` and `/first-rows` never reach Muninn at all.** They live
  on `datasets-server.huggingface.co`, a separate host clients contact directly.
  Nothing here can cache them without proxying that host too, which would be a
  separate feature.

`DELETE /_cache/viewer` drops the cached metadata; repo bytes are untouched.

## Docker and OCI pull-through

Muninn speaks a second protocol. `/v2/*` is a full OCI Distribution **pull** surface for
**any** upstream registry, addressed by path prefix:

```bash
docker pull muninn.host/ghcr.io/org/img:1.2.3
docker pull muninn.host/quay.io/prometheus/prometheus:v3.1.0
docker pull muninn.host/nginx                    # -> docker.io/library/nginx
```

Everything that is not `/v2/*` stays Hugging Face and is unchanged.

**Why a path prefix rather than a mirror.** Docker treats the first component of a reference
as a registry host when it contains a `.` or `:`, and dots are legal inside a repository
path — so `ghcr.io/org/img` arrives as an opaque repository name and Muninn routes on it.
That means **zero per-node configuration**: no `hosts.toml`, no `registry-mirrors` (which
only ever worked for Docker Hub), no daemon restart, and identical behaviour across docker,
podman, containerd, buildkit and Kubernetes. The cost is that image references have to be
rewritten, and anything missed silently bypasses the cache.

**What it gives you**

- Blobs are content-addressed, so the digest is **verified on ingest**. Bytes that do not
  hash to their digest are discarded, never cached — a corrupt layer served forever is far
  worse than a failed pull.
- Manifests are stored and served **byte-for-byte**. Any re-encoding would change the digest
  and break pull-by-digest and every signature check.
- Layers are shared across repositories on an upstream, so **layer dedup is free**.
- Single-flight: N nodes pulling the same cold image cost **one** upstream fetch per blob.
- Tags are revalidated like Hugging Face refs (`XHC_DOCKER_TAG_TTL`), and revalidation
  **fails open** — a tag deleted upstream keeps serving the digest you hold.

**Storage is a separate root** (`XHC_DOCKER_DIR`) with its own capacity budget, so image
churn can never evict models.

### Garbage collection is mark-and-sweep, not LRU

This is the one place the Docker side genuinely differs from the Hugging Face side. HF blobs
belong to exactly one snapshot tree, so LRU is safe. **Docker blobs are referenced by
manifests, and manifests by tags** — and an index points at per-platform manifests which
point at layers. Naive LRU evicts a layer a retained manifest still needs and produces an
image that fails at *pull* time with a baffling error, long after the eviction that caused it.

So Muninn walks tags and pins → manifests → config and layers, recursing through indexes, and
sweeps only what falls outside that set. Freeing space when everything is referenced drops a
**tag** and re-marks; eviction is top-down, because a blob is only safe once nothing points at
it. **Pinned tags and retained orphans are never candidates**, even if that means missing the
capacity target — running hot on disk is recoverable.

**Pinning an image pins its whole closure.** A pin that kept the manifest but let its layers
go would look intact until someone pulled it.

If the pin or orphan state cannot be read, **GC refuses rather than proceeding**. An absent
state file legitimately means "nothing is pinned"; an unreadable one means "unknown", and
collapsing those would silently disarm pin protection inside an unattended loop.

### Docker management endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/_cache/docker/prewarm` | pull an image and its closure ahead of a rollout; returns a job |
| `GET` | `/_cache/docker/prewarm/{id}` | poll it |
| `GET` | `/_cache/docker/images` | cached tags, with pin and orphan state |
| `GET`/`POST`/`DELETE` | `/_cache/docker/pins` | pin an image and its blob closure |
| `DELETE` | `/_cache/docker/images` | drop a tag; its blobs go on the next sweep |
| `POST` | `/_cache/docker/gc` | run mark-and-sweep now (`?dry_run=true` to see what would go) |

Prewarm is fire-and-forget, so nobody holds an HTTP connection open across a 30 GB pull.
**Pass a digest rather than a tag** for anything you intend to reproduce — a tag can move
mid-pull and assemble a tree from two commits.

### Docker configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `XHC_DOCKER_ENABLED` | `1` | serve `/v2/*` at all |
| `XHC_DOCKER_DIR` | `/docker` | storage root; use a separate volume |
| `XHC_DOCKER_MAX_SIZE` | unset | capacity budget for the image cache |
| `XHC_DOCKER_DEFAULT_UPSTREAM` | `docker.io` | used when the first path segment has no dot |
| `XHC_DOCKER_TAG_TTL` | `300` | seconds a tag→digest mapping is trusted; `0` never revalidates |
| `XHC_DOCKER_POLICY` | `open` | `open`, `allowlist` — parity with `XHC_INGEST_POLICY` |
| `XHC_ALLOW_REGISTRIES` / `XHC_DENY_REGISTRIES` | unset | host globs, **honoured in `open` mode too** |
| `XHC_ALLOW_IMAGES` / `XHC_DENY_IMAGES` | unset | globs over `<upstream>/<repo>` |
| `XHC_DOCKER_MAX_BLOB_BYTES` | unset | refuse an oversized layer before bytes move |
| `XHC_REGISTRY_AUTH_FILE` | unset | mounted `~/.docker/config.json` for upstream credentials |

> **Policy defaults to `open`**, at parity with the Hugging Face side. Path-prefix routing
> means anyone who can reach the port can pull from **any** registry onto your array. The
> server warns at boot in that state. Setting `XHC_ALLOW_REGISTRIES` is the cheapest useful
> hardening and does **not** require flipping the whole policy to `allowlist`.
>
> This is a separate concern from the trust boundary below. That one is about **who may read
> what the cache holds** and is deliberate; this one is about **which upstreams anyone may
> make the cache fetch from**, which is worth narrowing even on a network you trust.

**Not implemented:** push (a cache that accepts pushes is a registry, with GC, quota and
durability obligations), and client-facing auth — `XHC_DOCKER_AUTH` is parsed and validated
but **never read by any code path**. Setting it to `basic` does nothing.

### Private registries: the cache authenticates as itself

Mount the host's Docker credentials and point `XHC_REGISTRY_AUTH_FILE` at them. Whatever
the cache host is logged into, the cache can pull:

```bash
docker login registry.example.com          # on the Muninn host
```

```yaml
services:
  muninn:
    environment:
      XHC_REGISTRY_AUTH_FILE: /auth/config.json
    volumes:
      - ~/.docker/config.json:/auth/config.json:ro
```

It reads the standard `~/.docker/config.json` that `docker login` already produces, so
there is no bespoke credential format, and mounting it read-only as a secret keeps the
values out of `docker inspect`. Both auth schemes are supported: a Bearer challenge takes
the token dance, a Basic challenge is answered with the mounted credentials.

Credentials are sent **only in response to a challenge**, never preemptively — configuring
an auth file cannot leak credentials to a registry that never asked for them. If an upstream
challenges and no credentials are configured for it, its own `401` is passed back rather
than an invented answer.

> **This worked for the first time in 0.6.2.** Before that, credentials were loaded, logged
> at startup and never attached to a request; a registry using plain Basic auth could not be
> reached at all. If you are on an earlier version, the feature is documented but absent.

### The trust boundary is the network, deliberately

**Anything that can reach this cache can pull anything the cache holds.** That is the design,
not an oversight, and it follows from what a pull-through cache is:

- A cached hit consults **no credentials at all**. It checks the fleet-wide allow/deny policy
  and then serves off disk. The store is keyed by upstream, repo and digest — there is no
  principal anywhere in it.
- So per-client authorization is not something that can be bolted on. It would be enforced on
  the **miss** and silently absent on every **hit** after it, and the first client to pull a
  private image would make it readable by everyone. A control that looks present and is not
  is worse than none.

For that reason Muninn does **not** forward a client's `Authorization` header upstream, and
will not. It authenticates as itself, with credentials you mount.

Run it where you would run an NFS server: on a network whose reachability you already control.
If you need a door on it, put one in front — a reverse proxy doing basic auth works with
`docker login` today, at the cost of carving out `/healthz` and `/metrics`, which are
unauthenticated by design.

## Management API

All under `/_cache`. Set `XHC_MANAGE_TOKEN` to require `Authorization: Bearer …`.

| method | path | purpose |
|---|---|---|
| `GET` | `/_cache/status` | disk, capacity, watermarks, active jobs, scan cost, **effective Xet env** |
| `GET` | `/_cache/repos?refresh=true` | cached repos with size, file count, revisions, pin state |
| `POST` | `/_cache/prewarm` | ingest a repo ahead of a rollout |
| `GET` | `/_cache/jobs`, `/_cache/jobs/{id}` | ingest progress, elapsed, throughput |
| `GET`/`POST`/`DELETE` | `/_cache/pins` | pin management |
| `GET`/`DELETE` | `/_cache/orphans` | repos deleted upstream and retained |
| `POST` | `/_cache/orphans/check` | run an upstream liveness sweep now |
| `GET`/`PUT` | `/_cache/policy` | inspect or set what may be ingested |
| `DELETE` | `/_cache/viewer` | drop cached dataset metadata |
| `POST` | `/_cache/evict` | force an LRU sweep |
| `DELETE` | `/_cache/repos` | drop a repo, or one `revision` of it (409 if pinned) |
| `GET` | `/healthz` | container healthcheck |

`/_cache/status` echoes the Xet variables the process actually sees. A silently
unset or wrong value there is the single most likely cause of a slow WAN ingest,
so check it first.

### Prewarming is the primary path

If you know your model set in advance — and with centralised model management
you do — edge nodes should only ever see cache hits.

```bash
curl -X POST localhost:8080/_cache/prewarm -H 'content-type: application/json' -d '{
  "repo_id": "meta-llama/Llama-3.1-70B-Instruct",
  "allow_patterns": ["*.safetensors", "*.json", "tokenizer*"],
  "pin": true
}'
```

`allow_patterns` matters on the Hub: many repos ship both `.safetensors` and
`.bin` copies of the same weights, and pulling both doubles your footprint for
nothing.

### Retention: models deleted upstream

Eviction is LRU, and LRU is the wrong instinct for a repo that no longer exists
on the Hub. Evicting a *live* repo costs you a re-download. Evicting one that
has been **deleted upstream destroys the only remaining copy** — and if you use
this cache as a reproducibility reference, that ends the experiment.

So Muninn checks cached repos against upstream every `XHC_ORPHAN_CHECK_INTERVAL`
(6 h) and marks the ones that have gone. Under the default
`XHC_ORPHAN_POLICY=retain` those are exempt from eviction, exactly like a pin,
but applied automatically — you don't have to predict which models will vanish.

```bash
curl localhost:8080/_cache/orphans | jq          # what is being retained, and why
curl -X POST localhost:8080/_cache/orphans/check # sweep now
curl -X DELETE localhost:8080/_cache/orphans \
  -H 'content-type: application/json' -d '{"repo_id":"org/model"}'   # release one
```

Classification is deliberately biased toward keeping data:

| upstream says | marked | rationale |
|---|---|---|
| `200` | live — mark cleared if it had one | it can be re-fetched |
| `404` | **orphan** (`deleted`) | gone; this is the only copy |
| `401` / `403` | **orphan** (`gated_or_unauthorized`) | you can't re-fetch it either |
| `429`, `5xx`, timeout, DNS failure | *unchanged* | a transient fault must never make an archive evictable |

That last row is the important one. Only an unambiguous `200` un-marks a repo,
so a Hub outage or a rate-limit burst cannot quietly convert your archive back
into eviction fodder.

#### Orphans stay fully usable

Both `/api/{type}s/{repo}` and `/api/{type}s/{repo}/tree/{rev}` are rebuilt from
the cached snapshot, so `snapshot_download` **and** `list_repo_files` work on a
repo that no longer exists upstream. The tree's `oid` is the same value the
resolve path serves as the ETag — they agree by construction, since both read
the blob symlink.

Retaining the bytes is only half the job. `snapshot_download` (and `hf download
<repo>`) enumerates a repo through `/api/{type}s/{repo}` before fetching
anything, and that call is proxied — so a deleted repo used to be *half*
usable: `hf_hub_download` worked per file, but you could not list it.

When upstream 404s a repo we still hold, Muninn now rebuilds the listing from
the cached snapshot — `sha` from `refs/`, `siblings` from the snapshot tree,
and only files that actually resolve, so it never advertises a blob it cannot
serve. Verified: a cold client runs a full `snapshot_download` of a repo whose
upstream returns 404 for everything.

The answer is tagged both ways, so an archived listing is never mistaken for a
live one:

- response header `x-xhc-synthesized: true` (plus `x-xhc-cache: SYNTHESIZED`)
- body fields `xhcSynthesized` and `xhcSynthesizedReason`

The body tag is safe: `huggingface_hub`'s `ModelInfo`/`DatasetInfo` accept
unknown fields, which is asserted in the test suite so a future release cannot
silently break the contract.

Deliberate limits — this synthesizes a **file listing**, not the Hub API:

- only `/api/{models,datasets,spaces}/{repo}[/revision/{rev}]`; sub-resources
  like `/tree/` and `/paths-info` still go upstream
- only on a real upstream **404**. An unreachable Hub returns 502, so an outage
  can never quietly start serving stale listings
- only when we hold the snapshot; an uncached repo still 404s honestly
- live repos always get the Hub's real answer, untouched

Set `XHC_SYNTHESIZE_REPO_INFO=0` to turn it off.

#### Releasing a retained orphan

Retention makes orphans unevictable, so freeing that space is a deliberate act:

```bash
# whole repo, and the orphan mark is cleared with it
curl -X DELETE localhost:8080/_cache/repos \
  -H 'content-type: application/json' -d '{"repo_id":"org/model"}'

# or a single revision, leaving the rest of the repo intact
curl -X DELETE localhost:8080/_cache/repos \
  -H 'content-type: application/json' \
  -d '{"repo_id":"org/model","revision":"<commit-sha>"}'
```

Deleting always clears the orphan mark once nothing of the repo remains, so
`retained_bytes` cannot keep claiming space that is no longer held.

**Pins remain absolute** — there is deliberately no force flag. A pinned repo
returns 409 and you must `DELETE /_cache/pins` first. That unpin is the
acceptance step that stops a pinned model being destroyed by one mistyped call.

**The cost is real:** retained orphans are unevictable, so they permanently
reduce usable capacity. Eviction logs a warning and `/_cache/evict` returns
`reached_goal: false` with `protected_bytes` when protection wins over the
target — watch that rather than discovering a full array. Set
`XHC_ORPHAN_POLICY=evict` if you'd rather have the space.

### Pinning vs. eviction

Eviction is LRU over revisions, triggered on a timer and by watermark. **Pinning
is repo-level and absolute** — a pinned repo is never an eviction candidate,
even if that means the cache can't reach its low-water mark. That's the right
failure mode for a fleet rollout: better to run hot on disk than to evict the
model every node is about to request. Pin the current working set; let
experiments age out.

## Configuration

| variable | default | meaning |
|---|---|---|
| `HF_TOKEN` | — | org token. Edge nodes then need no Hub credentials, and gated licences are accepted once, centrally. |
| `HF_HUB_CACHE` | `/cache` | the array. Standard `huggingface_hub` layout. |
| `XHC_CACHE_MAX_SIZE` | filesystem size | eviction target, e.g. `70T`. Binary units. |
| `XHC_HIGH_WATER` / `XHC_LOW_WATER` | `0.90` / `0.75` | evict when above high, down to low |
| `XHC_EVICT_INTERVAL` | `900` | background sweep, seconds |
| `XHC_MISS_POLICY` | `stream` | `stream` \| `redirect` \| `wait` |
| `XHC_BLOCK_CLIENT_XET` | `1` | 404 the Xet token endpoints so clients can't bypass the cache |
| `XHC_INGEST_CONCURRENCY` | `4` | simultaneous WAN ingests |
| `XHC_NEGATIVE_TTL` | `60` | seconds to remember an upstream 404; `0` disables |
| `XHC_ORPHAN_POLICY` | `retain` | `retain` \| `evict` — what to do with repos deleted upstream |
| `XHC_ORPHAN_CHECK_INTERVAL` | `21600` | seconds between upstream liveness sweeps; `0` disables |
| `XHC_SYNTHESIZE_REPO_INFO` | `1` | rebuild repo/tree listings from cache when upstream 404s |
| `XHC_REF_TTL` | `300` | seconds a ref→commit mapping is trusted; `0` never revalidates |
| `XHC_INGEST_POLICY` | `open` | `open` \| `allowlist` |
| `XHC_ALLOW_REPOS` / `XHC_DENY_REPOS` | unset | comma-separated globs; deny wins |
| `XHC_POLICY_SCOPE` | `ingest` | `ingest` \| `all` — whether policy also gates cache hits |
| `XHC_MAX_FILE_BYTES` | unset | refuse to ingest a file larger than this |
| `XHC_VIEWER_ENDPOINTS` | `parquet,croissant` | dataset metadata endpoints to cache |
| `XHC_VIEWER_CACHE_TTL` | `3600` | seconds; `0` disables freshness but keeps entries for deleted datasets |
| `XHC_DATASETS_SERVER` | `https://datasets-server.huggingface.co` | upstream for the `/datasets-server/*` route; empty disables it |
| `XHC_DATASETS_SERVER_ENDPOINTS` | `splits,first-rows,info,size,is-valid,parquet` | which of those to cache (never `rows`) |
| `XHC_MANAGE_TOKEN` | unset | bearer token for `/_cache/*` |
| `XHC_STREAM_CHUNK` | `4194304` | LAN read/serve chunk size |
| `XHC_MAX_RANGES` | `64` | max parts in a multi-range request before the header is ignored |
| `HF_XET_NUM_CONCURRENT_RANGE_GETS` | `32` (image) | **main WAN throughput dial** (`hf_xet` default is 16) |
| `HF_XET_HIGH_PERFORMANCE` | unset | bigger buffers/concurrency; wants ≥64 GB RAM |
| `HF_XET_CHUNK_CACHE_SIZE_BYTES` | `100G` (compose) | `hf_xet` scratch; the one place chunk-level dedup can pay off |
| `HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY` | `1` (image) | required by the default `stream` policy |

Invalid config fails at import rather than at first request — a bad
`XHC_MISS_POLICY` will refuse to start the container.

## On-disk layout

The cache is a stock `huggingface_hub` directory
(`models--org--name/{blobs,snapshots,refs}`). Deliberately: ingest is just
`hf_hub_download`, so atomic writes, symlinking and blob-level dedup across
revisions come for free, and the array stays readable by any standard HF client.
If this service ever gets in your way you can mount the volume read-only
elsewhere and point `HF_HUB_CACHE` straight at it. Our own state lives in
`.xhc/` (currently just `pins.json`).

That file-level dedup is also the dedup that actually pays here. Fine-tunes
rewrite essentially every weight tensor, so Xet's chunk-level dedup across them
recovers little beyond tokenizers and configs; identical files across revisions
already cost one copy.

## Scaling

`scan_cache_dir()` stats every blob, so its cost tracks **file count, not
bytes**. Measured with `scripts/bench_scan.py` (sparse files, real layout):

| shape | files | logical size | scan |
|---|---|---|---|
| 500 repos × 30 large shards | 15,000 | 80.5 TB | **0.55 s** |
| 2,000 repos × 2 revs × 50 shards | 200,000 | 83.9 TB | **12.1 s** |

Same capacity, 22× the scan cost — file count is what bites, so a cache full of
many-shard datasets is the case to watch. Two consequences, both handled:

- The view cache TTL is **adaptive**: it holds a scan result for 10× the time
  the scan took (clamped to 30–600 s), so a slow scan can't eat the wall clock.
  At 200 k files that's a 120 s TTL; a cached `/_cache/status` returns in 0.5 ms.
- The eviction sweep deliberately uses the **cached** view for its
  trigger check. `evict()` re-scans authoritatively before deleting anything, so
  forcing a fresh scan there would pay for two full scans (24 s) per sweep to
  answer a question a stale view answers fine.

**The hot path never scans.** A cache hit is a direct path resolution, so
serving is unaffected by tree size — verified at 200 k files / 84 TB.

Watch `scan_duration_s` on `/_cache/status`. If it climbs past ~30 s, the fix is
an incremental index rather than a rescan.

## Verified behaviour

Exercised end-to-end against the live Hub, on real Xet-backed repos:

**Correctness**

- `hf download` through `HF_ENDPOINT` cold and warm; served bytes SHA-256
  identical to a direct upstream download
- 3 concurrent clients streaming a **3.95 GB** file off a cold cache → all three
  SHA-256 match, from a **single** ingest
- 12 concurrent cold requests → 1 ingest job (single-flight)
- `Range` → `206` with correct `content-range`
- Xet token endpoints 404'd; clients with Xet still enabled fall back and succeed
- prewarm, pins, pin-protected eviction (409), delete, LRU eviction over watermark
- `redirect` policy: 302 + background ingest → next request is a HIT

**Performance** (loopback / page cache, so these bound the software, not your hardware)

- warm hit, single client: **9.7 GB/s**
- warm hit, 6 concurrent clients: **13.3 GB/s** aggregate
- cold `stream`, 3 concurrent clients: **157 MB/s each** off one WAN ingest
  (WAN-bound, not server-bound)

**Retention and archive behaviour** (v0.2.0, against a stubbed upstream)

- one repo deleted upstream, one live, eviction forced under real pressure
  (20 MB cap vs 40 MB cached) → the **live** repo was evicted and the **deleted**
  one kept
- unreachable upstream → `inconclusive=1`, orphan mark preserved (the fail-safe:
  an outage must never make an archive evictable)
- repo restored upstream → mark cleared automatically
- `XHC_ORPHAN_POLICY=evict` → the same orphan became evictable again
- live upstream → zero repos marked, so no false positives
- force-evict: pinned → `409`; after unpin → deleted, orphan mark cleared,
  `retained_bytes` 12,512,611 → 0

**Repo-info synthesis**

- cold client, upstream 404ing everything → full `snapshot_download`, 10 files
- live repos pass through untouched: no `x-xhc-synthesized` header, real Hub
  fields (`lastModified`, `downloads`) intact
- an uncached repo still 404s honestly; `/tree/` and other sub-resources are not
  synthesized

**Range handling**

- multi-range `206` reassembled with the stdlib multipart parser: 3 parts, each
  `Content-Range` correct, every byte matching the source
- declared `Content-Length` equal to the bytes actually produced (691 = 691)
- overlapping request coalesced to a single part and byte-identical to the span
- all-unsatisfiable → `416` with `Content-Range: bytes */<size>`
- **range off a cold cache**: 100 bytes out of a 988 MB file, bytes matching the
  Hub, in 6.4 s from one ingest — instead of transferring the whole file

**Error semantics**

- upstream 404 → `404` + `X-Error-Code`, client raises `EntryNotFoundError`.
  Against the pre-fix code the same request produced `LocalEntryNotFoundError`
  — a *connectivity* error — for a file that simply does not exist
- 94 ms cold, 1.2 ms from the negative cache

The `0.2.0` image was pulled back from GHCR and re-verified end to end, so these
hold for the published artifact and not only the working tree.

Three real bugs were caught only by end-to-end testing, not by unit checks:

1. Cache hits returned no `ETag`, which `huggingface_hub` refuses to download
   without. Fixed by recovering it from the blob symlink, since
   `snapshots/<commit>/<file>` links to `blobs/<etag>`.
2. `Settings.from_env()` carried a hardcoded default that shadowed the dataclass
   field, so changing the declared default silently did nothing.
3. Forwarding the upstream `content-encoding: gzip` header alongside
   `httpx`-decoded bytes made clients try to gunzip plain JSON. `curl` hid this
   completely — it sends no `Accept-Encoding`, so upstream never compressed —
   and only a real `requests`-based client exposed it. A reminder that testing
   with `curl` alone is not testing the client contract.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q          # offline unit tests
ruff check app scripts tests
uvicorn app.main:app --reload --port 8080
```

The unit tests are deliberately offline. The behaviour that actually matters —
ingest, coalescing, streaming integrity, eviction — needs the live Hub and is
not run in CI; see **Verified behaviour** for what was exercised by hand and
how. `scripts/bench_scan.py` and `scripts/verify_sequential_writes.py` are the
two harnesses worth re-running when dependencies change.

## Contributing

Issues and PRs welcome. Two things make a change much easier to accept:

- If you touch the ingest, streaming, or eviction paths, say how you exercised
  it against a real repo — the offline tests will not catch a regression there.
- If you change a default, grep for it. Defaults are asserted in `config.py`,
  `.env.example`, `docker-compose.yml`, and the README table, and they have
  drifted apart before.

## Brand assets

`brand/` holds the derived assets; `images/` holds the original generations they
came from.

| file | use |
|---|---|
| `brand/muninn-banner.png` | 1280×640 wordmark lockup — README header and social card |
| `brand/muninn-banner-plain.png` | same, mark only, for contexts that supply their own title |
| `brand/muninn-icon.png` | 1024² transparent icon master |
| `brand/muninn-icon-{16..512}.png` | pre-sized icons |
| `brand/favicon.ico` | multi-resolution favicon (16/32/48/64) |

Palette: ink `#1C222B`, gold `#C7A764`, paper `#E1DED1`.

The icon was keyed off a solid white background with a soft alpha ramp rather
than a hard threshold, so the dry-brush edges survive; partial-alpha pixels
average rgb(112,112,112), so there is no white fringe on dark backgrounds.

## License

MIT — see [LICENSE](LICENSE).

## Limitations

- **Read path only.** Uploads pass through to the Hub unmodified; nothing is
  written back through the cache.
- **Single node.** No cache sharing or coordination between multiple instances.
- **Eviction granularity is a whole revision**, not individual files. The LRU
  sweep picks whole revisions; manual `DELETE /_cache/repos` can target one
  revision, but neither can drop a single file.
- **`stream` depends on undocumented `hf_xet` write ordering.** Verified on
  0.34.4; re-run the verification script after upgrading, or use `redirect`.
- Not exercised: sustained multi-day load, and a real 100 GbE fabric (all
  throughput numbers above are loopback).

### Two client-side limits that will bite a correct deployment

Neither is a Muninn defect and neither can be fixed on the serving side. Both
were found in production, and both look like a cache fault when they happen.

- **A single file over ~46.6 GiB cannot be fetched over plain HTTP.**
  `huggingface_hub` raises before it issues any request:

  ```python
  # file_download.py
  elif expected_size and expected_size > constants.MAX_HTTP_DOWNLOAD_SIZE:
      raise ValueError("The file is too large to be downloaded using the regular
                        download method. Install `hf_xet` ...")
  ```

  `MAX_HTTP_DOWNLOAD_SIZE` is a bare module literal, `50 * 1000 * 1000 * 1000`
  — **decimal, so the real ceiling is 46.57 GiB**, not the 50 the name implies.
  There is no environment variable for it.

  Muninn reports the true size on `HEAD` and always will: understating it would
  make `huggingface_hub` reject or silently truncate the result, and a cache
  whose value is that the bytes are the same bytes cannot make that trade.
  Muninn serves arbitrary byte ranges, but that does not help — **the refusal
  happens from metadata alone, before any `GET` is issued.**

  Workaround: fetch that shard outside `snapshot_download` (an ordinary ranged
  `GET` against Muninn, into the same cache layout) and let
  `ignore_patterns` take the rest. Do **not** patch the constant: it guards
  integrity on a path that genuinely cannot be trusted at that size.

- **`HF_HUB_DOWNLOAD_TIMEOUT` defaults to 10 seconds, which is hostile to a
  pull-through cache.** On a cold miss the first byte cannot arrive until the
  upstream fetch begins, so **the first node to want a model — the one that is
  supposed to pay the ingest cost — is the one that times out**, while every
  later node is served from disk. Set it to `600` on edge nodes. A mid-transfer
  drop on a large blob is recoverable: `huggingface_hub` resumes.
