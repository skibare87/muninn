<p align="center">
  <img src="brand/muninn-banner.png" alt="Muninn — pull-through cache for Hugging Face and container images" width="820">
</p>

<p align="center">
  <a href="https://github.com/skibare87/muninn/actions/workflows/ci.yml"><img src="https://github.com/skibare87/muninn/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="license: MIT"></a>
  <a href="https://github.com/skibare87/muninn/pkgs/container/muninn"><img src="https://img.shields.io/badge/ghcr.io-muninn-2496ED?logo=docker&logoColor=white" alt="ghcr.io/skibare87/muninn"></a>
</p>

A pull-through cache for **Hugging Face** models and datasets and for **OCI container
images**: fetch once over the WAN, then serve every host on your network from local
disk. It runs as one container on a single box, a NAS or a Kubernetes cluster, with
optional per-key access control, workload-identity (OIDC/JWT) auth, and an
object-store second tier (S3, GCS, R2) to refill a lost disk.

*In the Norse telling, Odin's raven Muninn — "memory" — flies out each day and
returns with what it found. Same job here: fetch it once, remember it, and let
everyone else read from memory.*

**The one idea:** the WAN leg and the LAN leg want different protocols, so don't
serve both with one reverse proxy.

| leg | protocol | why |
|---|---|---|
| NAS ← Hugging Face | native **Xet**, parallel range GETs | 16–64 concurrent streams against the CDN. This is where Xet's speed actually comes from. |
| edge node ← NAS | plain HTTP, whole file | no chunk reassembly, no second chunk cache on the node. Just bytes off local disk. |

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
NAS does not need a toolchain. `<version>` below is a placeholder, not a
typo: take the newest `X.Y.Z` from [CHANGELOG.md](CHANGELOG.md) or the
[tags](https://github.com/skibare87/muninn/tags). This README deliberately names
no current release, because a number written here goes stale with the next one
and nothing would tell you.

```bash
docker pull ghcr.io/skibare87/muninn:<version>

docker run -d --name muninn -p 8080:8080 \
  -v /mnt/nvme/hf-cache:/cache \
  -v /var/lib/muninn/xet:/xet \
  -e HF_TOKEN=hf_xxx \
  -e XHC_CACHE_MAX_SIZE=70T \
  ghcr.io/skibare87/muninn:<version>
```

Or from source, which is also how you get the compose file's full env set:

```bash
cp .env.example .env      # set HF_TOKEN, XHC_CACHE_PATH and XHC_MANAGE_TOKEN
docker compose up -d --build
curl -s -H "Authorization: Bearer $XHC_MANAGE_TOKEN" localhost:8080/_cache/status | jq
```

To run the published image under compose instead of building, replace the
`build: .` line in `docker-compose.yml` with
`image: ghcr.io/skibare87/muninn:<version>`.

Release notes for every version are in [CHANGELOG.md](CHANGELOG.md) and on the
[Releases page](https://github.com/skibare87/muninn/releases). Both are rendered
from the annotated git tag, which is written at release time and is the source of
truth — the changelog is regenerated, never hand-edited.

**Published tags** (multi-arch, `linux/amd64` + `linux/arm64`), built by
GitHub Actions on every version tag:

| tag | meaning |
|---|---|
| `X.Y.Z` | immutable — **pin this on a fleet** |
| `X.Y` | latest patch in that minor line; **moves** |
| `latest` | most recent tagged release; **moves** |
| `edge` | tracks `main`; expect breakage |

**Only the full `X.Y.Z` tag is immutable.** `X.Y` is not — publishing a new
patch re-points it, as `v0.5.1` did to `0.5` and `v0.5.2` did again an hour
later. `latest`, `X.Y` and `edge` all give every node whatever was pushed last,
with nothing to roll back to when a push goes wrong.

For a deployment you genuinely cannot have move under you, pull the `X.Y.Z` you
chose once, then pin the **manifest digest of the image you actually
deployed**:

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
  ├─ cached?  → 200, stream from local disk      (x-xhc-cache: HIT)
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

### Muninn's own paths never reach the Hub

Everything not claimed by another route goes to the Hub, which is how HF API
paths Muninn does not model keep working. The paths Muninn itself owns are the
exception, **whether or not the feature behind them is switched on**:

| path | owned by | when switched off |
|---|---|---|
| `/v2`, `/v2/*` | OCI registry | `XHC_DOCKER_ENABLED=0` |
| `/_cache/docker/*` | docker management API | `XHC_DOCKER_ENABLED=0` |
| `/_cache/*` | management API | `XHC_MANAGE_TOKEN` unset or blank |
| `/_auth/*`, `/_console/*` | browser login and key management | `XHC_OIDC_ISSUER` unset |
| `/datasets-server/*` | datasets-server proxy | `XHC_DATASETS_SERVER=` (empty) |
| `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` | API documentation | `XHC_DOCS=0` |
| `/healthz`, `/metrics` | health and Prometheus | always on |

A request for one of these that no enabled route answers gets a local `404` with
a one-line plain-text body, and nothing is forwarded. A switched-off surface
names the setting that enables it (`the OCI registry surface is disabled
(XHC_DOCKER_ENABLED=0)`); an enabled one with no such route or method says
`no such Muninn endpoint: POST /healthz`. Without this, a cache with docker off
answered `/v2/` with the Hub's `401`, its HTML and its headers.

The first five rows are whole subtrees. The last two are exact paths only (a
trailing slash included), so `/docs/…` deeper than those still goes to the Hub.
The check matches the path after `..` segments are resolved, because the
upstream client resolves them too.

It runs **after** the web root, so a static page you put at a switched-off path
(say `/docs/index.html`) is still served, and **before** the `XHC_HF_AUTH=key`
credential gate, which protects the Hub proxy and has nothing to protect here.
A docker client probing `/v2/` on a cache with docker off is told so, not sent a
Basic challenge for a registry that does not exist. The docker CLI shows only
`not found` for a 404 and discards the body; `curl` shows the reason.

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
curl -X PUT localhost:8080/_cache/policy -H "Authorization: Bearer $XHC_MANAGE_TOKEN" \
  -H 'content-type: application/json' -d '{
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

### What is verified on ingest, and what is not

Both protocols are content-addressed. Until 0.9.3 only one of them **checked**.

An OCI blob is hashed as it is ingested and refused if it does not match its
digest. A Hugging Face file was written under whatever ETag the Hub declared,
and nothing recomputed it — the blob's filename *is* the upstream ETag. Two
protocols, two different guarantees, and nothing said so.

They now refuse the same way. Each ingested HF file is hashed and compared
against its ETag; a mismatch deletes the blob and fails the ingest rather than
caching it. Set `XHC_HF_VERIFY=0` to turn this off.

**Both kinds of Hub ETag are checked.** The Hub returns a sha256 for LFS files —
every weight file — and the git blob id for the rest (configs, tokenizers,
small text), which is `sha1(b"blob <size>\0" + content)`: it does cover the
bytes, and this was measured to match the Hub's ETag on real repos before
relying on it. Each is checked in the same single pass over the file. **An ETag
of neither shape is reported as `UNVERIFIABLE`, never as verified.**
`muninn_ingest_verify_total{result="..."}` carries all three outcomes and each
is seeded at zero, so a zero means zero and a missing series means the process
was down.

Two limits, stated here rather than left to be discovered:

- **Under `stream`, the first caller may already have the bad bytes.** They are
  served as they arrive, so verification can stop a bad blob being *kept* but
  cannot retract what was already sent. Use `wait` if that matters more than
  first-byte latency.
- **The Xet transport IS covered — an earlier version of this section said it
  was not, and that was wrong.** The check runs on the file after the download
  returns, so it hashes whatever landed regardless of how it arrived. Measured
  against the real Hub: the download takes the Xet path, the blob's filename is
  the sha256 of its bytes, and verification passes. `tests/test_xet_path_is_verified.py`
  pins it, with a bit-flip case as the negative control.
  What is *not* known is whether `hf_xet` independently detects a corrupt chunk
  during reconstruction. That is defence in depth, not coverage: a corrupt
  reconstruction fails the post-ingest hash either way.
- **Verification covers INGEST, never the existing cache.** It runs only on bytes
  fetched on that run — a cache hit resolves locally and never reaches the ingest
  path, and the snapshot check skips any blob not written that run. So a flat
  `MISMATCH` counter says new ingests are healthy and says **nothing** about
  blobs already on disk, including every blob ingested before this feature
  existed. Answering that would need a full re-read of the cache, which this does
  not do.
- **Snapshot ingest is verified too**, over the landed tree rather than per file,
  because `snapshot_download` offers no per-file hook. Blobs are deduplicated by
  inode, so content shared between files is hashed once, and blobs that were
  *not* fetched on this run are skipped — a repeat prewarm does not re-hash the
  half it already had. That makes it a check on ingest and not a scrub: on-disk
  rot in a blob nobody re-fetched is a different problem and is not covered.
  The job's `verify` field and the log line say which is which:
  `new_verified` / `new_unverifiable` / `mismatched` for files fetched this run,
  `already_present_not_reverified` for the rest. A re-prewarm of a complete repo
  reads "0 new files verified; N already present", not "0 verified".
- **A job is `done` only after verification passes.** While the hash runs the
  job says `verifying`, with `finished_at` still null; `done` and `finished_at`
  are set together, and a mismatch ends in `error`, never `done`. (An earlier
  version marked a snapshot `done` before verifying it, so a large file could
  sit at `done` for minutes while still being checked.) Gate on `done`, not on
  the bytes appearing.

Why the default is on: sha256 measured at **1692 MiB/s** on this host against an
observed ingest rate of **192 MB/s** — about **9.2× faster than bytes arrive**,
so hashing is not the bottleneck. Both raw figures are given because the ratio
is a derived number and inherits their units: an earlier version of this line
said 8.8×, which divided MiB/s by MB/s.
Verify it on your own hardware before assuming it holds on yours.

### Read-only toward the Hub

Everything the Hugging Face surface forwards goes upstream with **the cache's own Hub
token**. So Muninn forwards only `GET` and `HEAD`, plus the read-only `POST` endpoints
downloads use; every other method is answered locally with **405** and never reaches the
Hub. This holds in every mode — with `XHC_HF_AUTH=none`, with `XHC_HF_RULES=off`, and for a
`*` key — because it is not authorisation: it is what the cache's credential may be used for.

| forwarded `POST` | used by |
|---|---|
| `/api/{models,datasets,spaces}/<repo>/paths-info/<rev>` | `HfApi.get_paths_info`, which `HfFileSystem` uses to stat files |

That is the only read among the `POST`s `huggingface_hub` 0.34.4 makes. Everything else is a
write and is refused: commits and preupload, creating, moving or deleting repos, branches,
tags, settings, LFS uploads, discussions, Space controls, collections. Pushing to the Hub goes
direct to the Hub, with your own token.

### One hostname as a homepage and a cache

`XHC_WEB_ROOT=/srv/www` serves static files at `/`. Unset by default, so nothing
changes for an existing deployment.

**Why this is in Muninn rather than in a reverse proxy.** The OCI surface is
bounded under `/v2` by spec, so a proxy *can* split docker traffic from a
homepage. It cannot split the Hugging Face surface: HF clients construct
arbitrary top-level paths like `/owner/repo/resolve/main/config.json`, so there is
no prefix to match on. Muninn already knows which paths are HF paths, so the
discriminator is a **precedence rule** rather than a pattern:

> **If a file exists under the web root, serve it. Otherwise fall through to
> Hugging Face.**

**That makes the web root's contents a claim on those paths.** A directory named
`models/` or `datasets/` in there would silently shadow real HF traffic, and the
symptom would be *"the cache stopped working"* rather than *"a file was served"*.
Keep it to a homepage and its assets.

It cannot shadow `/v2`, `/healthz`, `/metrics` or `/_cache` while they are
enabled — those routers are mounted before the HF catch-all, so they win by
ordering. That ordering is now load-bearing for a security property and is
pinned by a test. A **switched-off** surface has no router, so the web root can
serve a file at its path; anything it does not serve is answered locally and
never forwarded (see [Muninn's own paths never reach the
Hub](#muninns-own-paths-never-reach-the-hub)).

**Containment is enforced by resolving the path, not by comparing strings.** A
prefix check on the raw request path is the classic bypass: `..` and symlinks both
defeat it. The candidate is fully resolved and then tested for containment, so a
symlink pointing out of the root fails the same check as `../../etc/passwd`. A
configured-but-missing root logs a warning and serves nothing rather than raising
— a typo in one setting should not take down a cache whose main job is unrelated.

**It is unauthenticated by design.** Neither client-auth gate covers it — not
`XHC_DOCKER_AUTH` on `/v2`, and not `XHC_HF_AUTH` on the Hugging Face surface, which is
checked only after the web root has had its chance. A homepage is public; do not put
anything there that is not.

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

Re-verified on **hf-xet 1.6.0** (the version in the image), same file, both
ways: **PASS** with the flag unset (15/15 samples) and set (11/11). The flag's
name does not appear anywhere in the 1.6.0 library, so on that version it is
most likely not read at all, and writes are sequential without it. That is
*not* a documented guarantee, so the image still sets the flag, and you should
re-verify after any `hf_xet` upgrade:

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

> **The `/healthz` response body is part of the contract, not an implementation
> detail.** It returns `{"ok": true, "free_bytes": N}`, and blackbox probes
> commonly match on the body rather than only the status — a 200 with a broken
> cache underneath is exactly what such a probe exists to catch. Changing the
> shape silently stops those probes alarming, and **an alert that stops firing
> looks identical to one that has nothing to report.** Treat it as a stable
> interface.


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

**Every labelled series exists from startup, at zero.** Prometheus handles a
counter *reset*; it cannot handle a series that is not *there*, and those are
different failures that look identical on a graph. A counter keyed on first
increment would leave `muninn_docker_requests_total{result="UPSTREAM_AUTH"}`
absent until the first such request after each restart — so a window containing
a restart is full of holes indistinguishable from zero, and `increase()` over it
cannot tell "this never happened" from "the process restarted and nothing has
triggered this label yet". Every result/kind combination the code can emit is
therefore seeded to zero at startup, so **a zero means zero and a gap means the
process was down.** A test enumerates the call sites and fails if a new result
is added without being seeded.

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

**"Running hot" means near capacity, not out of space.** Those are different states, not points on a spectrum. If the filesystem actually fills, an ingest fails mid-stream and the client receives a **truncated body on an already-sent `2xx`**, surfacing as a digest mismatch rather than a clear error — and there is **no fallback to upstream**, because a client configured to use this cache has had its image reference rewritten, so the cache *is* its registry. Only `XHC_MISS_POLICY=redirect` sends a client upstream on a miss, and it is not the default. Watch `muninn_disk_free_bytes`: every budget-derived number reads healthy while this happens, because eviction compares the cache's own size against its own budget and never consults free space.

**Pinning an image pins its whole closure.** A pin that kept the manifest but let its layers
go would look intact until someone pulled it.

If the pin or orphan state cannot be read, **GC refuses rather than proceeding**. An absent
state file legitimately means "nothing is pinned"; an unreadable one means "unknown", and
collapsing those would silently disarm pin protection inside an unattended loop.

**Partial downloads left by a killed process are reclaimed too.** A layer is written to
`<digest>.incomplete` (or `<digest>.tier.incomplete` when it comes from the object-store
tier) and renamed into place only after its digest matches. Kill the process mid-download
and that file stays; it is not a blob, so mark-and-sweep never considered it. Each GC pass
now also sweeps partials, and so does startup (once, before the first interval). A partial
is removed only when **all three** of these hold:

1. no download in this process owns it (the in-process single-flight table);
2. no process holds its lock: every writer holds an exclusive `flock` on the file while it
   has it open, and the kernel releases it when the writer dies, so a second Muninn
   process sharing the directory is seen too;
3. it has not been written to for `XHC_DOCKER_PARTIAL_MAX_AGE` seconds (default `21600`,
   six hours). This is the backstop for a filesystem that does not honour `flock` (some
   network and FUSE mounts), so it has to exceed any silence a live download can have.
   Upstream blob reads have no read timeout, which means a stalled registry can hold a
   download open for a long time without sending a byte.

Removing a live partial would not corrupt anything, since its writer's rename fails and
that pull errors, but it would fail a pull, so every guard errs towards keeping. The GC
result carries a `partials` object: `removed`, `freed_bytes`, and what was looked at and
kept (`scanned`, `kept_owned`, `kept_locked`, `kept_young`, `max_age_s`), so a `removed: 0`
can be told apart from "found nothing to look at". Each removal is logged with its name,
size and idle time. Partial bytes are not added to the result's top-level `freed_bytes`,
which counts blobs and manifests.

**Only a file named exactly by its digest is a blob or manifest.** The GC walk admits a
file under `blobs/` or `manifests/` only if its name is 64 lowercase hex characters, and
ignores everything else rather than trying to list what to skip. Manifests, their `.meta`
sidecars and tag files are written to a temp beside the target
(`<name>.part<pid>.<8 hex>`; releases up to v0.9.29 wrote `<name>.part<pid>`) and renamed
into place. Before this rule, the walk read a manifest temp as an unreferenced manifest and
swept it. That reclaimed leftovers, but only by accident, and in the window between write
and rename it could delete a live temp, which failed the manifest write.

A temp left by a process killed before its rename is reclaimed by the same partial sweep,
on the same three guards: no write in this process owns it, no process holds its lock (the
writer holds an exclusive `flock` on the temp until the rename), and it has been idle for
`XHC_DOCKER_PARTIAL_MAX_AGE`. Only names of the form `<final>.part<digits>` or
`<final>.part<digits>.<8 hex>`, where `<final>` is a well-formed name for that tree, are
considered. Any other file is left alone. These temps count in the `partials` totals and
are also broken out under `partials.writes` with the same fields, so a reclaimed write can
be told apart from a reclaimed download.

**Abandoned push uploads are reclaimed on the same rule, and on the same age.** A push
stages each blob in `<XHC_DOCKER_DIR>/_uploads/<upstream>/<uuid>` between the `POST` that
opens the session and the `PUT` that lands it. A client that never sends the `PUT` (a
cancelled push, a CI runner killed mid-layer), or a process that dies mid-upload, used to
leave that file there permanently, because it sits outside every tree the GC walks. Up to
v0.9.30 the session itself also stayed in memory for the life of the process. Now:

- **a session expires** once it has gone `XHC_DOCKER_PARTIAL_MAX_AGE` without a request and
  nothing is running inside it. Expiry deletes its staging file. The next request on it gets
  `404 BLOB_UPLOAD_UNKNOWN`, which is the OCI answer for a session the registry no longer
  has, and a docker client restarts the upload. Expiry counts idle time, not total time, so
  an upload that keeps sending chunks never expires. A `PUT` still pushing upstream in
  `proxy` mode counts as activity for as long as it runs.
- **a staging file is removed** by the partial sweep only when no live session in this
  process owns it, no process holds its lock (taken while a chunk is being written), and it
  has been idle for `XHC_DOCKER_PARTIAL_MAX_AGE`. Only uuid-named files are considered.
  These are broken out under `partials.uploads`.

The two share one age on purpose. If a session could stay alive longer than the sweep waits,
the sweep could delete the file of a session running in another process. As a second
guard, a session checks before every chunk and before landing that its staging file exists
and is exactly as long as what it has written. If not, the session is dropped with
`BLOB_UPLOAD_UNKNOWN`. Before this check, a staging file removed mid-upload was silently
recreated empty, and the upload finished with a correct digest over bytes that were never
all on disk.

Six hours is far longer than a live session is ever idle: a docker client sends its next
`PATCH` or `PUT` within seconds. It is also short enough that an abandoned upload's bytes go
within one GC interval of it.

**The store-forward pending area is swept at startup in both layouts.** A process killed
while writing an obligation leaves a `.writing` temp (or, with `XHC_STATE_DIR` set, a
`.partial` or `.linking` copy of held bytes). Up to v0.9.30 these were removed only when
`XHC_STATE_DIR` was set, so the default `<XHC_DOCKER_DIR>/_pending/` kept them for good. Now
startup removes them from `<XHC_DOCKER_DIR>/_pending/` always, and from the state dir's
pending area when it is set. It runs before the first request is served, so no write in the
process can own them yet.

### `XHC_DOCKER_TAG_TTL` has three regimes, and `0` is the surprising one

| value | meaning |
| --- | --- |
| `300` (default) | trust a tag→digest mapping for that many seconds |
| `0` | **never** revalidate — mutable tags are frozen at whatever was first cached |
| `always` | revalidate on every request |

**`0` means never, not always.** It is the value an operator reaches for wanting the strictest
behaviour, and it selects the loosest — so the knob fails toward staleness in the direction of
"I thought I turned checking on". `0` is unchanged because deployments rely on it; `always` is a
new spelling rather than a redefinition, so upgrading changes nothing.

A negative value is also read as `always`, because that is what someone guesses when they want
"no caching" — and silently treating it as `never` is the exact surprise this exists to remove.

The boot log prints the **resolved regime in words** (`tag_ttl=always-revalidate`,
`tag_ttl=NEVER-revalidate`, `tag_ttl=300s`) rather than the raw number, because `tag_ttl=0s`
reads like "no delay" and gives an operator no way to tell which of the three they have.

### A full disk degrades the pull instead of breaking it

**Under budget is not the same as having room.** Eviction compares the cache's own size against
its own budget (`XHC_DOCKER_MAX_SIZE`) and never consults free space — so on a shared filesystem
anything else can fill the volume while this cache sits far under budget.

Before `XHC_DOCKER_MIN_FREE`, every ingest then failed **mid-stream**, after the client already
had a `2xx`, arriving as a truncated body and a digest mismatch with the real cause invisible.
**And there is no fallback for the client to take** — a node configured to use this cache has had
its image reference rewritten, so the cache *is* its registry.

Below the floor, a miss is now **streamed straight through to the client and not cached**. The
pull gets slower; it does not break. Responses carry `x-xhc-cache: BYPASS-NO-SPACE`, because
"the cache is cold" and "the cache cannot write" look identical from the client otherwise, and
the condition is logged at `WARNING` on every bypass.

**It does not evict to make room, deliberately.** On a filesystem with no quota or reservation,
space freed here does not come back to this cache — it goes to whatever is filling the volume.
The cache would shrink, evict again, and end up small *and* still failing, having destroyed warm
data to get there. Refusing to cache is reversible and costs nothing that was already paid for.

An **unreadable** filesystem keeps caching rather than switching to bypass: the risk being
guarded here is a slowdown, not data loss, so unknown resolves to the status quo. That is the
opposite of how pin state resolves unknown, and deliberately so.

Watch `muninn_disk_free_bytes` — it is the only figure that moves here, because every
budget-derived number reads healthy throughout.

### What a failed pull means, by status

The docker CLI **discards the body and headers and prints only the status**, so the status is
the entire message most people ever see. Measured on docker 29.5.1: `404` renders as
`not found` and nothing else, while `401`, `403` and `502` all render as
`unexpected status ...: <code> <reason>`. **404 is the only status that hides itself**, which
is why an auth failure must not wear one.

| status | means | who fixes it |
| --- | --- | --- |
| `404` | not in the cache and the upstream does not have it — including a mistyped image name, and a private repo the cache holds no credentials for | whoever ran the pull |
| `403` | refused by this cache's own policy (`XHC_DOCKER_POLICY`, allow/deny lists) | the cache operator |
| `502` | the cache could not authenticate to the upstream — credentials missing, wrong, expired, or lacking scope | the cache operator |
| `401` | authenticate **to the cache** (only when `XHC_DOCKER_AUTH=basic`) | whoever ran the pull |

`401` is reserved for the cache's own client-facing auth and is never used for an upstream
failure: *"authenticate to the cache"* and *"the cache cannot authenticate upstream"* are
different actors with different fixes.

Every failure carries diagnostic headers for logs and `curl`, since the CLI will not show them:
`x-xhc-upstream-status`, `x-xhc-upstream-auth` (`unconfigured`, `rejected`, `anonymous-refused`
or `n/a`), and `x-xhc-hint` where an explanation helps.

**Why a nonexistent repo is a `404` even though the upstream said `401`.** Docker Hub and ghcr
refuse to leak existence: for a repo that does not exist they issue a valid anonymous token and
then answer `401` to the request carrying it. So *"you mistyped the name"* and *"this is private
and you cannot see it"* arrive as the same response, and neither is a fault of the cache. The
cache distinguishes **being refused after authenticating** — an answer, rendered `404` — from
**being unable to authenticate at all**, which is a genuine gateway problem and stays `502`.
Rendering the first as `502` told users the infrastructure was broken when they had simply
mistyped an image name.

### Docker management endpoints

Part of the management API: off unless `XHC_MANAGE_TOKEN` is set, and behind the same
bearer token.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/_cache/docker/prewarm` | pull an image and its closure ahead of a rollout; returns a job |
| `GET` | `/_cache/docker/prewarm/{id}` | poll it; survives a restart as `interrupted` |
| `GET` | `/_cache/docker/prewarm` | every prewarm job the ledger holds, running first, and the ledger's health |
| `GET` | `/_cache/docker/images` | cached tags, with pin and orphan state |
| `GET`/`POST`/`DELETE` | `/_cache/docker/pins` | pin an image and its blob closure |
| `DELETE` | `/_cache/docker/images` | drop a tag; frees every layer no other tag references. `?sweep=1` reclaims now and reports the bytes, otherwise the next sweep does it |
| `POST` | `/_cache/docker/gc` | run mark-and-sweep now (`?dry_run=true` to see what would go) |
| `GET` | `/_cache/docker/pending` | store-forward: what has been accepted but not yet confirmed upstream |
| `DELETE` | `/_cache/docker/pending` | give up on one outstanding forward and unpin it — **the content is then evictable and may be the only copy** |

Prewarm is fire-and-forget, so nobody holds an HTTP connection open across a 30 GB pull.
**Pass a digest rather than a tag** for anything you intend to reproduce — a tag can move
mid-pull and assemble a tree from two commits.

**Prewarm jobs move through the same states as the Hugging Face jobs**
(`pending → running → verifying → done`, or `error`), and **`done` means verified**:

- every blob fetched was hashed as it landed and renamed into place only on a match;
- every manifest was checked against its digest before it was stored — and when the
  prewarm asked for it **by digest**, against *that* digest, not just the one the
  upstream's response header named;
- `verifying` then confirms the whole closure (every manifest and blob) is on disk at
  its content address. That step catches a real case: an image prewarmed by digest with
  `"pin": false` is referenced by no tag, so a garbage-collection sweep during a long
  pull can take the early layers. That job ends in `error`, not `done`.

**A by-digest prewarm is pinned by default.** No tag points at an image fetched by
digest, so without a pin the next GC sweep would collect the image you just asked to
have warm, and a sweep during the pull would take its early layers. So when the
request does not say, `pin` is decided by the reference:

| `pin` in the request | by digest (`…@sha256:…`) | by tag (`…:1.2.3`) |
| --- | --- | --- |
| absent | **pinned** | not pinned (the tag already protects it; a pinned tag is exempt from capacity eviction) |
| `true` | pinned | the tag is pinned |
| `false` | not pinned: the old behaviour, collectable once the job ends | not pinned |

The pin is written **before the first byte is fetched**, so a sweep mid-pull already
honours it. It is an ordinary pin: it appears in `GET /_cache/docker/pins` as
`<upstream>/<repo>@sha256:…`, the job reports the exact entry as `pinned_as`, and you
remove it with `DELETE /_cache/docker/pins` `{"image": "<that entry>"}`, after which the
image is ordinary garbage. **Pins accumulate:** every distinct by-digest prewarm adds one,
and nothing expires them. Review `GET /_cache/docker/pins` when rolling a release forward,
or send `"pin": false` for images you only need warm for one rollout.

If the prewarm ends in `error`, a pin **that job added** is removed again (a pin that was
already there is left alone). An `interrupted` prewarm keeps its pin, because re-submitting
resumes it. If `pins.json` cannot be read, a pinning prewarm fails rather than rewriting
the file: saving over an unreadable pins file would silently drop every other pin.

Blobs already on disk are **not re-hashed** — they only ever arrive by a verified
rename — and the job says how many there were (`blobs_present`, a subset of
`blobs_done`). `bytes_done` counts only the bytes fetched by this job.

**Prewarm jobs survive a restart,** on the same terms as the Hugging Face job ledger
(see *Jobs, restarts, and whether a snapshot is complete*): they are kept in
`prewarm.json` in the OCI state directory (`<XHC_DOCKER_DIR>/.xhc/`, or
`$XHC_STATE_DIR/oci/`); a prewarm that was running when the process stopped is reported
as **`interrupted`** with its last recorded counts; at most the newest **50** finished
or interrupted prewarms are kept, for at most **7 days**; the write rate and the atomic
write are the same; and an unreadable file is moved aside as
`prewarm.json.corrupt.<epoch>` and never stops the cache. A graceful stop cancels
running prewarms and records them as `interrupted` straight away.

**Resuming is re-submitting.** `POST` the same image again: blobs already cached are
skipped with no upstream request, so only what the interrupted run had not finished is
fetched, and the new job's `resumes` names the one it carries on from. Manifests are
fetched again, which is a few small requests. Re-submitting while the same prewarm is
still running returns that job instead of starting a second one (a request with a
different `pin` value is a different prewarm).

### Docker configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `XHC_DOCKER_ENABLED` | `1` | serve `/v2/*` at all |
| `XHC_DOCKER_DIR` | `/docker` | storage root; use a separate volume |
| `XHC_DOCKER_MAX_SIZE` | unset | capacity budget for the image cache |
| `XHC_DOCKER_DEFAULT_UPSTREAM` | `docker.io` | used when the first path segment has no dot |
| `XHC_DOCKER_TAG_TTL` | `300` | seconds a tag→digest mapping is trusted. **`0` means NEVER revalidate**, not always — use `always` for check-every-request |
| `XHC_DOCKER_POLICY` | `open` | `open`, `allowlist` — parity with `XHC_INGEST_POLICY` |
| `XHC_ALLOW_REGISTRIES` / `XHC_DENY_REGISTRIES` | unset | host globs, **honoured in `open` mode too** |
| `XHC_ALLOW_IMAGES` / `XHC_DENY_IMAGES` | unset | globs over `<upstream>/<repo>` |
| `XHC_DOCKER_MAX_BLOB_BYTES` | unset | refuse an oversized layer before bytes move |
| `XHC_DOCKER_MIN_FREE` | `1G` | below this much free space, a miss is **proxied to the client uncached** instead of ingested; `0` disables |
| `XHC_DOCKER_PARTIAL_MAX_AGE` | `21600` | seconds a `.incomplete` blob, a manifest or tag write temp (`.part<pid>`), or a push upload's staging file must go unwritten before GC may remove it, and then only if nothing in this process owns it and no process holds its lock. Also how long a push upload session may sit idle before it expires. See *Garbage collection is mark-and-sweep* |
| `XHC_REGISTRY_AUTH_FILE` | unset | mounted `~/.docker/config.json` for upstream credentials |

> **Policy defaults to `open`**, at parity with the Hugging Face side. Path-prefix routing
> means anyone who can reach the port can pull from **any** registry onto your array. The
> server warns at boot in that state. Setting `XHC_ALLOW_REGISTRIES` is the cheapest useful
> hardening and does **not** require flipping the whole policy to `allowlist`.
>
> This is a separate concern from the trust boundary below. That one is about **who may read
> what the cache holds** and is deliberate; this one is about **which upstreams anyone may
> make the cache fetch from**, which is worth narrowing even on a network you trust.

### Push-through

**Off by default.** `XHC_DOCKER_PUSH=1` enables it.

```bash
docker push <cache-host>/ghcr.io/you/image:latest
```

The image is forwarded to `ghcr.io/you/image:latest` **and** kept in the cache, so
the next node to pull it gets a local hit rather than a cold fetch.

| Variable | Default | Meaning |
| --- | --- | --- |
| `XHC_DOCKER_PUSH` | `0` | accept pushes at all |
| `XHC_DOCKER_PUSH_MODE` | `proxy` | `proxy` or `store-forward`. Hyphen, not underscore; an invalid value refuses to start rather than falling back |
| `XHC_DOCKER_CACHE_ON_PUSH` | `1` | keep the pushed image locally |
| `XHC_DOCKER_PUSH_LIMITS` | unset | path to a regctl-format file giving per-registry chunk sizes |
| `XHC_DOCKER_BLOB_CHUNK` | unset | global chunk size for hosts with no entry in that file |
| `XHC_DOCKER_PUSH_PENDING_MAX_SIZE` | unset | with `XHC_STATE_DIR` set, the most `store-forward` may hold on the state volume awaiting upstream (e.g. `20G`). Over it, pushes are refused with `507`. See [What survives what](#what-survives-what) |

A worked example, since the settings above are easier to read than to assemble:

```yaml
environment:
  XHC_DOCKER_PUSH: "1"
  XHC_DOCKER_PUSH_MODE: store-forward      # answer 201 early, forward behind
  XHC_DOCKER_CACHE_ON_PUSH: "1"            # keep the image after forwarding
  XHC_DOCKER_PUSH_LIMITS: /auth/push-limits.json
```

The mode is **global**, not per-registry — there is one push mode for the whole cache.
`XHC_DOCKER_PUSH_LIMITS` is the only per-registry push setting.

#### The point: chunk limits stop being every client's problem

A `docker push` does a **monolithic PUT** and has no chunk-size knob. A registry behind a
body-size-limiting proxy rejects that outright, so pushing to one means knowing a number
the OCI protocol never advertises — which is why tools like `regctl` have to be configured
per host, and why plain `docker push` fails against such a registry for large layers.

Muninn decouples the two: the client pushes normally, and **what goes upstream is
re-chunked per registry**. Point `XHC_DOCKER_PUSH_LIMITS` at a regctl-format file — the
same shape `regctl registry set --blob-chunk` already produces, so if you have solved this
before you already have the values:

```json
{"hosts": {"registry.example.com": {"blobChunk": 16777216, "blobMax": 16777216}}}
```

`blobMax` is the threshold above which to chunk; `blobChunk` is the piece size. Muninn reads
**only** those two fields and ignores any credentials in the file — mount a limits-only file
rather than a real regctl config.

**If you configure nothing, it still works.** On a `413` Muninn halves the chunk, retries,
and logs the exact config line to add. You learn the number from your own logs instead of
bisecting a production registry. That depends on the proxy returning a clean `413`; some
drop the connection instead, which is indistinguishable from a network failure, and those
need configuring by hand.

#### `proxy` vs `store-forward`

`proxy` (default) confirms upstream **before** answering the client, so a `201` means the
registry really has it. `store-forward` answers as soon as the content is on disk and
pushes behind, which is faster and survives upstream flakiness — but **it tells the client
the push succeeded before it has.** CI that pushes and then triggers a pull elsewhere can
race it. The mode that can lie is the one you have to ask for.

In `store-forward`, content that has not yet been confirmed upstream is **pinned**: it is
the only copy in existence and cannot be re-fetched, so eviction and GC leave it alone. A
push that fails stays pinned and stays visible rather than being tidied away.

The two settings compose rather than conflict. `XHC_DOCKER_PUSH_MODE` decides **when the
client is answered**; `XHC_DOCKER_CACHE_ON_PUSH` decides **whether the copy is kept**. With
`store-forward` and `CACHE_ON_PUSH=0` the store is **ephemeral** — the content survives long
enough to be forwarded and is deleted once upstream confirms. That is a useful combination:
absorb the upload and push behind, without the pushed image occupying the cache.

#### What `store-forward` actually promises

A `201` in `store-forward` means **accepted and owed**, not **delivered**. The mode is
*eventually* consistent rather than immediately consistent, and everything below exists so
that "eventually" is a commitment rather than a hope.

- **Manifests are deferred until their blobs are upstream.** A manifest that lands before
  its layers is a tag that resolves to a broken image, which is worse than a tag that does
  not resolve yet. Muninn holds the manifest until every blob it references is confirmed.
- **Transport failures retry with backoff** (5 attempts, 1s/4s/15s/45s). A registry that is
  briefly unreachable does not cost you the push.
- **Outstanding forwards survive a restart.** Each one is recorded on disk before the client
  is answered and cleared only on confirmation, so a forward interrupted by a restart — or
  one that had already exhausted its retries — is re-enqueued on the next boot rather than
  lost. With `XHC_STATE_DIR` set they also survive losing the docker dir: see
  [What survives what](#what-survives-what). Startup logs each recovered obligation at `WARNING`: those clients were told `201`
  and the upstream does not have their content yet. An obligation marker that cannot be
  *read* is left in place rather than discarded, because unreadable is not the same as
  absent.
- **Nothing pending is evictable.** Unconfirmed content is the only copy in existence, so
  it is pinned and GC leaves it alone.

#### What survives what

A pending push is three things: the **record** of what is owed (upstream, repository,
digest, and for a manifest the tag it goes to), the **bytes** (the blob, or the manifest
body), and the upstream **credentials**. The credentials come from `XHC_REGISTRY_AUTH_FILE`,
so they survive whatever your configuration survives. An upload the client never finished
was never answered `201`, and is not kept: the client retries it. Its session expires, and
its staged bytes are reclaimed, after `XHC_DOCKER_PARTIAL_MAX_AGE` idle (see
*Garbage collection is mark-and-sweep*).

| | `XHC_STATE_DIR` unset | `XHC_STATE_DIR` set |
|---|---|---|
| record | `<XHC_DOCKER_DIR>/_pending/<key>.json` | `$XHC_STATE_DIR/oci/pending/<key>.json` |
| manifest body | inside the record, and in the cache | inside the record, and in the cache |
| blob bytes | in the cache only | `$XHC_STATE_DIR/oci/pending/<key>.blob`, and in the cache |
| record cannot be written | push **refused** (`507` if full, `503` otherwise) | push **refused** (`507` if full, `503` otherwise) |
| process restart | survives | survives |
| docker dir lost or replaced | **lost** | **survives** |
| state dir lost | n/a | **lost** |

- **No `201` without a record, in either mode.** The record is written, and synced,
  before the client is answered. If it cannot be written the push is refused, not
  acknowledged and then forgotten at the next restart. Before this, a failed write was
  only logged and the client still got `201`.
- **Without `XHC_STATE_DIR`, the docker dir is the durable storage.** A pending push
  survives a restart but not the disk. Do not put the docker dir on disposable storage with
  `store-forward` unless you also set `XHC_STATE_DIR`.
- **With it set, the bytes are held on the state volume before the client gets its `201`.**
  If the state dir and docker dir share a filesystem the hold is a hard link and costs
  nothing; otherwise it is a copy, hashed and checked against the digest and synced
  before it counts. Once the upstream confirms, the held copy is deleted. The cache copy
  stays or goes according to `XHC_DOCKER_CACHE_ON_PUSH`. If the docker dir is replaced
  while a push is pending, the next boot puts the image back into the cache from the held
  copy, so pulls from this cache still find it, and forwards it.
- **The state volume needs room for what is in flight.** The hold is refused, and so is
  the push, if copying it would leave less than 64 MiB free there, or would take the
  pending area over `XHC_DOCKER_PUSH_PENDING_MAX_SIZE`. The client gets
  `507 Insufficient Storage` and nothing is recorded, pinned or kept, so it can retry
  once the queue drains. Muninn never accepts a push and drops it later.
- **Upgrading.** The first boot with `XHC_STATE_DIR` set moves each existing
  `<XHC_DOCKER_DIR>/_pending/*.json` to the state dir and holds its bytes there first. If
  the bytes cannot be held, the boot stops and the old record stays where it was. Records
  are **moved**, not copied like pins: a stale second copy would re-send a manifest to a
  tag that may have moved on since. If a record's bytes are already gone, the record still
  moves and shows up as a failed forward.
- **Unsetting `XHC_STATE_DIR` again strands pending pushes in the state dir.** Wait until
  `GET /_cache/docker/pending` is empty first.

`GET /_cache/docker/pending` is the window into all of it, one row per outstanding forward
with `state` (`running`, `retrying`, `failed`, `cancelled`, `done`), the `error` text if it
failed, `attempts`/`max_attempts`, and whether it is `pinned`. **An empty queue means
delivered** — that is the distinction the whole mechanism exists to create, and it is worth
checking directly against the upstream at least once rather than trusting the queue alone.

Giving up is explicit: `DELETE /_cache/docker/pending` abandons one forward and unpins it.
That is a deliberate decision to break the promise made to whoever pushed, and it makes the
content evictable when it may be the only copy anywhere. Nothing does it automatically.

#### What enabling push means

> **Push is not gated by authentication.** If push is enabled and client auth is off,
> **anyone who can reach this cache can push to any registry it holds credentials for**,
> under the cache's identity, with no attribution — a `docker push` cannot identify
> itself. That is the same trust model as the pull surface rather than an exception to it.
> Restrict who can reach the port, or set **both** `XHC_DOCKER_AUTH=basic` **and**
> `XHC_DOCKER_HTPASSWD` — **the file alone is ignored**, because auth defaults to `none`
> and the loader returns before ever opening it. Muninn warns at boot; it does not refuse.
>
> This sentence named only the file until 0.9.6, so an operator could follow it exactly,
> restart, see no error, and still be serving an unauthenticated push-through cache. The
> file being set while auth is `none` now logs a warning naming both variables.

**Not implemented:** delete and cross-repo mount. Removing upstream content is a retention
decision for that registry's owner, not for a cache sitting in front of it.

### Optional client auth on the pull surface

Off by default. Nothing changes unless you turn it on.

| Variable | Default | Meaning |
| --- | --- | --- |
| `XHC_DOCKER_AUTH` | `none` | `none`, `basic` |
| `XHC_DOCKER_HTPASSWD` | unset | path to a bcrypt htpasswd file; **required** when `basic` |

```bash
htpasswd -B -c ./htpasswd hiro      # bcrypt; -B is not optional
htpasswd -B    ./htpasswd mimir
```

```yaml
environment:
  XHC_DOCKER_AUTH: basic
  XHC_DOCKER_HTPASSWD: /auth/htpasswd
volumes:
  - ./htpasswd:/auth/htpasswd:ro
```

Then `docker login <cache-host>` as usual. **bcrypt only** — Apache's other formats are
unsalted or broken, and silently accepting one would make a weak file look configured, so
they are refused at startup with the line number.

> **On its own this is a GATE, not per-client isolation.** With `XHC_DOCKER_AUTH=basic`
> alone, everyone who authenticates sees **everything the cache holds**. One credential per
> consumer still buys you revocation, which is worth having on a shared cache — but it buys
> only that.
>
> **`XHC_AUTHZ_DB` adds real per-key authorization** to the `/v2` surface. See
> *Per-key authorization* below, including what it still does not do.

#### Per-key authorization (`XHC_AUTHZ_DB`)

Replaces the flat htpasswd with a SQLite store of principals, keys and rules, so a
credential can be allowed to pull `docker.io/library/*` and nothing else, or to push to one
repository while pulling from several.

```yaml
environment:
  XHC_AUTHZ_DB: /srv/authz/authz.db
```

Set it and a Basic credential becomes **key id as the username, key secret as the
password**. It *replaces* the htpasswd gate rather than layering on it — two credential
stores answering the same question is how one of them silently stops being consulted.

Rules are patterns over `<upstream>/<repository>`, each granting pull, push or both — and,
with `XHC_HF_AUTH=key`, over Hugging Face repositories as `models/<repo>`, `datasets/<repo>`
or `spaces/<repo>`, the same shape as `XHC_ALLOW_REPOS`:

```
docker.io/library/*          pull
ghcr.io/myorg/*              pull+push
models/google/gemma-4-*      pull      # some of one organisation's models
datasets/*                   pull      # every dataset
*                            pull+push # anything, anywhere, Hugging Face included
```

`*` spans `/`. Patterns match the repository and **never the tag**, so
`docker.io/library/alpine pull` covers every tag of alpine. The list is **allow-only with
no precedence**: a rule can grant, nothing can deny, and an **empty list grants nothing**.
There is no ordering to get wrong and no deny rule that a later grant can quietly override.

Revocation beats every grant: disabling a key, or the principal holding it, refuses
everything immediately — including authentication, so `docker login` fails rather than
succeeding and leaving the user to discover the revocation on their next pull.

With `XHC_OIDC_ISSUER` also set, users manage their own keys through a browser login on the
cache's own homepage, and an administrator sets each user's allowlist. See
*Interactive login* below.

**Authorization runs at reference resolution, before hit-or-miss is decided.** That is what
makes it honest rather than decorative, and it is worth stating because the obvious
implementation is not honest: a check performed only when fetching from upstream would be
enforced on the miss and silently absent on every hit after it — false from the first cache
fill. Here the decision is coupled to `_resolve_or_error` in the same call that parses the
reference, so a route cannot serve a repository it did not authorise without also failing
to work out which repository it is. The Hugging Face surface is built the same way; see
*Rules on the Hugging Face surface* below.

#### Rules on the Hugging Face surface (`XHC_HF_RULES`)

With `XHC_HF_AUTH=key`, the same rules decide which Hugging Face repositories a key may pull.
This matters more here than on `/v2`: the cache fetches from the Hub **with its own token**,
and that token has accepted gated licences and can see private repositories. Without rules,
every live key borrows all of it.

| repository | rule reference |
|---|---|
| model `myorg/llama-ft` | `models/myorg/llama-ft` |
| canonical model `gpt2` (no org) | `models/gpt2` |
| dataset `myorg/corpus` | `datasets/myorg/corpus` |
| space `myorg/demo` | `spaces/myorg/demo` |

- **The same shape as `XHC_ALLOW_REPOS`.** A rule recorded as `models/<org>/<name> pull`
  before rules reached this surface is enforced as it stands; nothing needs migrating.
- **The first segment decides the surface.** A rule starting `models/`, `datasets/` or
  `spaces/` grants only Hugging Face repositories and never an image; every other rule grants
  only images and never a repository. A registry reference always starts with a host
  (`docker.io`, `ghcr.io`, `localhost:5000`), so the text could not collide anyway, but the
  surface check does not rely on that: a registry pattern such as `*/org/*`, which *would*
  match the text `models/org/x`, grants nothing here. **Only a bare `*` grants on both.**
- **Wildcards.** `*` spans `/` and `?` is one character, on both surfaces. Rule matching is
  **case-insensitive**. `XHC_ALLOW_REPOS` / `XHC_DENY_REPOS` use Python's `fnmatch`, where `*`
  also spans `/` but matching is **case-sensitive** on Linux: `models/Org/*` in the ingest
  allowlist does not match `models/org/x`, while the same rule here does.
- **All of Hugging Face** is `models/*`, `datasets/*` and `spaces/*` together. That covers
  every repository and every listing, but **not** endpoints that name no repository
  (`whoami-v2`, collections, papers, anything the Hub adds later): those need a bare `*`.
- **Pull only.** Muninn never pushes to the Hub.
- **A key scope narrows it the same way.** A key scoped to `docker.io/library/*` cannot pull
  models, even if its holder can.

**No rule is stored that could never match.** Saving rules — from the console,
`/_cache/authz` and `authzctl` alike — refuses with 400 and a named reason:

| rule | result |
|---|---|
| `*`, `* pull+push` | accepted; both surfaces |
| `models/google/gemma-4-* pull`, `models/gpt2`, `datasets/*`, `spaces/org/demo` | accepted; Hugging Face |
| `docker.io/library/*`, `localhost/app`, `registry.local:5000/x`, `<default upstream>/…` | accepted; registry |
| `*/library/*` (wildcard first segment) | accepted; registry only |
| `models/org/x push` | refused: the Hugging Face surface is pull-only |
| `models`, `datasets/` | refused: names no repository |
| `hf/models/org/x` | refused: `hf/` is not a prefix; use `models/…` |
| `model/org/x`, `dataset/…` | refused: unknown type prefix |
| `google/gemma`, `library/*` | refused: neither a registry host nor a Hugging Face type |

The last row is also a change on `/v2`: a registry rule without a host, such as
`library/*`, never matched anything, because references always carry their host. It is now
refused instead of being stored.

**Enforced on every request, on hits as well as misses.** The decision is taken from the path
alone at the top of the Hugging Face catch-all, before the cache is consulted, and every
handler that serves a repository re-checks that the repository it is about to serve is the
one that was authorised — so a path the two parsers read differently is refused rather than
served. Every path gets a decision; none is waved through for not looking like a repository:

| path | needs a rule matching |
|---|---|
| anything naming a repo: `/<repo>/resolve/…`, `/datasets/<repo>/…`, `/api/models/<repo>[/…]` (info, `revision`, `tree`, `refs`, `paths-info`, `xet-read-token`, …), `/api/datasets/<repo>/parquet`, `/datasets-server/…?dataset=<repo>`, web views like `/<repo>/raw/…` | `<type>s/<repo>` |
| a listing or search over one type: `/api/models`, `/api/datasets?search=…` | `<type>s/` — i.e. `models/*` or `*` |
| everything else: `/api/whoami-v2`, collections, papers, endpoints the Hub adds later | a bare `*` only |

Listings need a type-wide grant because they answer with the **cache's** Hub identity and can
name private repositories it can see. `whoami-v2` needs `*` because it
answers with the cache's own account — name, email, organisations — not the caller's; no
download calls it. Where a path could name two repositories — `/api/models/org/refs` is
either `org/refs`'s info or canonical `org`'s refs, and Muninn cannot know which the Hub will
choose — **both** must be allowed. That includes a sub-resource Muninn has never heard of, so
a narrow key is refused a brand-new Hub endpoint rather than having it guessed at.

**Paths with `.`, `..` or empty segments are refused with 400, in every mode** — with rules
off and with `XHC_HF_AUTH=none` too. The upstream client normalises them, so
`org/allowed/../secret` would otherwise be checked as one repository — by a key's rules, or by
`XHC_ALLOW_REPOS` — and fetched as another.

**A refusal is `403` with `X-Error-Code: GatedRepo`** and an `X-Error-Message` naming the key
and the repository, e.g. `refused by this cache's rules: key 3f2a… has no rule granting pull
on models/myorg/secret`. Not 404, which would send the user looking for a typo in a
correct repo id. `GatedRepo` because that is the situation — the repository exists and this
credential is not on its list — and because `huggingface_hub` re-raises it as
`GatedRepoError` from the `HEAD` every download starts with, where a plain 403 is swallowed
into *"check your connection"*. Unlike `/v2`, whose client prints only the status, the
reason is sent to the caller; it describes only their own key and never lists rules.

> **Upgrading with `XHC_HF_AUTH=key` already on: this changes who can pull.** Enforcement is
> the default. A principal whose allowlist is `*` is unaffected. A principal with only
> registry rules — `docker.io/*` and nothing else — is now **refused every Hugging Face
> repository**, and so is any key scoped to registry patterns only, even one held by a `*`
> principal. Before upgrading, add `models/…` / `datasets/…` rules for those principals, or set
> `XHC_HF_RULES=off` to keep the previous behaviour (any live key pulls anything) while you
> do. With `XHC_HF_AUTH=none` — the default — nothing changes at all.

**What it still does not do, and these are limits rather than bugs:**

- **It covers the Hugging Face path only with `XHC_HF_AUTH=key`.** Without it there is no
  credential on that surface, so there is nobody to hold a rule, and model and dataset
  traffic is gated only by `XHC_INGEST_POLICY` / `XHC_ALLOW_REPOS`, which are
  **server-wide**. With it, rules apply per key unless `XHC_HF_RULES=off`.
- **Allowing a path grants whatever is already cached there.** The cache is shared storage
  and holds no per-tenant copies. If one tenant pulls a private image, a second tenant whose
  rules cover that path is served it from disk — Muninn does not re-check their entitlement
  with the upstream registry, because on a hit it never contacts the upstream at all. **Do
  not use one Muninn to separate tenants who must not read each other's private images.**
  Rules decide which *paths* a key may use; they do not re-derive upstream entitlement.
- **The blast radius of the cache's own upstream credentials is unchanged.** Anything
  `XHC_REGISTRY_AUTH_FILE` can reach, any key allowed that path can reach through the cache.

**It gates `/v2/*`, and the Hugging Face surface when `XHC_HF_AUTH=key`, and nothing
else.** `/healthz` and `/metrics` stay unauthenticated by design, and `/_cache` keeps its own separate `XHC_MANAGE_TOKEN` — a pull credential does not
open the management API. This is the main advantage over a blanket reverse-proxy rule, which
swallows the health and metrics endpoints unless you carve them out by hand.

**It fails closed.** `basic` with a missing, unreadable, empty or non-bcrypt htpasswd file
**refuses to start**. An absent config means you did not ask for auth; an unreadable one when
you *did* means unknown, and resolving unknown to permissive is how a cache silently reopens
itself after someone loses a file.

Basic auth sends credentials in clear, so put TLS in front. Muninn cannot see its own front,
so it warns at boot rather than refusing.

*Why per-host credentials rather than one shared secret:* a `docker pull` cannot send an
identifying header and Muninn's OCI path records no principal, so credentials are the only
mechanism by which this cache can ever know which node pulled what. A shared secret does not
defer that, it forecloses it — and revoking one node becomes impossible.

### Interactive login (`XHC_OIDC_ISSUER`)

So that users of a shared cache can manage their own credentials without a shell on the box.
Entirely optional: with no issuer configured there is no login, no session cookie and no
management surface — the routes are not mounted at all, rather than mounted and disabled.

```yaml
environment:
  XHC_AUTHZ_DB:            /srv/authz/authz.db      # required
  XHC_OIDC_ISSUER:         https://accounts.example.com
  XHC_OIDC_CLIENT_ID:      muninn
  XHC_OIDC_CLIENT_SECRET:  ...
  XHC_OIDC_REDIRECT_URI:   https://cache.example.com/_auth/callback
  XHC_SESSION_SECRET:      ...                      # signs the session cookie
```

Register `https://<your-host>/_auth/callback` with your provider. Any provider with OIDC
discovery works; nothing here names one.

**Setting `XHC_OIDC_ISSUER` without the rest refuses to start**, naming what is missing. A
login route that 500s on the first real user is worse than a service that will not boot,
because by then nobody is watching.

**The first person to log in becomes admin.** The claim is made in a single `BEGIN IMMEDIATE`
transaction, so two simultaneous first logins cannot both win — a race with exactly one
correct answer, deciding who administers the service. With
[admin from the identity provider](#admin-from-the-identity-provider-xhc_oidc_admin_claim)
configured, this does not happen: being first grants nothing.

After that:

| | |
| --- | --- |
| any logged-in user | create, list, disable and delete **their own** keys |
| admin | see all users, set each user's allowlist, grant/revoke admin, disable a user |

**A user manages the credentials they hold; an admin decides what those credentials may
do.** The allowlist belongs to the user, not the key, and only an admin can write it — if a
user could edit their own, "create a key" and "grant myself push to everything" would be the
same operation. A newly created key therefore grants nothing until an administrator sets an
allowlist, which the UI says rather than leaving the user to discover.

#### Admin from the identity provider (`XHC_OIDC_ADMIN_CLAIM`)

For an organisation whose provider already says who administers what: admin follows a
role or group there, so **revoking it at the provider removes admin here**.

```yaml
environment:
  XHC_OIDC_ADMIN_CLAIM: realm_access.roles   # Keycloak realm roles
  XHC_OIDC_ADMIN_VALUE: muninn-admin
```

- **Admin is recomputed at every login** from the verified id_token: granted when the claim
  carries the value, **revoked** when it does not — including for someone who was admin
  before.
- **The first-login grant is off.** The first person to log in is not made admin; with a
  provider deciding the role, being first proves nothing.
- **The claim** is a name, or a dotted path into nested claims (`realm_access.roles`). A
  top-level claim whose own name contains dots — namespaced claims are URLs, such as
  `https://cache.example.com/roles` — is matched as a whole first, then the dotted path.
- **Matching is exact**: the claim equals the value, or is a list with an element equal to
  it. No prefix, substring or case-folding, and a claim of any other type (boolean, number,
  object) grants nothing.
- **The claim must be in the id_token.** Some providers put groups only in the access
  token or at the userinfo endpoint; map it into the id_token and request whatever scope
  carries it (`XHC_OIDC_SCOPES`). An id_token **without** the claim is not admin, and the
  first time that happens the log names the claim and lists the claim *names* the token did
  carry, once per process — a provider that never emits it looks exactly like "nobody is an
  admin" otherwise.
- **Both variables or neither.** One without the other refuses to start, naming the missing
  one, as does either without `XHC_OIDC_ISSUER`.
- **The console's admin toggle is gone** in this mode, and `POST
  /_console/users/{subject}/admin` returns `409`: the target's next login would silently
  undo it. Grant or revoke the role at the provider.

**How fast a revocation lands.** Muninn learns of a change at the provider **at that user's
next login**; nothing here polls the provider. Once a login has recorded it, it applies to
every session that user holds **on its next request**, because admin is never in the session
cookie — each request re-reads the principal. So an admin whose role is revoked at the
provider, and who does not log in again, keeps admin until their session expires: the bound
is **`XHC_SESSION_TTL`** (12 hours by default). Shorten it if that is too long. To cut a
session off sooner, revoke in the store as well — `authzctl revoke-admin` or disabling the
user both apply on the next request.

**The last admin is demoted like anyone else**, and the log says so at `ERROR` ("this
instance now has no admin"). Refusing would keep admin for exactly the person whose role was
just revoked, when they are the only admin — the case the feature exists for. An instance
with no admin is recoverable three ways, none needing a restart:

1. grant the role at the provider and log in;
2. set `XHC_BOOTSTRAP_ADMIN` (below) to that person's subject and log in as them;
3. `python -m app.authzctl grant-admin SUBJECT` — applies to that user's existing session
   on its next request, and lasts until their next login recomputes it.

**`XHC_BOOTSTRAP_ADMIN` is the break-glass path, and outranks the claim.** Precedence at
each login in this mode:

| | admin? |
|---|---|
| `XHC_BOOTSTRAP_ADMIN` equals this login's **subject** | yes, whatever the claim says |
| the claim carries `XHC_OIDC_ADMIN_VALUE` | yes |
| otherwise | **no**, even if they were admin before |

So in this mode it is a **standing** grant, evaluated at every login rather than only when
the principal is first created, and each login it grants logs a warning saying so. That is
what lets it rescue an instance whose claim mapping is broken at the provider; it is also why
it belongs **unset outside an emergency**.

**In this mode it matches the subject only, never the email.** A grant re-applied at every
login must not key on an address most providers let users edit. A value containing `@` logs
a loud warning at startup, because an email there matches nobody; it is not refused, because
some providers issue subjects that contain `@`. Use the person's subject, which
`python -m app.authzctl list` and the console's user list both show. Outside
this mode nothing changes: subject or email, at first creation only.

Workload tokens (`XHC_JWT_ISSUERS`) are unaffected: they authenticate `/v2` and the Hugging
Face surface, never the console, so no JWT is ever an admin.

#### The console at `/console`

`examples/web-root/` ships two pages: the homepage at `/`, and the key-management
console at **`/console`**. Point `XHC_WEB_ROOT` at that directory and both are served.

The console is a separate page on purpose. It began as a section at the bottom of the
homepage, and the report that moved it was *"I have to scroll down a mile to find it"* —
a management surface under four sections of marketing copy is somewhere nobody looks.

| | |
| --- | --- |
| any signed-in user | create, disable and delete **their own** keys; see their allowlist |
| admin | all users, each with an editable allowlist, admin toggle (absent when [the provider decides admin](#admin-from-the-identity-provider-xhc_oidc_admin_claim)), disable, and delete |

Creating a key shows the `docker login` line and the `HF_ENDPOINT`/`HF_TOKEN` pair with
the values filled in. Deleting a user takes their keys and allowlist with them, and is
refused for the last administrator and for yourself.

The page is a client of **`/_console`**, which is the JSON API behind it —
`/_console/keys` for a user's own credentials and `/_console/users` for administration,
both authorised by the session cookie rather than by a cache key. It is named here
because it exists, not because you are expected to call it directly; the console page is
the supported way in, and `/_auth` is the login surface it depends on.

Both pages are self-contained — no framework, no CDN, no external request of any kind —
and every user-written string is inserted with `textContent`, never `innerHTML`.

A key secret is shown **once**, at creation. The store keeps only a SHA-256 hash, which is
what makes a copy of the database less than a full compromise.

Sessions are a signed cookie (`HttpOnly`, `Secure`, `SameSite=Lax`) carrying nothing but the
subject and an expiry. **No role, no key, no rule** — everything is re-read from the store on
every request, so disabling a user or revoking admin takes effect on their next request
rather than whenever their cookie happens to expire. (A revocation made *at the identity
provider* reaches the store at that user's next login; see
[above](#admin-from-the-identity-provider-xhc_oidc_admin_claim).)

The login is deliberately **not** a second gate on `/v2`. Pulls and pushes authenticate with
a key, every time; a browser session never authorises one. Keeping the two credential kinds
disjoint means a stolen cookie cannot pull images and a leaked key cannot manage keys.

*Why application-level OIDC rather than an edge access product:* an access proxy can only
enforce on hostnames it terminates, and putting the cache's data path behind one imposes that
proxy's request-body limit on every blob push. A blob `PUT` over that limit returns 413 in a
way that leaves a tag pointing at the previous manifest — a push that reports failure after
having half-succeeded. The login is a browser concern; it does not need to sit in front of
the bytes.

### Headless provisioning (`/_cache/authz`, `python -m app.authzctl`)

For CI, clusters and anything else with **no identity provider**: create principals, set
their rules and mint keys without a browser. Two front ends over the same operations — an
HTTP API behind the manage token, and a CLI shipped in the image that works directly on the
database, so an init container can provision before the server starts.

> **Setting `XHC_MANAGE_TOKEN` together with `XHC_AUTHZ_DB` makes the manage token a
> key-minting credential.** Anyone holding it can create an administrator and mint keys for
> any principal. Handle it as a Secret — never in a compose file, an image, a command line or
> a CI log — and rotate it the way you would rotate a root password.

**Unconfigured means absent, not open.** Like every `/_cache` route, these return `404`
when `XHC_MANAGE_TOKEN` is unset or blank, whatever you send. They additionally return
`404` without `XHC_AUTHZ_DB`, because there is no store to provision. A wrong or missing
token is `401`.

#### Endpoints

All take and return JSON, authorised by `Authorization: Bearer $XHC_MANAGE_TOKEN`.

| method | path | purpose |
|---|---|---|
| `GET` | `/_cache/authz/principals` | principals with their rules and key count |
| `POST` | `/_cache/authz/principals` | `{"subject": "svc:ci", "email": "", "is_admin": false}` → `201`; `409` if it exists |
| `DELETE` | `/_cache/authz/principals/{subject}` | delete a principal **and its keys**; `409` for the last admin |
| `PUT` | `/_cache/authz/principals/{subject}/rules` | replace the grant: `{"rules": ["docker.io/library/* pull"]}`; `400` names a bad line |
| `POST` | `/_cache/authz/principals/{subject}/keys` | mint: `{"label": "ci", "scope": ["docker.io/library/alpine pull"]}` → `201` with `key_id` and `secret` |
| `GET` | `/_cache/authz/keys?principal=` | keys, all or one principal's — never a secret or its hash |
| `POST` | `/_cache/authz/keys/{key_id}/disabled` | `{"disabled": true}` or `false` |
| `DELETE` | `/_cache/authz/keys/{key_id}` | delete one key |

An unknown principal or key is `404` on every route — disabling a mistyped key id is an
error, not a `200` that revoked nothing.

- **`is_admin` is `false` unless you send the literal boolean `true`.** Provisioning never
  goes through the first-login-becomes-admin path, so a machine account created on an empty
  store is not an admin. Unknown fields are refused (`422`), so a misspelt `is_admin` cannot
  silently produce the wrong kind of principal.
- **Rules use the console's syntax**, one per item: `<pattern> pull|push|pull+push`, the
  verb defaulting to `pull`. A line that does not parse is refused with `400` naming it, and
  nothing is written. An empty list is accepted and **grants nothing**.
- **A scope narrows one key** to part of its holder's rules. `*` in a scope means no limit,
  as in the console.
- **The server generates the secret**, with the same generator as the console, and returns
  it **once**, with `Cache-Control: no-store`. Only its SHA-256 is stored; nothing can
  retrieve it again. There is no way to supply your own.
- **Subjects** must not contain `/`, control characters or surrounding whitespace, and are
  refused rather than trimmed. They share a namespace with OIDC subjects, so use a prefix
  such as `svc:` that no provider will issue.
- **A provisioned principal makes the store non-empty**, so if you later enable
  `XHC_OIDC_ISSUER`, the first person to log in is *not* made admin. Set
  `XHC_BOOTSTRAP_ADMIN` to their subject or email for that.

#### The CLI

```bash
python -m app.authzctl --db /srv/authz/authz.db <command>   # or set XHC_AUTHZ_DB
```

| command | |
|---|---|
| `create-principal SUBJECT [--email E] [--admin] [--exist-ok]` | `--exist-ok` succeeds on a re-run, but fails if the existing principal's admin flag differs |
| `set-rules SUBJECT RULE...` | one rule per argument; setting none needs `--empty` |
| `mint SUBJECT [--label L] [--scope RULE]... [--secret-file PATH]` | the only command that prints a secret |
| `list` | principals and keys as JSON, never secrets |
| `disable-key` / `enable-key` / `delete-key KEY_ID` | |
| `delete-principal SUBJECT` | |
| `grant-admin` / `revoke-admin SUBJECT` | the recovery path for an instance with no admin; revoking the last admin is refused, and an unknown subject is an error |

`--secret-file PATH` writes the secret to a **new** file created `0600` and prints only the
key id. It refuses an existing file (`--overwrite` replaces it) and is created *before*
minting, so an unwritable path leaves no orphaned key. `--secret-file-format token` writes
`<key_id>:<secret>`, the form `HF_TOKEN` takes; the default writes the secret alone, which
is the docker password.

It runs safely beside a live server. The server checks SQLite's own change counter on every
authentication, so a key minted by the CLI authenticates on the very next request and a key
disabled by it is refused on the very next request — no restart, no signal.

`mint` is not idempotent: every run creates another key. Mint from a one-shot job, not from
an init container that re-runs on every pod start, or delete the previous key when you
replace it.

#### Worked example

Over HTTP:

```bash
H="Authorization: Bearer $XHC_MANAGE_TOKEN"
curl -fsS -H "$H" -X POST https://cache.example.com/_cache/authz/principals \
     -d '{"subject": "svc:ci"}' -H 'content-type: application/json'
curl -fsS -H "$H" -X PUT https://cache.example.com/_cache/authz/principals/svc:ci/rules \
     -d '{"rules": ["docker.io/library/* pull", "ghcr.io/myorg/* pull+push",
                    "models/myorg/* pull"]}' \
     -H 'content-type: application/json'
curl -fsS -H "$H" -X POST https://cache.example.com/_cache/authz/principals/svc:ci/keys \
     -d '{"label": "ci runner"}' -H 'content-type: application/json'
# -> {"key_id": "3f2a…", "secret": "Qm9…", …}   shown once
```

Or the same from an init container, straight into a file a Secret can be built from:

```bash
python -m app.authzctl create-principal svc:ci --exist-ok
python -m app.authzctl set-rules svc:ci 'docker.io/library/* pull' 'ghcr.io/myorg/* pull+push' \
    'models/myorg/* pull'
python -m app.authzctl mint svc:ci --label 'ci runner' \
    --secret-file /secrets/muninn-token --secret-file-format token
```

Then use the key on both surfaces:

```bash
echo "$SECRET" | docker login cache.example.com -u "$KEY_ID" --password-stdin
docker pull cache.example.com/docker.io/library/alpine:3.20

export HF_ENDPOINT=https://cache.example.com
export HF_TOKEN="$KEY_ID:$SECRET"          # with XHC_HF_AUTH=key
hf download myorg/llama-ft                 # allowed by 'models/myorg/* pull'
hf download otherorg/model                 # 403, GatedRepoError naming the key and repo
```

**Rules are enforced on both surfaces.** With `XHC_HF_AUTH=key` and the default
`XHC_HF_RULES=enforce`, this key pulls `myorg`'s models and nothing else from Hugging Face,
on a cache hit as on a miss. `models/…` patterns are pull-only; see
*Rules on the Hugging Face surface* above.

### Workload identity (JWT)

Pods, CI jobs and client-credentials services can authenticate with a **short-lived token
their platform already issues**, instead of a static key: a Kubernetes projected
service-account token, a Keycloak (or any OIDC provider's) `client_credentials` token, a
GitHub Actions OIDC token. The token authenticates as a **principal** in `XHC_AUTHZ_DB`, and
that principal's rules apply exactly as they do for a key — same rule syntax, same
enforcement on `/v2` and on the Hugging Face surface, on hits as on misses.

**Off unless `XHC_JWT_ISSUERS` is set.** It requires `XHC_AUTHZ_DB` and refuses to start
without it.

#### Configuration

`XHC_JWT_ISSUERS` is JSON: one object, or a list of them. One environment variable rather
than a file, because the declaration holds no secret and a container, a compose file and a
Kubernetes `env:` (or `valueFrom: configMapKeyRef`) all set it the same way.

```json
[{"issuer": "https://kubernetes.default.svc.cluster.local",
  "audience": "muninn",
  "subject_template": "k8s:{sub}"},
 {"issuer": "https://token.actions.githubusercontent.com",
  "audience": "https://github.com/myorg",
  "subject_template": "gha:{sub}"}]
```

| key | | |
|---|---|---|
| `issuer` | required | compared **exactly** with the token's `iss`. It is the trust anchor: a discovery document declaring a different issuer is refused |
| `audience` | required | a string or a list. The token's `aud` must contain one of them. Required because the audience is what says a token was minted **for this cache** — without it, any token the issuer mints for anything would be accepted |
| `subject_template` | required | how a token names its principal. `{sub}` is the identity claim's value, `{iss}` the issuer. Must start with a literal prefix (or `{iss}`) and contain `{sub}` once |
| `subject_claim` | `sub` | which claim identifies the caller |
| `algorithms` | `RS256 RS384 RS512 PS256 PS384 PS512 ES256 ES384 ES512` | allowlist; `EdDSA` may be added. `none` and every `HS*` algorithm are refused at startup |
| `jwks_uri` | from discovery | `https://…`, or `file:///absolute/path` for a mounted key set |
| `ca_file` | system CAs | CA bundle for fetching discovery and keys, e.g. a cluster's `ca.crt` |
| `fetch_token_file` | — | a file whose contents are sent as `Bearer` when fetching discovery and keys; re-read on every fetch |
| `auto_create` | `false` | create an unknown principal on first sight, with **no rules** |
| `leeway_s` | `30` | clock skew allowed on `exp`, `nbf` and `iat`; at most `300` |
| `fetch_timeout_s` | `5` | per fetch of discovery or keys |

Anything else in an issuer object — a misspelt key included — refuses startup with a message
naming the issuer and the key. So does a missing audience, `none` or `HS256` in
`algorithms`, two issuers with the same `issuer`, and two templates where one's literal
prefix is a prefix of the other's (`k8s:{sub}` and `k8s:x{sub}`), because then a token from
one issuer could name a principal of the other.

**HMAC (`HS256` and family) is not supported at all.** An HMAC issuer shares its signing
secret with every verifier, so this cache would hold a secret able to mint that issuer's
tokens; and the classic algorithm-confusion attack — a token signed `HS256` with the RSA
*public* key as the secret — exists only because a verifier accepted both families. Every
issuer this is for signs asymmetrically.

**Subjects are encoded.** Principal subjects cannot hold `/`, and a GitHub Actions `sub` is
`repo:myorg/app:ref:refs/heads/main`. `%`, `/` and control characters in the claim are
percent-encoded, which cannot map two different values to one subject:

| issuer's claim | `subject_template` | principal |
|---|---|---|
| `system:serviceaccount:ml:trainer` | `k8s:{sub}` | `k8s:system:serviceaccount:ml:trainer` |
| `repo:myorg/app:ref:refs/heads/main` | `gha:{sub}` | `gha:repo:myorg%2Fapp:ref:refs%2Fheads%2Fmain` |

Pick a prefix no other principal uses. A template of `svc:{sub}` lets a token whose `sub` is
`ci` authenticate as a principal `svc:ci` created for a key.

#### Where a token is accepted

| surface | how | username |
|---|---|---|
| Hugging Face (with `XHC_HF_AUTH=key`) | `Authorization: Bearer <jwt>`, which is what `huggingface_hub` sends for `HF_TOKEN` | — |
| `/v2` | the Basic **password**, which is all docker and containerd send, or `Authorization: Bearer <jwt>` | **ignored**; use `jwt` by convention |
| `/_cache/*` | **never** | — |

The username is ignored because the token carries the identity; a username that could
disagree with it would be a second, weaker claim about who is asking.

**The credential's shape decides which verifier runs, once.** A JWT is three base64url
segments separated by `.`; a key is `<key_id>:<secret>` and its secret never contains a
`.`. A token that fails is never retried as a key, and a key never as a token, so the reason
a credential was refused is always the reason of the one verifier that owns it. With
`XHC_JWT_ISSUERS` unset, nothing is parsed differently from before.

**Not on `/_cache`**, which stays on `XHC_MANAGE_TOKEN` alone. That surface pauses
eviction, deletes repositories, and — with `XHC_AUTHZ_DB` — mints keys and creates
administrators. Accepting a workload token there would make every pod whose service
account an administrator ever granted a rule into a candidate operator of the cache, and
would turn an issuer compromise into a key-minting capability. Management is rare and
deliberate; it keeps one credential with one meaning.

**What a refusal says.** A refused token is `401` with the general reason in the body and in
`X-XHC-Auth-Error` — `token expired`, `wrong audience`, `unknown issuer`, `unknown signing
key`, `bad signature`, `algorithm not allowed`, `issuer keys unavailable`, `unknown
principal`, `principal disabled`. On the Hugging Face surface it is also in
`X-Error-Message`, which `huggingface_hub` prints. The token is never echoed; the specific
reason (which audience, which kid) is in Muninn's log. The docker CLI prints only the
status.

**An unknown principal is `401`, not `403`.** A genuine token for nobody this cache knows is
the same situation as an unknown key id — the credential resolves to no identity — and a
`403` would make `docker login` report success for a principal that does not exist. With
`auto_create`, the principal is created (never admin) with **no rules**: it authenticates,
`docker login` succeeds, and every pull is `403` until an administrator grants it
something, at which point the same token pulls with no restart. Note that an auto-created
principal makes the store non-empty, so set `XHC_BOOTSTRAP_ADMIN` if a human is meant to
become admin by logging in later.

**A disabled principal is refused**, on the very next request, however the change was made
— the console, `/_cache/authz`, or `authzctl` beside a running server.

**No key scope.** A key can be narrowed to part of its holder's grant; a token has no row to
hang a narrowing on, so it carries its principal's full rules. To give one workload less,
give it its own principal — which a per-service-account subject already does.

#### Verification

Signature by the issuer's key set, selected by `kid` (a token without one is refused); the
key's type must match the token's `alg`, which must be in the issuer's allowlist; `iss`
exact; `aud` must contain a configured audience; `exp` required; `nbf` and `iat` honoured
with `leeway_s`. Token headers that name their own key (`jku`, `x5u`, `jwk`) are ignored,
and a token with `crit` extensions is refused.

- **Keys** come from `jwks_uri`, or from the issuer's discovery document, which must
  declare exactly the configured issuer and an `https` `jwks_uri` — a document fetched over
  the network cannot point this cache at a file or at plaintext. They are cached, refreshed
  every 10 minutes, and refetched when a token names an unknown `kid` (which is how key
  rotation arrives). **Fetches are rate-limited to one per 30 seconds per issuer**, whatever
  prompted them, so a flood of tokens with invented `kid`s costs the issuer one request per
  window, not one per token; a new key is picked up by the first token that uses it after
  the window.
- **Issuer unreachable.** With keys already in hand, verification continues with them and
  the log says so — an issuer outage must not become a cache outage. With none — never
  fetched since start — every token from that issuer is refused (`issuer keys unavailable`),
  and the issuer is not retried more than once per window. Keys are fetched in the
  background at startup, so a cache that boots while the issuer is up rides out a later
  outage.
- **A `file://` key set** is read at startup — missing, unreadable or holding no usable key
  refuses startup — and re-read on refresh, so an updated ConfigMap is picked up without a
  restart.

**Performance.** A pod pulling a 400-file snapshot presents one token 400 times. The
verified *identity* is cached per token — keyed by its SHA-256, never the token itself — for
`XHC_JWT_CACHE_TTL` seconds (default 60) and **never past the token's own `exp`**. What the
principal may do is *not* cached with it: rules and the disabled flag are read on every
request from the same store snapshot keys use, refreshed on SQLite's change counter, so
revocation is immediate.

#### Kubernetes, worked

The service account's `sub` is `system:serviceaccount:<namespace>:<name>`, and the
principal keeps the colons literal: `k8s:system:serviceaccount:<namespace>:<name>`. Only
`%`, `/` and control characters are percent-encoded; a hand-encoded `%3A` matches no token.

**On GKE, use the cluster's public issuer.** It is
`https://container.googleapis.com/v1/projects/<project>/locations/<location>/clusters/<cluster>`,
serves its discovery document anonymously, and advertises a public `jwks_uri`, so discovery
works with no extra configuration. The in-cluster `/.well-known/openid-configuration`
reached through the API server advertises a *private* `jwks_uri` (the control plane's
internal address), so do not copy what `kubectl get --raw` shows there. Confirmed on a real
GKE cluster: a projected token with the right audience pulled what its principal allows (200),
was refused outside its rules (403), and a tampered signature, the pod's default token (wrong
audience) and a service account with no principal were each refused (401).

Elsewhere, find the cluster's issuer — it is what goes in `issuer`, byte for byte:

```bash
kubectl get --raw /.well-known/openid-configuration | jq -r .issuer
# e.g. https://kubernetes.default.svc.cluster.local
```

**Muninn** — trusting the cluster, and fetching its keys through the in-cluster API server
with its own service account:

```yaml
env:
  - name: XHC_AUTHZ_DB
    value: /srv/authz/authz.db
  - name: XHC_HF_AUTH
    value: key
  - name: XHC_JWT_ISSUERS
    value: >-
      {"issuer": "https://kubernetes.default.svc.cluster.local",
       "audience": "muninn",
       "subject_template": "k8s:{sub}",
       "jwks_uri": "https://kubernetes.default.svc/openid/v1/jwks",
       "ca_file": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
       "fetch_token_file": "/var/run/secrets/kubernetes.io/serviceaccount/token"}
```

Three ways to get the cluster's keys, depending on what the cluster allows. *None has been
exercised against a live cluster by this project's tests*, which use a fake issuer; check
yours with the `kubectl` commands shown.

1. **Through the API server, authenticated** (above). The discovery and JWKS endpoints are
   readable by the `system:service-account-issuer-discovery` ClusterRole, which a default
   cluster binds to all service accounts — so Muninn's own token works. `jwks_uri` is set
   explicitly because the discovery document's `jwks_uri` names the API server's
   *advertised* address, which is often not reachable from a pod.
2. **Anonymously**, if the cluster binds that role to `system:unauthenticated` (some managed
   clusters also publish discovery at a public URL). Drop `fetch_token_file`, and `ca_file`
   too if the URL has a public certificate.
3. **From a file**, for a Muninn that cannot reach the API server at all (outside the
   cluster, or a locked-down network). Export the key set and mount it:

   ```bash
   kubectl get --raw /openid/v1/jwks > jwks.json
   kubectl create configmap cluster-jwks --from-file=jwks.json
   ```

   and set `"jwks_uri": "file:///etc/muninn/jwks/jwks.json"`. The file is re-read on
   refresh; you must update it when the cluster's service-account signing key rotates, or
   new tokens are refused as `unknown signing key`.

**The workload** — a projected token with audience `muninn`, handed to `huggingface_hub`:

```yaml
spec:
  serviceAccountName: trainer            # in namespace ml
  containers:
    - name: train
      env:
        - name: HF_ENDPOINT
          value: https://cache.example.com
        - name: HF_TOKEN_PATH            # and do NOT set HF_TOKEN: it takes priority
          value: /var/run/secrets/muninn/token
      volumeMounts:
        - name: muninn-token
          mountPath: /var/run/secrets/muninn
          readOnly: true
  volumes:
    - name: muninn-token
      projected:
        sources:
          - serviceAccountToken:
              path: token
              audience: muninn
              expirationSeconds: 3600
```

**The principal and its rules**, from an init container or a shell beside the server:

```bash
python -m app.authzctl create-principal k8s:system:serviceaccount:ml:trainer --exist-ok
python -m app.authzctl set-rules k8s:system:serviceaccount:ml:trainer \
    'models/myorg/* pull' 'docker.io/library/* pull'
```

No `mint`: there is no key. The pod's token is the credential, and the kubelet rotates it.

**Token rotation and `huggingface_hub`.** The kubelet refreshes a projected token once it is
80% through its lifetime (or 24 hours old), replacing the file. `huggingface_hub` 0.34.4
**re-reads the token file on every request** rather than caching it for the process:

- `constants.py:178` resolves `HF_TOKEN_PATH` once, at import — the *path*, not its contents;
- `utils/_auth.py:121-123`, `_get_token_from_file()`, does `Path(constants.HF_TOKEN_PATH).read_text()`
  on every call, with no cache;
- `utils/_headers.py:154`, `get_token_to_send()`, calls `get_token()` whenever no token was
  passed explicitly, and `file_download.py:972` (`hf_hub_download`) builds its headers per
  call — `snapshot_download` calls it per file.

So a long-running process picks up the rotated token on its next file. Three ways to defeat
that, all avoidable:

- **`HF_TOKEN` in the environment wins over the file** (`utils/_auth.py:49`: Colab, then
  the environment, then the file). Setting `HF_TOKEN=$(cat …)` at process start freezes the
  token for the process lifetime; after `expirationSeconds` every request is `401 token
  expired`. Use `HF_TOKEN_PATH`.
- **Passing a token explicitly** — `token="…"`, `HfApi(token=…)` — pins that string. Leave it
  unset, or pass `token=True`, which still reads the file each call.
- **One download in flight keeps the headers it started with.** Muninn authenticates at the
  start of each request, so a multi-gigabyte transfer that outlives the token completes;
  the next file uses the new one.

**Image pulls.** `docker login` stores whatever password it is given, so a token used there
expires with the token:

```bash
docker login cache.example.com -u jwt --password-stdin < /var/run/secrets/muninn/token
```

That suits a CI job that logs in per run. **The kubelet does not present a pod's projected
token when pulling that pod's images** — it uses `imagePullSecrets` or a credential provider
— so node-level image pulls through this cache still need a key in an `imagePullSecret`.

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

## Object-store tier (`XHC_TIER2`)

> ⚠ **THE TIER GROWS WITHOUT BOUND. MUNINN NEVER DELETES FROM IT.** Nothing in
> Muninn deletes an object from the bucket: not eviction, not a mismatch, not
> an orphan sweep. How long to keep what is there is **your cost decision**.
> Muninn does not default one, recommend one or ship one. Its size is on
> `muninn_tier_objects` and `muninn_tier_bytes`, measured by LIST at each
> reconcile.

An optional S3-compatible bucket between the local disk and the upstream. Off
unless `XHC_TIER2` is set; unset, every tier code path is skipped.

- **Read-through.** On a local miss, Muninn reads the bucket **before** the Hub
  or the registry. That covers OCI blobs, OCI manifests requested by digest, and
  Hugging Face files whose ETag is a sha256 (LFS and Xet files, which is every
  weight file). A fresh disk refills from the bucket, and the upstream is spared
  the request. The Hub is still asked for metadata (the `HEAD` on every miss),
  because that is where the expected hash comes from.
- **Write-back.** After an ingest reaches `done` (verified, when
  `XHC_HF_VERIFY` is on), and after an OCI blob's digest has matched and it is
  renamed into place, the content is copied to the bucket in the background.
  Nothing is ever uploaded from `verifying`, from `error`, or from a partial
  file. Uploads never block serving and never hold an ingest slot.
- **Prewarms read it too**, and a re-prewarm is how a lost disk is refilled.
  A prewarm lists the revision (as it already does to judge completeness),
  fills every sha256 file it names from the bucket, and then runs
  `snapshot_download` as before. That only links what landed, and fetches the
  rest from the Hub: small git-blob files, tier misses, and anything that
  failed verification. Tier fills use the same verify-first path as a file
  miss, under `huggingface_hub`'s per-blob lock, and at most
  `XHC_SNAPSHOT_MAX_WORKERS` of them run at once. Files verified as they
  arrived from the tier are counted in the job's `verify.verified_at_tier_read`
  and are not hashed a second time.
- **Fails open.** The tier is an accelerator and an archive, never the
  authority. An outage, a 5xx, a 401 or a verification failure falls through to
  the upstream. A 401 also disables the tier until the next probe (every 60 s).

What goes in the bucket, under your prefix. The retention class (`content`,
`index`) comes **before** the tenant, so a prefix-only lifecycle rule (B2 has no
other kind) can target one without the other:

```
<prefix>/v1/content/hf/<hf-host>/<repo_type>s/<org>/<name>/sha256/<etag>
<prefix>/v1/content/oci/<upstream>/blobs/sha256/<ab>/<hex>
<prefix>/v1/content/oci/<upstream>/manifests/sha256/<ab>/<hex>      verbatim bytes; media type as Content-Type
<prefix>/v1/index/hf/<hf-host>/<repo_type>s/<org>/<name>/commits/<commit>/<quoted-path>.json   {etag,size,sig}
<prefix>/v1/index/hf/<hf-host>/<repo_type>s/<org>/<name>/refs/<quoted-ref>/<observed_at>-<commit>
<prefix>/v1/index/oci/<upstream>/tags/<repo>/<tag>/<observed_at>-<digest>
<prefix>/v1/_probe/<hostname>
```

Hugging Face content is keyed per repo, so one repo can be deleted by deleting
one prefix. OCI content is keyed per upstream, because layers are shared
heavily across repos.

### Trust: content versus mappings

- **Content is verified on every read from the tier, unconditionally.** A blob
  is named by its own hash, and the value it is checked against comes from the
  request: the Hub's `HEAD`, or the digest in the URL. It never comes from the
  bucket. So a bucket can withhold content but cannot forge it. This does not
  depend on `XHC_HF_VERIFY`, which is a choice about bytes from the Hub.
- **Mappings are not content.** A revision → commit → file → ETag chain, or a
  tag → digest, is only as trustworthy as whoever can write the bucket, and on
  R2 a token scopes to a whole bucket, never to a prefix. So index objects are
  signed with `XHC_TIER2_INDEX_KEY`, an HMAC key from **your configuration**,
  never stored in the bucket. The signature also covers the observation time,
  so an old signed observation cannot be replayed under a newer name.
- **The index is written with or without the key.** Without it, each entry is
  written **unsigned** and says so in the object itself (`"auth": "unsigned"`
  in a commit entry's body, `auth: unsigned` metadata on a ref or tag entry).
  Nothing reads the index yet, so an unsigned entry costs no trust today.
  **Whether a later restore will use unsigned entries is not decided.** An
  unsigned index can be restored from only if that policy allows it. Entries
  are immutable and never re-signed, so **set the key from the start** if you
  want everything written to stay restorable under a signed-only policy.
- **On a mismatch** the bytes are discarded and nothing is linked;
  `muninn_tier_verify_total{result="mismatch"}` counts it and the key is logged
  and listed under `tier.bad_keys` on `/_cache/status`. That key is not read
  again by this process, so the request (or its retry) goes to the upstream.
  The object is **not** deleted from the bucket. Go and look at it.

### Read modes: `verify-first` (default) and `stream`

A Hugging Face client in cache mode does not hash what it receives. A wrong
object of the right length, streamed to it, would be accepted in full, because
the mismatch is only knowable after the last byte.

- **`verify-first`** hashes the bytes as they land, in a file no reader follows,
  and renames them into place only on a match. Clients wait for that, as they
  do under `XHC_MISS_POLICY=wait`. Their first byte arrives after the object's
  last. The response says `x-xhc-cache: TIER-HIT`. If the tier's bytes failed
  verification and the job fell through to the upstream, it says `MISS-WAIT`
  (Hugging Face) or `MISS` (OCI).
- **`stream`** serves tier bytes as they arrive, **before they are verified**,
  and says `x-xhc-cache: TIER-STREAM`. A mismatch ends the ingest in error, and
  a client that has already read every byte keeps them. Defensible for OCI,
  whose clients check the digest themselves. Not defensible for Hugging Face.

There is **one hash pass** in each direction. A tier read never fetches and then
re-reads the file to hash it. A tier-verified Hugging Face blob is not hashed
again by `XHC_HF_VERIFY`, because that would be a second full read of a file
already checked. A write-back reads each byte of the local file once. It hashes
that byte in the same pass and sends it.

### Write-back details

- **Every upload re-verifies what it sends.** If the local bytes no longer hash
  to their name (disk rot, a stray write), the upload is refused and counted as
  `verify_mismatch`. So a blob reaches the tier only if its bytes hash to its
  name, whatever `XHC_HF_VERIFY` is set to.
- **Single PUT** (object ≤ `XHC_TIER2_PART_SIZE`). The file is read once into
  memory and hashed once. The name is sent as SigV4's `x-amz-content-sha256`,
  so a store that checks it refuses a body that does not match. The
  `x-amz-checksum-sha256` header is also sent when `XHC_TIER2_CHECKSUM_HEADER`
  is on.
- **Multipart** (larger objects). Each part is read once, folded into the
  whole-object hash, and sent from memory. A part that fails in transit is
  re-sent from that buffer rather than re-read. `CompleteMultipartUpload` is
  called only after the whole-object hash has matched. On a mismatch or failure
  the upload is aborted and no object appears. Memory per upload is one part.
- **HEAD before PUT.** An object already in the bucket, from another instance
  or an earlier run, is skipped (`skipped_exists`).
- **The queue is in memory, and the bucket is the durable record.** A
  reconciler runs at startup and every `XHC_TIER2_RECONCILE_INTERVAL`. It LISTs
  the content prefix, compares it with what is on local disk, and enqueues the
  difference. With an index key it also backfills the index for local
  snapshots and refs. A restart therefore loses nothing but time. Overflow past
  `XHC_TIER2_QUEUE_MAX` is dropped, counted as `dropped_queue_full`, and picked
  up by the next reconcile.
- **Evicted before upload:** the item is dropped (`skipped_evicted`). Eviction
  never waits for uploads, and local eviction and GC never touch the tier.
- **Add an abort-incomplete-multipart lifecycle rule.** An interrupted
  multipart upload leaves parts that are billed. That rule is **not** a
  retention rule and deletes no object.

### Retention: read this before adding a lifecycle rule

1. **The tier grows without bound.** Muninn never deletes from it.
2. Its size is `muninn_tier_objects` / `muninn_tier_bytes` (LIST-derived, at
   each reconcile).
3. **A lifecycle rule on `v1/content/` expires by upload age, not by use.**
   Write-back skips objects that already exist, so a model pulled every day
   ages out exactly like one never read again.
4. A rule whose prefix covers `v1/index/` removes the mappings, and the content
   they point to then cannot be restored once the upstream has deleted it.

### What phase 1 does, and does not do

| does | does not |
|---|---|
| read-through for OCI blobs, OCI manifests by digest, and HF files with a sha256 ETag | serve anything from the tier when the **upstream is unreachable for metadata**. A Hugging Face miss still needs the Hub's `HEAD`, and a tag still needs the registry |
| write-back after `done`, with the upload-time hash | read or restore from the **index**, which is written but never read. A model deleted upstream does **not** survive through the tier yet |
| write the index: signed with `XHC_TIER2_INDEX_KEY`, marked unsigned without it | tier small, non-LFS Hugging Face files (`config.json`, tokenizers), which are keyed by git blob id. A model restored without its `config.json` is not a model, so that is the next phase's first job |
| verify every tier read | |
| static keys, and a GKE metadata-server token for GCS | AWS role credentials (IRSA, EKS Pod Identity, instance profiles) |
| | parallel ranged reads from the tier: one stream per object |
| | delete anything from the tier, ever |

Nothing here is a claim about speed. Whether the tier is faster than the Hub for
your bucket, region and object sizes has not been measured. Measure a
single-stream GET from your bucket against a Hub fetch before relying on it.
It pays for itself mainly as survival (in a later phase) and as relief from
upstream rate limits. Cross-region or cross-cloud buckets also pay egress per
byte.

### Stores and credentials

The client is a small SigV4 client on `httpx`, with no new dependency. It
**never makes a bucket-level call** (no CreateBucket, HeadBucket or
ListBuckets). A token scoped to one bucket fails those by design. Required
permissions under the prefix: get, put, list (`s3:ListBucket`), and the
multipart calls, including abort. It needs no delete permission.

- **Static keys** (`XHC_TIER2_CREDENTIALS=static`, the default for `s3://`):
  AWS, Cloudflare R2, MinIO, Backblaze B2, and GCS through HMAC interop keys.
  Keys come from `XHC_TIER2_ACCESS_KEY_ID` / `XHC_TIER2_SECRET_ACCESS_KEY`, or
  from `XHC_TIER2_ACCESS_KEY_ID_FILE` / `XHC_TIER2_SECRET_ACCESS_KEY_FILE` for a
  mounted Kubernetes Secret. Setting both forms of one is refused. **The
  standard `AWS_*` variables are never read**, so an ambient credential meant
  for something else cannot silently become the tier's.
- **GKE Workload Identity** (`XHC_TIER2_CREDENTIALS=gcp-metadata`, the default
  for `gs://`): a bearer token from the metadata server, refreshed before it
  expires, against the GCS XML API at `storage.googleapis.com`.

**The startup probe** PUTs and GETs `v1/_probe/<hostname>` and compares the
bytes. It then GETs a key that must be absent and **refuses to call the tier
healthy unless that returns 404**. Without list permission S3 answers 403 for a
missing key, and every tier miss would then read as an auth failure. The probe
runs in the background, so a bucket that is down at boot delays nothing. The
result is in the `tier` block of `/_cache/status`, which also shows the last
error, the queue depth, the keys marked bad and the last reconcile.

**What has been tested against real servers:**

- **MinIO** (`pytest -m minio`, and a CI job): SigV4, the payload hash,
  `x-amz-checksum-sha256`, multipart, ListObjectsV2 pagination and the probe's 404.
- **GCS, in a real deployment** (regional bucket, uniform access, workload
  identity through the GKE metadata server, `XHC_TIER2_CHECKSUM_HEADER` at its
  `gs://` default of `false`): the XML API accepted the metadata bearer token; the
  probe passed, including 404 for a missing key; ListObjectsV2 was parsed in full
  (GCS answers in its own XML namespace, `http://doc.s3.amazonaws.com/2006-03-01`;
  elements are matched by local name, and a listing that declares keys and yields
  none is refused rather than read as an empty bucket), observed as the reconcile
  counting every content object with nothing re-enqueued; multipart uploads completed,
  including a single 49.9 GB file, 181 GB in all with no errors; and a re-prewarm after dropping the local copy filled
  every sha256 file from the tier, verified as it arrived, at about 3.6× that
  day's Hub rate on a small node. That is one bucket in one region, not a
  guarantee about GCS in general.

**Not yet verified on the real services:**


- `x-amz-checksum-sha256` on GCS (off by default there) and on R2. If the probe
  reports a 400 on its PUT, set `XHC_TIER2_CHECKSUM_HEADER=false`.
- Anything on R2, B2 or AWS, including R2's single-PUT size limit and its
  403-versus-404 for a missing key. The probe checks the last against your bucket
  at every start, so a wrong assumption shows up as an unhealthy tier and not as
  silent misbehaviour.

**`tier_objects` / `tier_bytes` in `/_cache/status`** count **content** objects only
(not index entries or the probe object), and come from a bucket listing at
each reconcile (startup, then every `XHC_TIER2_RECONCILE_INTERVAL`), plus the
objects this process has uploaded since (`uploaded_since_listing`). They are not a
fresh listing on every read.

### Several instances, one bucket

Supported, with the same `XHC_TIER2` prefix. Content is immutable and
content-addressed, so concurrent PUTs write identical bytes. Index objects are
immutable (commits) or append-only (one object per ref or tag observation), so
nothing needs a lock. The cost of sharing: one instance's credential can write
mappings that every instance would trust in a later phase. It cannot poison
content. That is why the index is signed with a key the bucket does not hold.

### Tier configuration

| variable | default | meaning |
|---|---|---|
| `XHC_TIER2` | *(unset: off)* | `s3://bucket/prefix` or `gs://bucket/prefix` |
| `XHC_TIER2_ENDPOINT` | derived | `scheme://host[:port]` for R2 (`https://<account>.r2.cloudflarestorage.com`), MinIO, B2 and similar. Path-style addressing when set. Unset: AWS (virtual-hosted) for `s3://`, `https://storage.googleapis.com` for `gs://` |
| `XHC_TIER2_REGION` | `auto` | SigV4 signing region. With the AWS default endpoint, `auto` signs as `us-east-1`; set the bucket's region for AWS |
| `XHC_TIER2_CREDENTIALS` | `static` (s3), `gcp-metadata` (gs) | `static` \| `gcp-metadata` |
| `XHC_TIER2_ACCESS_KEY_ID[_FILE]` | — | static key id, or a file holding it |
| `XHC_TIER2_SECRET_ACCESS_KEY[_FILE]` | — | static secret, or a file holding it |
| `XHC_TIER2_READ` / `XHC_TIER2_WRITE` | `true` / `true` | a read-only replica, or a write-only seeding instance |
| `XHC_TIER2_READ_MODE` | `verify-first` | `verify-first` \| `stream`. `stream` serves unverified bytes; see above |
| `XHC_TIER2_MIN_SIZE` | `0` | skip the tier for smaller Hugging Face files, and don't write back OCI blobs smaller than this. OCI blob reads cannot apply it, because their size is unknown before the request. Manifests are exempt |
| `XHC_TIER2_PART_SIZE` | `64M` | single PUT up to this size, multipart above it. Minimum `5M` (S3's floor). Also the memory one upload holds |
| `XHC_TIER2_UPLOAD_CONCURRENCY` | `2` | concurrent uploads |
| `XHC_TIER2_QUEUE_MAX` | `10000` | in-memory upload queue bound; the reconciler covers overflow |
| `XHC_TIER2_RECONCILE_INTERVAL` | `21600` | seconds between reconciles (and one at startup); `0` disables, and the queue is then best-effort |
| `XHC_TIER2_INDEX_KEY[_FILE]` | *(unset)* | HMAC key for index objects. Unset: the index is still written, **unsigned**, and marked so; see the trust section |
| `XHC_TIER2_CHECKSUM_HEADER` | `true` (s3), `false` (gs) | also send `x-amz-checksum-sha256` on single PUTs |

Tier metrics: `muninn_tier_requests_total{proto,kind,result=hit|miss|error|refused}`,
`muninn_tier_verify_total{result=verified|mismatch}`,
`muninn_tier_upload_total{result=ok|failed|skipped_exists|skipped_evicted|verify_mismatch|dropped_queue_full}`,
`muninn_tier_index_writes_total{result=signed|unsigned|failed|skipped_exists}`,
`muninn_tier_bytes_read_total` and `muninn_tier_bytes_written_total` (body
bytes only; a HEAD is never counted), and the gauges `muninn_tier_healthy`,
`muninn_tier_upload_queue_depth`, `muninn_tier_objects`, `muninn_tier_bytes`
and `muninn_tier_last_reconcile_timestamp`. Tier bytes are never added to
`muninn_bytes_ingested_total` or the docker ingest counter.

## Management API

> **Deleting a tag frees exactly its own layers.** Eviction is top-down: dropping the tag
> removes the root, and mark-and-sweep collects whatever became unreachable. A layer another
> tag still uses stays reachable and is never collected — which matters because container
> images share bases constantly, and an eviction that over-collected would break every other
> image on the node. A pinned image survives its tag being dropped, because a pin is a root
> in its own right.

All under `/_cache`, and **off unless `XHC_MANAGE_TOKEN` is set.** With it unset or blank,
every `/_cache` route — the read-only ones and `/_cache/docker/*` included — answers `404`
with the plain-text body `the management API is disabled (XHC_MANAGE_TOKEN is unset)`, and
the server logs one warning at startup saying so. With it set, every route requires
`Authorization: Bearer $XHC_MANAGE_TOKEN`; a missing or wrong token is `401`. `/healthz` and
`/metrics` are not part of this surface and do not depend on it (`/metrics` has its own
`XHC_METRICS_AUTH`).

> **Earlier releases left this API open** when `XHC_MANAGE_TOKEN` was unset, to
> anything that could reach the port. It now switches it off. Scripts that called
> `/_cache` without a token need one.

| method | path | purpose |
|---|---|---|
| `GET` | `/_cache/status` | disk, capacity, watermarks, active jobs, scan cost, **effective Xet env**, process `started_at` / `uptime_s` |
| `GET` | `/_cache/repos?refresh=true` | cached repos with size, file count, revisions, pin state, **`complete`** and present/expected counts |
| `POST` | `/_cache/prewarm` | ingest a repo ahead of a rollout |
| `GET` | `/_cache/jobs`, `/_cache/jobs/{id}` | ingest state, progress, elapsed, throughput, verification counts; survives a restart as `interrupted` |
| `GET`/`POST`/`DELETE` | `/_cache/pins` | pin management |
| `GET`/`DELETE` | `/_cache/orphans` | repos deleted upstream and retained |
| `POST` | `/_cache/orphans/check` | run an upstream liveness sweep now |
| `GET`/`PUT` | `/_cache/policy` | inspect or set what may be ingested |
| `DELETE` | `/_cache/viewer` | drop cached dataset metadata |
| `POST` | `/_cache/evict` | force an LRU sweep |
| `DELETE` | `/_cache/repos` | drop a repo, or one `revision` of it (409 if pinned) |
| various | `/_cache/authz/*` | principals, rules and keys — see *Headless provisioning*. Also needs `XHC_AUTHZ_DB` |

`GET /healthz` is the container healthcheck. It is not under `/_cache` and needs no token.

`/_cache/status` echoes the Xet variables the process actually sees. A silently
unset or wrong value there is the single most likely cause of a slow WAN ingest,
so check it first.

### Prewarming is the primary path

If you know your model set in advance — and with centralised model management
you do — edge nodes should only ever see cache hits.

```bash
curl -X POST localhost:8080/_cache/prewarm -H "Authorization: Bearer $XHC_MANAGE_TOKEN" \
  -H 'content-type: application/json' -d '{
  "repo_id": "meta-llama/Llama-3.1-70B-Instruct",
  "allow_patterns": ["*.safetensors", "*.json", "tokenizer*"],
  "pin": true
}'
```

`allow_patterns` matters on the Hub: many repos ship both `.safetensors` and
`.bin` copies of the same weights, and pulling both doubles your footprint for
nothing.

### Jobs, restarts, and whether a snapshot is complete

**Job states.** A job only moves forward:

```
pending -> running -> verifying -> done
                  \-------+------> error
pending | running | verifying  --(process restart)-->  interrupted
pending | running | verifying  --(graceful stop)---->  interrupted
```

`verifying` appears only with `XHC_HF_VERIFY=1` (the default). `done` means the
bytes landed **and** passed verification, and it always comes with
`finished_at`. `/_cache/status` counts `pending`, `running` and `verifying` as
active.

**Jobs survive a restart.** (OCI image prewarms do too, in their own ledger and on
the same terms: see *Docker management endpoints*.) Job records are kept in a small ledger,
`jobs.json` in the HF state directory (`<HF_HUB_CACHE>/.xhc/`, or
`$XHC_STATE_DIR/hf/`). A job that was `pending`, `running` or `verifying` when
the process stopped comes back as **`interrupted`**, with `downloaded_bytes`
showing the last recorded progress. It is **not** resumed automatically and
**not** dropped: a poller holding its id gets an answer instead of
`no such job`. How it gets there depends on how the process stopped:

- **Crash, OOM kill, `SIGKILL`.** Nothing runs at the end, so the ledger still
  says `pending`, `running` or `verifying`. The next process reports the job as
  `interrupted`, with `interrupted_at` set to **its own start time**.
- **Graceful stop** (`SIGTERM`, `docker stop`, a pod being deleted). Shutdown
  records every job still in flight as `interrupted`, with `interrupted_at` set
  to **the moment of the stop**, and writes the ledger before anything else
  shuts down. The next process reports it unchanged. A snapshot's progress is
  the last 5-second sample. Anything still waiting on the job in this process
  is told it was interrupted: a `wait` request gets `503` with `Retry-After`,
  and a `stream` response ends early, so the client sees a short body and
  retries.
- **Neither.** A job cancelled for any other reason ends as `error` with
  `error: cancelled`.

Graceful means uvicorn reached its shutdown step, and it first waits for open
responses to finish. If the orchestrator's `SIGKILL` lands before then, that is
the crash case. A **second** `SIGTERM` or `Ctrl-C` makes uvicorn skip the
shutdown step, so nothing marks the job: it comes back `interrupted` if the
process dies at once, but can be recorded as `error: cancelled` if the process
lives on to cancel it (as it does when uvicorn is a container's PID 1).

- **Bound.** Finished and interrupted jobs are kept for at most **7 days**, and
  at most the newest **50 prewarm (snapshot) jobs** and **200 file jobs**. Those
  two limits are separate, so a burst of client cache misses cannot push the
  prewarms you are polling out of the history. Active jobs are never dropped.
- **Write rate.** A state change is written at once, except that changes less
  than 0.5 s after the previous write are batched into one write at the end of
  that half-second. A prewarm finishing or failing is always written at once.
  Progress on its own is written at most every 30 s. Each write replaces the
  file atomically, so a kill never leaves half a ledger. A crash can lose at
  most the last half-second of changes.
- **An unreadable ledger does not stop the cache.** It is moved aside as
  `jobs.json.corrupt.<epoch>`, the error is logged, and a fresh ledger starts.
  This is the opposite of `pins.json`, which fails closed: an unreadable pins
  file means the cache cannot tell what is protected, whereas lost job history
  protects nothing and deletes nothing. `/_cache/status` → `jobs.ledger` shows
  whether the ledger is being written and the last error.
- **To tell whether the process restarted,** compare a job's `created_at` with
  `started_at` on `/_cache/status`. That works even if the ledger was lost.

**Resuming is re-submitting.** To finish an interrupted prewarm, `POST` the same
prewarm again. It is safe and cheap: files already in the snapshot are skipped
**without any request to the Hub**, because `snapshot_download` passes the
commit and `huggingface_hub` returns a cached file before any network call. A
half-downloaded file resumes from where it stopped with a `Range` request.
Verification then hashes only the files fetched this time.
`tests/test_snapshot_completeness.py` checks this against a local fake Hub by
recording every request it receives.

### What `/_cache/repos` reports

Each repo and each revision carries:

| field | meaning |
|---|---|
| `complete` | `true`, `false`, or **`null` when the expected set is unknown** |
| `files_present` / `files_expected` | expected files held (with the right size) / expected files |
| `bytes_present` / `bytes_expected` | the same in bytes; `bytes_expected` is `null` if the listing had no sizes |
| `expected_scope` | per revision: `repo` (the whole repo was asked for) or `allow_patterns` |
| `allow_patterns` | per revision, when the scope is `allow_patterns`: the pattern sets that were asked for |

**Where "expected" comes from.** Before a prewarm downloads anything, it asks
the Hub for the revision's file listing with sizes and saves it locally
(`<HF_HUB_CACHE>/.xhc/manifests/`). Listing repos only reads that saved copy and
never calls the Hub. Because the listing is saved *before* the download starts,
a prewarm killed halfway shows up as `complete: false`, which is the case this
exists for. A pinned repo holding 32 MB of a 52 GB snapshot no longer looks
finished.

**Judged against what was asked for.** A prewarm with `allow_patterns`
expects only the files matching those patterns, matched by the same function
`snapshot_download` uses. So `["*.json"]` that fetched every JSON file is
`complete: true`, and `expected_scope: "allow_patterns"` says that it is
complete relative to those patterns and not the whole repo. Several prewarms
of one commit expect the union of what they asked for. One without patterns
expects everything.

**`null` is not "probably complete".** A snapshot built up file by file from
client requests has no saved listing, and neither does one whose listing call
failed. Those report `complete: null`, with `files_present` counting what is
there. A repo is `false` if any revision is `false`, otherwise `null` if any is
`null`, and `true` only when every revision is known to be complete.

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
H="Authorization: Bearer $XHC_MANAGE_TOKEN"
curl -H "$H" localhost:8080/_cache/orphans | jq          # what is being retained, and why
curl -H "$H" -X POST localhost:8080/_cache/orphans/check # sweep now
curl -H "$H" -X DELETE localhost:8080/_cache/orphans \
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
curl -X DELETE localhost:8080/_cache/repos -H "Authorization: Bearer $XHC_MANAGE_TOKEN" \
  -H 'content-type: application/json' -d '{"repo_id":"org/model"}'

# or a single revision, leaving the rest of the repo intact
curl -X DELETE localhost:8080/_cache/repos -H "Authorization: Bearer $XHC_MANAGE_TOKEN" \
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

**Partial downloads left by a killed process are swept automatically.** `huggingface_hub`
downloads into `blobs/<etag>.incomplete` and renames it on completion; Muninn's tier fill
writes `blobs/<etag>.tier.incomplete`. Kill the process mid-file (measured with `SIGKILL`,
on the plain-HTTP path and on a real Xet download) and it leaves the partial, a 0-byte
`.locks/<repo>/<etag>.lock`, the `refs/` entry and an empty `snapshots/<commit>/`. The
plain-HTTP path resumes that partial if the same file is requested again. The Xet path
does not: it rewrites the file from the start. Nothing resumes a `.tier.incomplete`.
`scan_cache_dir()` counts only blobs a snapshot links to, so it cannot see partial bytes.

The eviction loop sweeps partials at startup (once, before the first interval) and then
every `XHC_EVICT_INTERVAL`. `POST /_cache/evict` sweeps before it measures. A partial is
removed only when **all three** of these hold:

1. no download in this process owns it. That means no ingest job for the blob (a prewarm,
   or a file job with no known ETag, owns every partial in its repo) and no tier fill;
2. no process holds `huggingface_hub`'s own lock for it, `.locks/<repo>/<etag>.lock`.
   Every writer (the hub and the tier fill) holds that `flock` for the life of the
   partial, and the kernel drops it when the writer dies, so a second process sharing
   the cache is seen too. The sweep takes the lock itself, without blocking, and deletes
   only while holding it, so no download can start on the file in between. Lock files
   are left in place: the hub creates and keeps them;
3. it has not been written for `XHC_HF_PARTIAL_MAX_AGE` seconds (default `21600`, six
   hours). This is the backstop for a filesystem that does not honour `flock`. It is also
   how long a plain-HTTP partial keeps its resume value. It is a separate setting from
   `XHC_DOCKER_PARTIAL_MAX_AGE`, with the same default, for that reason.

Each removal is logged with its name, size and idle time, and a summary line gives the
totals. `/_cache/status` shows `cache.partial_bytes` (bytes in partials right now, live or
stale) and `cache.partials` (the last sweep: `trigger`, `at`, `scanned`, `removed`,
`freed_bytes`, `kept_owned`, `kept_locked`, `kept_young`, `kept_bytes`, `max_age_s`). A
`removed: 0` can therefore be told apart from "found nothing to look at".

**Partials count against the capacity budget.** Eviction's `used` is the blob bytes
`scan_cache_dir()` reports **plus** the partial bytes still on disk after the sweep. Those
partials are owned by a download or too young to judge, and an owned one is about to
become a blob. The `POST /_cache/evict` result breaks the figure down as `blob_bytes` and
`partial_bytes`, and carries the sweep's `partials` object. Bytes the sweep removed are in
`partials.freed_bytes`, not in `freed`, which counts evicted revisions. `size_on_disk` and
`muninn_cache_bytes` still mean blob bytes only. `disk.fs_used` is the filesystem's own
figure and always included partials.

**Two other things a killed ingest leaves are deliberately not removed.**

- **An empty `snapshots/<commit>/` directory.** `huggingface_hub` creates the snapshot
  directory and writes `refs/<revision>` before the first byte of a file arrives, so an
  ingest killed before any file lands leaves an empty snapshot. `/_cache/repos` lists it as
  a revision with `nb_files: 0`. If a prewarm recorded a manifest for that commit, the
  revision shows `complete: false` (0 of N files). Without a manifest it shows
  `complete: null`. Neither reads as a finished snapshot. The sweep leaves the directory
  alone because it holds no bytes, the next ingest of that commit reuses it, and removing
  it while `refs/` still names it would make `scan_cache_dir()` reject the whole repo
  ("Reference(s) refer to missing commit hashes"). That would drop the repo from
  `/_cache/repos`, and eviction would stop seeing its blobs. Since the ref is written first,
  a referenced empty snapshot is the usual case.
- **hf-xet's log files.** hf-xet writes one log file per process (about 44 KB each) to
  `$HF_XET_CACHE/logs/`, which is `/xet/logs` in the image. hf-xet prunes that directory
  itself each time a process starts: it deletes logs older than
  `HF_XET_LOG_DIR_MAX_RETENTION_AGE` (default 14 days), then trims oldest-first to
  `HF_XET_LOG_DIR_MAX_SIZE` (default `250mb`). It never deletes a file younger than
  `HF_XET_LOG_DIR_MIN_DELETION_AGE` (default 1 day) or one whose process is still running.
  This was checked against hf-xet 1.6.0's source (xet-core `v1.6.0`,
  `xet_runtime/src/config/groups/log.rs` and `logging/init.rs`) and by running it: a
  30-day-old log was removed at import, and a 3-day-old one was kept. Muninn adds no
  pruner of its own, because that would be a second mechanism deleting the same files. To
  tighten the bound, set those variables. To write no files at all, set
  `HF_XET_LOG_DEST=""`, which sends hf-xet's logs to the console at `warn` level.
  `RUST_LOG` sets the level. `HF_XET_LOG_DIR_DISABLE_CLEANUP` turns pruning off, so leave
  it unset. A test fails if an hf-xet upgrade stops pruning.

## Sizing memory for ingest

Almost all of the memory an ingest uses belongs to **hf-xet**, the library
`huggingface_hub` uses to download from Xet storage, not to Muninn. These are
measurements of peak anonymous memory (the part a container limit kills on, not
page cache), hf-xet 1.6.0, `huggingface_hub` 0.34.4. They are not guarantees; a
different library version can move them.

| what was downloaded | peak |
|---|---|
| one 3.9 GB file, plain `hf_hub_download`, no Muninn involved | ~2.3 GiB |
| the same file through Muninn | 1.9 - 2.6 GiB |
| one 1 GB file | ~0.3 GiB |
| single files of 16 - 50 GB (reported from a deployment) | ~2 - 2.5 GiB |
| a four-file 15 GB snapshot, `XHC_SNAPSHOT_MAX_WORKERS=1` | ~3.0 GiB |
| a five-file 24.4 GB snapshot, `XHC_SNAPSHOT_MAX_WORKERS=1` (reported from a deployment, working set, not anon) | ~2.6 GiB, against ~2.5 GiB for one 24 GB file on the same node; the same snapshot at 8 in flight was OOM-killed at 4 GiB |
| the same snapshot, 8 files in flight | ~4.4 GiB |
| one 3.9 GB file with `HF_HUB_DISABLE_XET=1` on the cache | ~45 MiB |

So the peak rises with file size and then levels off around 2 - 2.5 GiB per
file, climbs somewhat across a multi-file snapshot, and stacks with every file
or job in flight. Neither `HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY`, the range-GET
count nor `MALLOC_ARENA_MAX` moved it where it was measured.

**Rule of thumb:** allow about 3 GiB per concurrent ingest
(`XHC_INGEST_CONCURRENCY`) with one file in flight each, plus headroom. Two
settings multiply:

- `XHC_INGEST_CONCURRENCY` bounds **jobs**.
- `XHC_SNAPSHOT_MAX_WORKERS` bounds **files within one snapshot job**. It
  defaults to 1: hf-xet already parallelises inside a file, and one file at a
  time was also the fastest of 1, 2 and 8 where it was measured.

At startup Muninn compares these against the container's cgroup memory limit and
logs a warning when the limit looks too small. It warns rather than refuses,
because the comparison is against the estimates above. A limit that is too
small shows up as an OOM kill, and the job as `interrupted` in `/_cache/jobs`.

`HF_HUB_DISABLE_XET=1` *on the cache* cuts ingest memory to almost nothing, at a
throughput cost that depends on the path (about 60% of xet's rate in one
measurement; much worse has been seen elsewhere) and with `huggingface_hub`'s
50 GB per-file download limit applying. Measure it on your own network before
choosing it.

## Configuration

| variable | default | meaning |
|---|---|---|
| `HF_TOKEN` | — | org token. Edge nodes then need no Hub credentials, and gated licences are accepted once, centrally. |
| `HF_HUB_CACHE` | `/cache` | the array. Standard `huggingface_hub` layout. |
| `XHC_STATE_DIR` | *(unset)* | absolute path for durable state (pins, orphan marks, runtime policy, and `store-forward` pushes not yet delivered upstream). Unset keeps it inside each cache tree. Set, it moves to `$XHC_STATE_DIR/hf/` and `$XHC_STATE_DIR/oci/`. See [Separating state from blobs](#separating-state-from-blobs) |
| `XHC_CACHE_MAX_SIZE` | filesystem size | eviction target, e.g. `70T`. Binary units. |
| `XHC_HIGH_WATER` / `XHC_LOW_WATER` | `0.90` / `0.75` | evict when above high, down to low |
| `XHC_EVICT_INTERVAL` | `900` | background sweep, seconds |
| `XHC_HF_PARTIAL_MAX_AGE` | `21600` | seconds a `blobs/*.incomplete` partial must go unwritten before the sweep may remove it, and then only if no download owns it and no process holds the hub's lock for it. See *Pinning vs. eviction* |
| `XHC_MISS_POLICY` | `stream` | `stream` \| `redirect` \| `wait` |
| `XHC_BLOCK_CLIENT_XET` | `1` | 404 the Xet token endpoints so clients can't bypass the cache |
| `XHC_HF_VERIFY` | `1` | hash each ingested HF file against its ETag and refuse a mismatch |
| `XHC_WEB_ROOT` | *(unset)* | serve static files at `/` so one hostname is a homepage **and** a cache |
| `XHC_AUTHZ_DB` | *(unset)* | SQLite store enabling per-key push/pull authorisation; unset keeps the single shared htpasswd gate |
| `XHC_OIDC_ISSUER` | *(unset)* | OIDC provider, e.g. `https://accounts.example.com`. Setting it enables the browser login and the key-management console, and **requires** the four variables below plus `XHC_AUTHZ_DB` |
| `XHC_OIDC_CLIENT_ID` | *(unset)* | OAuth client id |
| `XHC_OIDC_CLIENT_SECRET` | *(unset)* | OAuth client secret |
| `XHC_OIDC_REDIRECT_URI` | *(unset)* | exact callback URL, e.g. `https://cache.example.com/_auth/callback`. Pinned, never taken from a query parameter |
| `XHC_OIDC_SCOPES` | `openid email profile` | scopes requested at the provider |
| `XHC_OIDC_DISCOVERY_URL` | *(derived)* | where the discovery document lives, when it is not `<issuer>/.well-known/openid-configuration`. Changes only where it is **fetched**; the issuer stays the trust anchor and a document declaring a different one is refused |
| `XHC_OIDC_PKCE` | `1` | set `0` only if a provider **rejects** the parameter. Not advertising support is not the same as refusing it |
| `XHC_OIDC_ADMIN_CLAIM` | *(unset)* | id_token claim that decides admin, e.g. `groups` or `realm_access.roles` (dotted for nested). With `XHC_OIDC_ADMIN_VALUE`, admin is recomputed at **every** login — granted or **revoked** — and the first-login grant is off. Both or neither; needs `XHC_OIDC_ISSUER`. See [Admin from the identity provider](#admin-from-the-identity-provider-xhc_oidc_admin_claim) |
| `XHC_OIDC_ADMIN_VALUE` | *(unset)* | the value that grants admin: equal to a string claim, or to one element of a list claim. Exact match |
| `XHC_BOOTSTRAP_ADMIN` | *(unset)* | subject or email granted admin **when their principal is first created**, regardless of how many exist. Needed when machine credentials are migrated in before the first human login. Never consulted again, so it cannot re-promote someone demoted, and it never creates a principal by itself. **With `XHC_OIDC_ADMIN_CLAIM` set it is instead a standing break-glass grant**, applied at every login of that person whatever the claim says, matching the **subject only** (a value containing `@` logs a warning at startup, since an email would match nobody) — leave it unset outside an emergency |
| `XHC_SESSION_SECRET` | *(unset)* | signs the session cookie. No generated default: a per-process random value logs everyone out on restart and fails to log anyone out across replicas |
| `XHC_SESSION_TTL` | `43200` | session lifetime in seconds (12h). With `XHC_OIDC_ADMIN_CLAIM`, also the longest an existing session keeps admin after the role is revoked at the provider |
| `XHC_METRICS_AUTH` | `none` | `token` requires `Authorization: Bearer $XHC_MANAGE_TOKEN` on `/metrics`. Default is open, because `/metrics` is usually already a scrape target and gating it silently stops alerting. Worth setting on a public instance: the `registry` label names your upstreams and `muninn_cache_bytes` is a capacity signal |
| `XHC_HF_AUTH` | `none` | `key` requires a credential from `XHC_AUTHZ_DB` on the **Hugging Face surface** — the catch-all serving everything not claimed by another router. Accepts Basic **or** `Bearer <key_id>:<secret>`, so a user can set `HF_TOKEN` to that and Hugging Face's own tooling works unchanged. Also accepts `Bearer <jwt>` from an issuer in `XHC_JWT_ISSUERS`. The web root stays public, so a homepage still renders logged out |
| `XHC_HF_RULES` | `enforce` | with `XHC_HF_AUTH=key`: `enforce` lets a key pull only the Hugging Face repos its rules cover (`models/org/*` and so on), on hits as well as misses; `off` lets any live key pull anything, the behaviour before this setting existed. **Upgrading with `XHC_HF_AUTH=key` on changes behaviour** for principals with only registry rules. See [Rules on the Hugging Face surface](#rules-on-the-hugging-face-surface-xhc_hf_rules). No effect when `XHC_HF_AUTH=none` |
| `XHC_JWT_ISSUERS` | *(unset)* | JSON list of OIDC issuers whose signed tokens authenticate as a principal in `XHC_AUTHZ_DB` (required with it), on `/v2` and, with `XHC_HF_AUTH=key`, the Hugging Face surface. Never on `/_cache`. Each needs `issuer`, `audience` and `subject_template`. See [Workload identity (JWT)](#workload-identity-jwt) |
| `XHC_JWT_CACHE_TTL` | `60` | seconds one verified token is remembered, so a pod pulling 400 files is verified once. Never past the token's `exp`; revocation of its principal is still immediate. `0` verifies every request |
| `XHC_DOCS` | `1` | FastAPI's `/docs`, `/redoc` and `/openapi.json`. They describe the management API and are unauthenticated by construction; set `0` on a public deployment |
| `XHC_INGEST_CONCURRENCY` | `4` | simultaneous WAN ingests (jobs) |
| `XHC_SNAPSHOT_MAX_WORKERS` | `1` | files in flight **within** one snapshot ingest, passed to huggingface_hub as `max_workers`. It multiplies with `XHC_INGEST_CONCURRENCY`. Memory is the reason: see *Sizing memory for ingest*. Before this setting existed it was fixed at 8 |
| `XHC_NEGATIVE_TTL` | `60` | seconds to remember an upstream 404; `0` disables |
| `XHC_ORPHAN_POLICY` | `retain` | `retain` \| `evict` — what to do with repos deleted upstream |
| `XHC_ORPHAN_CHECK_INTERVAL` | `21600` | seconds between upstream liveness sweeps; `0` disables |
| `XHC_SYNTHESIZE_REPO_INFO` | `1` | rebuild repo/tree listings from cache when upstream 404s |
| `XHC_REF_TTL` | `300` | seconds a ref→commit mapping is trusted; `0` never revalidates |
| `XHC_UPSTREAM` | `https://huggingface.co` | the Hub this cache fetches from |
| `XHC_TIER2` | *(unset)* | an S3-compatible bucket as a second tier. Its own settings are in [Tier configuration](#tier-configuration). **Muninn never deletes from it** |
| `XHC_HOST` | `0.0.0.0` | bind address |
| `XHC_PORT` | `8080` | bind port |
| `XHC_REQUEST_TIMEOUT` | `60` | seconds before an upstream request is abandoned |
| `XHC_STREAM_START_TIMEOUT` | `120` | seconds a `stream` miss waits for the first bytes to land |
| `XHC_STREAM_POLL_INTERVAL` | `0.25` | seconds between checks for new bytes while streaming a miss |
| `XHC_INGEST_POLICY` | `open` | `open` \| `allowlist` |
| `XHC_ALLOW_REPOS` / `XHC_DENY_REPOS` | unset | comma-separated globs; deny wins. Matched against `models/<org>/<name>`, `datasets/<org>/<name>` or `spaces/<org>/<name>`, so the type prefix is required: `models/org/name-*,datasets/my-org/*`. A bare `org/name-*` matches nothing. Case-sensitive, and `*` also matches `/`. Per-key rules use the same shape but match case-insensitively; see [Rules on the Hugging Face surface](#rules-on-the-hugging-face-surface-xhc_hf_rules) |
| `XHC_POLICY_SCOPE` | `ingest` | `ingest` \| `all` — whether policy also gates cache hits |
| `XHC_MAX_FILE_BYTES` | unset | refuse to ingest a file larger than this |
| `XHC_VIEWER_ENDPOINTS` | `parquet,croissant` | dataset metadata endpoints to cache |
| `XHC_VIEWER_CACHE_TTL` | `3600` | seconds; `0` disables freshness but keeps entries for deleted datasets |
| `XHC_DATASETS_SERVER` | `https://datasets-server.huggingface.co` | upstream for the `/datasets-server/*` route; empty disables it |
| `XHC_DATASETS_SERVER_ENDPOINTS` | `splits,first-rows,info,size,is-valid,parquet` | which of those to cache (never `rows`) |
| `XHC_MANAGE_TOKEN` | unset | enables the `/_cache/*` management API and is the bearer token it requires. **Unset or blank means the management API is off**: every `/_cache` route answers `404`. With `XHC_AUTHZ_DB` set it also enables `/_cache/authz`, which **mints keys** — handle it as a Secret |
| `XHC_STREAM_CHUNK` | `4194304` | LAN read/serve chunk size |
| `XHC_MAX_RANGES` | `64` | max parts in a multi-range request before the header is ignored |
| `HF_XET_NUM_CONCURRENT_RANGE_GETS` | `32` (image) | range-GET parallelism in older `hf_xet`. **On hf-xet 1.6.0 it appears to have no effect:** the name is absent from the library, and 1, 4 and 32 gave the same time and memory on one ~110 MB/s link. Kept in the image for versions that read it |
| `HF_XET_HIGH_PERFORMANCE` | unset | bigger buffers/concurrency; wants ≥64 GB RAM |
| `HF_XET_CHUNK_CACHE_SIZE_BYTES` | `100G` (compose) | `hf_xet` scratch; the one place chunk-level dedup can pay off |
| `HF_XET_LOG_DEST` | unset | where hf-xet logs. Unset: one file per process in `$HF_XET_CACHE/logs/`, pruned by hf-xet. Empty string: console only, no files. See *Pinning vs. eviction* |
| `HF_XET_LOG_DIR_MAX_RETENTION_AGE` / `HF_XET_LOG_DIR_MAX_SIZE` / `HF_XET_LOG_DIR_MIN_DELETION_AGE` | `14d` / `250mb` / `1d` (hf-xet's defaults) | hf-xet's own pruning of its log directory. `HF_XET_LOG_DIR_DISABLE_CLEANUP` turns it off. Leave that unset |
| `HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY` | `1` (image) | asks for front-to-back writes, which the default `stream` policy needs. hf-xet 1.6.0 writes sequentially with or without it (measured; see *stream* above) |

Invalid config fails at import rather than at first request — a bad
`XHC_MISS_POLICY` will refuse to start the container.

## On-disk layout

The cache is a stock `huggingface_hub` directory
(`models--org--name/{blobs,snapshots,refs}`). Deliberately: ingest is just
`hf_hub_download`, so atomic writes, symlinking and blob-level dedup across
revisions come for free, and the array stays readable by any standard HF client.
If this service ever gets in your way you can mount the volume read-only
elsewhere and point `HF_HUB_CACHE` straight at it. Our own state lives in
`.xhc/`: `pins.json`, `orphans.json` and `policy.json`, plus a regenerable
viewer response cache under `.xhc/viewer/`. The docker store keeps its own
`pins.json`, `orphans.json` and prewarm job ledger `prewarm.json` in
`<XHC_DOCKER_DIR>/.xhc/`.

### Separating state from blobs

Blobs can always be fetched again. Pins and orphan marks cannot: they are what
stops eviction deleting something, and under `XHC_ORPHAN_POLICY=retain` an
orphaned repo is the only copy left. If the blobs sit on disposable local disk,
put the state somewhere that survives it:

```yaml
environment:
  XHC_STATE_DIR: /state          # a small persistent volume
volumes:
  - /srv/muninn-state:/state
```

| unset (default) | `XHC_STATE_DIR` set |
|---|---|
| `<HF_HUB_CACHE>/.xhc/{pins,orphans,policy,jobs}.json` | `$XHC_STATE_DIR/hf/` |
| `<XHC_DOCKER_DIR>/.xhc/{pins,orphans,prewarm}.json` | `$XHC_STATE_DIR/oci/` |
| `<XHC_DOCKER_DIR>/_pending/` (store-forward pushes owed upstream) | `$XHC_STATE_DIR/oci/pending/`, with the bytes they need |

The two protocols get separate subdirectories, so their pin files never
collide. The viewer response cache stays with the blobs, because it is
regenerable and can be large. So do the prewarm listings used for
`complete` (`.xhc/manifests/`): they describe the blobs and are useless without
them. The job ledgers (`jobs.json`, and `prewarm.json` for OCI prewarms) sit with
the state so job history survives a lost cache disk, but they are not protection.
Each is copied across the first time
it is read rather than at startup, and a failure to copy it is logged, not
fatal.

- **Migration.** At startup, and again the first time any path reads one of
  these files, a file absent from the state dir but present in the old `.xhc/`
  is copied across and the copy is logged. The old file is left in place and is
  no longer read. The copy is byte for byte, so an unreadable pins file arrives
  unreadable and still makes eviction refuse rather than treating the cache as
  unpinned. A file already in the state dir is never overwritten.
- **Refuses to start** if the directory cannot be created or written, or is not
  an absolute path. It does not fall back to the cache tree, because a fallback
  would put protection back on the disk you just declared disposable.
- **Pending `store-forward` pushes move here too, bytes and all**, so a push a client was
  told succeeded survives replacing the blob disk. Size the volume for what can be in flight,
  or bound it with `XHC_DOCKER_PUSH_PENDING_MAX_SIZE`. See
  [What survives what](#what-survives-what).
- **Put `XHC_AUTHZ_DB` on the same volume.** The key store holds principals,
  keys and grants, and losing it locks every user out until they are
  re-issued. It has exactly the same lifetime as the pins, and none of the
  blobs', so it belongs beside them rather than on the disk you expect to lose.

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
pytest -m minio    # the object-store tier against a throwaway MinIO (needs docker)
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
