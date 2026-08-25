# Spec: Docker/OCI pull-through caching

Status: **phase 1 implemented and verified against real registries** (2026-08-25).
Phases 2 and 3 not started. See §14 for what live testing settled and changed.

Adds a second protocol to Muninn: an OCI Distribution pull-through cache for
**any** upstream registry, addressed by path prefix, sharing the array,
eviction, retention, policy and metrics with the Hugging Face side.

```
docker pull cache.example.com/ghcr.io/skibare87/muninn:0.4.0
docker pull cache.example.com/docker.io/library/nginx:1.27
docker pull cache.example.com/quay.io/prometheus/prometheus:v3.1.0
```

Decided up front, from discussion:

- **Client auth is optional**, off by default.
- **Allowlists reach parity** with the existing HF ingest policy.

---

## 1. Why path prefix

`docker pull cache.example.com/ghcr.io/org/img:tag` splits on the first `/`.
Docker treats the first component as a registry host when it contains `.` or
`:`, or is `localhost`. `cache.example.com` qualifies, so the rest is the
**repository name**, and dots are legal in repository path components per the
OCI spec. `ghcr.io/org/img` is a valid repo name and is never re-parsed as a
host. Muninn therefore receives:

```
GET /v2/ghcr.io/org/img/manifests/tag
```

and routes on the first path segment.

**What this buys.** Zero per-node configuration — no `hosts.toml`, no
`registry-mirrors` (which only ever worked for Docker Hub anyway), no
`insecure-registries`, no daemon restart. Identical behaviour across docker,
podman, containerd, buildkit and Kubernetes, because from their side it is
simply a registry. It also removes Docker Hub anonymous rate limits outright,
which is usually the first benefit anyone actually feels.

**What it costs.** Every image reference has to be rewritten — `FROM` lines,
k8s manifests, compose files. Anything missed silently bypasses the cache. That
is the entire tradeoff against transparent mirroring, and it is accepted here
because it works everywhere with no fleet configuration.

Not exclusive: containerd `hosts.toml` can be layered on later, pointing at the
same server, for nodes that want transparency. No code change needed for that.

---

## 2. Routing

`/v2/*` is the Docker surface. Everything else stays Hugging Face, unchanged.
The Docker router must be registered **before** the HF catch-all, which would
otherwise swallow it.

Upstream resolution from the repository name:

| reference | upstream | upstream repo |
|---|---|---|
| `ghcr.io/org/img` | `https://ghcr.io` | `org/img` |
| `quay.io/org/img` | `https://quay.io` | `org/img` |
| `docker.io/library/nginx` | `https://registry-1.docker.io` | `library/nginx` |
| `docker.io/nginx` | `https://registry-1.docker.io` | `library/nginx` |
| `nginx` (no dot in first segment) | default upstream | `library/nginx` |

**Docker Hub needs a special case** and it is the most commonly used upstream:
`docker.io` is not the API host (`registry-1.docker.io` is), `index.docker.io`
is an alias, and single-segment repositories get an implicit `library/` prefix.
Encode this once, in one table.

`XHC_DOCKER_DEFAULT_UPSTREAM` (default `docker.io`) handles a first segment with
no dot, so `cache.example.com/nginx` behaves the way everyone expects.

---

## 3. API surface

Pull only.

| method | path | behaviour |
|---|---|---|
| `GET` | `/v2/` | answered locally: `200`, `Docker-Distribution-API-Version: registry/2.0`. Not repo-scoped, so there is no upstream to forward it to. |
| `GET`/`HEAD` | `/v2/<name>/manifests/<ref>` | `<ref>` is a tag or `sha256:…` |
| `GET`/`HEAD` | `/v2/<name>/blobs/<digest>` | content-addressed, immutable |
| `GET` | `/v2/<name>/tags/list` | proxied; cached with a short TTL |
| anything else | | `405`, with a body saying this is a pull-through cache |

Push endpoints (`POST`/`PATCH`/`PUT` uploads, cross-repo mounts) are explicitly
out of scope, as uploads are on the HF side.

### 3.1 Manifests

**Serve the bytes verbatim. Never re-serialize.** The digest is computed over
the exact bytes, so re-encoding — key reordering, whitespace, anything — changes
the digest and breaks every pull-by-digest and every signature check. This is
the classic way these proxies get subtly and confusingly broken. Store raw bytes
plus the recorded `Content-Type`.

- Echo `Docker-Content-Digest` on every manifest response.
- `HEAD` returns identical headers with no body.
- `Content-Type` must be the stored media type, not guessed.

**Accept negotiation.** The same tag can return different manifests depending on
the client's `Accept` (OCI index vs Docker manifest list vs a platform
manifest). The tag→digest mapping is therefore keyed on
`(upstream, repo, tag, normalized-accept-fingerprint)`. Manifests themselves are
stored by digest, where there is no ambiguity.

Manifest lists and multi-arch then fall out for free: everything below the tag is
digest-addressed and immutable.

### 3.2 Tag revalidation — reuse, do not reinvent

A tag is a mutable ref pointing at a digest. This is **exactly** the `main` →
commit problem solved in v0.3.0, and should reuse that machinery:

- `sha256:…` references are immutable and never revalidated, at zero cost.
- Tag→digest mappings are trusted for `XHC_DOCKER_TAG_TTL` (default `300`, for
  parity with `XHC_REF_TTL`).
- Revalidation is single-flight per `(upstream, repo, tag)`.
- It **fails open**: unreachable, rate-limited, 401 or 404 upstream means keep
  serving the digest we hold. Required for orphan retention, where a deleted tag
  404s permanently and correctly.

### 3.3 Blobs

Blobs are content-addressed by `sha256:…` and therefore immutable — strictly
better than HF ETags, because correctness is checkable rather than assumed.

- **Verify the digest on ingest.** On mismatch, discard and return `502`. Never
  commit unverified bytes to the array.
- Single-flight per digest, as with HF file ingest.
- Stream-while-caching via the existing tail-follow path: layers are large and
  N nodes rolling the same deployment should cost one upstream pull.
- `Range` is honoured, both on hits and mid-ingest, reusing the v0.2.x work —
  containerd uses ranges to resume.
- Blobs are shared across repositories within an upstream, so **layer dedup is
  free**: `nginx:1.26` and `nginx:1.27` share base layers on disk with no extra
  work.

---

## 4. Authentication

### 4.1 To upstream registries

Registries answer `401` with `WWW-Authenticate: Bearer realm=…,service=…,scope=…`;
the client fetches a short-lived scoped token from the realm and retries.
Muninn performs this on the fleet's behalf, so **edge nodes stay anonymous** —
the same win as `HF_TOKEN`, and one place to rotate credentials.

Tokens are cached per `(upstream, scope)` with a TTL, the same pattern as ref
revalidation.

**Credentials are supplied as a standard `~/.docker/config.json`,** mounted as a
Docker secret. Reusing the format `docker login` already produces means no
bespoke config, and it keeps credentials out of `docker inspect`:

```yaml
secrets:
  registry_auth:
    file: ./registry-config.json
services:
  muninn:
    secrets: [registry_auth]
    environment:
      XHC_REGISTRY_AUTH_FILE: /run/secrets/registry_auth
```

Per-registry env vars are supported as a fallback for simple cases
(`XHC_REGISTRY_GHCR_IO_TOKEN`), but the secret file is the documented path.

### 4.2 From clients — optional, off by default

`XHC_DOCKER_AUTH`:

- `none` (default) — anonymous pull, matching the HF side's LAN posture.
- `basic` — `401` with `WWW-Authenticate: Basic realm="muninn"`, credentials
  from a mounted htpasswd file (bcrypt). Works with plain `docker login`.

Note Docker refuses basic auth over plaintext HTTP except on localhost, so
`basic` implies TLS. `cache.example.com` already has a certificate, so this
is a non-issue here but must be documented for anyone else.

Token/OAuth2 service auth is **not** in scope: it is materially more work and
basic auth over TLS covers the stated need.

---

## 5. Policy — parity with the HF side

Path-prefix routing means anyone who can reach the host can pull *anything* from
*any* registry onto the array. The existing policy machinery extends directly,
in the same `.xhc/policy.json`, under a `docker` key:

```json
{
  "docker": {
    "mode": "allowlist",
    "registries": ["docker.io", "ghcr.io", "quay.io", "registry.k8s.io"],
    "allow": ["ghcr.io/skibare87/*", "docker.io/library/*"],
    "deny":  ["*/*:latest-nightly"],
    "scope": "ingest",
    "max_blob_bytes": 10737418240
  }
}
```

Semantics match the HF side exactly, so there is one model to learn:

- **Deny always wins** over allow.
- Policy gates **ingest, not serving**, by default, so tightening it cannot
  break a rollout in flight. `"scope": "all"` enforces on cache hits too.
- `max_blob_bytes` is checked against the upstream `Content-Length` before any
  bytes move.
- Refusals are `403` with `x-xhc-policy: denied`. They deliberately do **not**
  imitate a registry auth error, which would send people to `docker login`
  pointlessly.

**Registry allowlist is separate from image patterns** because it is the guard
that matters most here: a two-line allowlist of hosts blocks the entire class of
"someone pulled a random image through the shared box".

### Recommendation, needing a decision

The HF side defaults to `open` for parity. For Docker I would default
`registries` to **allowlisted** (`docker.io, ghcr.io, quay.io, registry.k8s.io`)
with image patterns open. That blocks arbitrary third-party registries by
default while letting everything normal work untouched. Strict parity would mean
`open`. Flagging rather than deciding.

---

## 6. Storage

A separate root from the HF cache, so `scan_cache_dir` never sees it and the
two layouts cannot confuse each other:

```
$XHC_DOCKER_DIR/                       # default /docker, its own volume
  <upstream>/
    blobs/sha256/<ab>/<digest>         # 2-char shard
    manifests/sha256/<ab>/<digest>     # verbatim bytes
    manifests/<digest>.meta.json       # media type, size, fetched_at
    tags/<repo>/<tag>.json             # {digest, media_type, checked_at}
```

The two-character shard matters: the scan-cost benchmarking in v0.1.0 showed
cost tracks **file count**, and a busy registry cache reaches six figures of
blobs. Flat directories would make every sweep painful.

`<upstream>` is part of the path because the same digest can legitimately exist
on multiple registries, and because it makes per-registry eviction and
accounting possible.

---

## 7. Eviction, pinning and retention

The one genuinely new hard problem. HF blobs are referenced by exactly one
snapshot tree; **Docker blobs are referenced by manifests, and manifests by
tags.** Evicting a blob that a retained manifest still references produces a
broken image that fails at pull time with a confusing error.

**Mark and sweep, not naive LRU:**

1. Walk tags → manifests → referenced blobs (config + layers, recursing through
   manifest lists), building a referenced set.
2. Eviction candidates are blobs **not** in that set, oldest first.
3. Referenced blobs are only evictable once their manifest is evicted, which
   means eviction works top-down: drop the tag and manifest first, then their
   now-unreferenced blobs.

**Pinning an image pins its closure.** `pin ghcr.io/org/img:1.2.3` must retain
the manifest *and* every blob it references, or the pin is worthless. This is
the main behavioural difference from HF pins and needs to be explicit in the
implementation and the docs.

**Orphan retention applies unchanged.** A tag or repository deleted upstream is
detected by the existing sweep pattern and, under `XHC_ORPHAN_POLICY=retain`,
becomes unevictable. Deleted tags are considerably more common in registry land
than deleted HF repos, so this will fire more often — and it is exactly the
reproducibility property that motivates putting this in Muninn rather than
running a separate cache.

**Capacity.** Separate budget from the HF cache (`XHC_DOCKER_MAX_SIZE`,
high/low water reused), because a single shared budget makes it possible for
image churn to evict models, which is surprising. A shared budget can be added
later if wanted.

---

## 8. Metrics and management

Metrics follow the existing naming:

```
muninn_docker_requests_total{result="HIT|MISS|SYNTHESIZED|DENIED", kind="manifest|blob"}
muninn_docker_upstream_requests_total{registry="ghcr.io", status="…"}
muninn_docker_bytes{served,ingested}_total
muninn_docker_blobs / muninn_docker_bytes / muninn_docker_capacity_bytes
muninn_docker_tag_lookups_total
```

Management endpoints mirror the HF ones:

| method | path | purpose |
|---|---|---|
| `GET` | `/_cache/docker/images` | cached images, tags, sizes, pin and orphan state |
| `POST` | `/_cache/docker/prewarm` | pull an image ahead of a rollout — the direct analogue of HF prewarm, and the feature most likely to be used daily |
| `GET`/`POST`/`DELETE` | `/_cache/docker/pins` | pin an image and its blob closure |
| `DELETE` | `/_cache/docker/images` | force-evict a repo, tag, or digest |
| `POST` | `/_cache/docker/gc` | run mark-and-sweep now |

---

## 9. Configuration

| variable | default | meaning |
|---|---|---|
| `XHC_DOCKER_ENABLED` | `1` | serve `/v2/*` at all |
| `XHC_DOCKER_DIR` | `/docker` | storage root, separate volume |
| `XHC_DOCKER_MAX_SIZE` | unset | capacity for the Docker cache |
| `XHC_DOCKER_DEFAULT_UPSTREAM` | `docker.io` | used when the first segment has no dot |
| `XHC_DOCKER_TAG_TTL` | `300` | tag→digest revalidation, parity with `XHC_REF_TTL` |
| `XHC_DOCKER_AUTH` | `none` | `none` \| `basic` |
| `XHC_DOCKER_HTPASSWD` | unset | htpasswd file for `basic` |
| `XHC_REGISTRY_AUTH_FILE` | unset | mounted `config.json` for upstream credentials |
| `XHC_ALLOW_REGISTRIES` / `XHC_DENY_REGISTRIES` | see §5 | host-level globs |
| `XHC_ALLOW_IMAGES` / `XHC_DENY_IMAGES` | unset | globs over `<upstream>/<repo>` |
| `XHC_DOCKER_MAX_BLOB_BYTES` | unset | refuse an oversized layer before bytes move |

---

## 10. Failure modes

| situation | behaviour |
|---|---|
| upstream unreachable, blob cached | serve from cache |
| upstream unreachable, blob absent | `502` |
| tag deleted upstream | serve last known digest (retention) |
| digest mismatch on ingest | discard, `502`, log loudly — never commit |
| upstream `401` with no configured credentials | pass the challenge through; the client sees a normal auth failure |
| unknown/denied registry | `403` + `x-xhc-policy` |
| push attempted | `405` with an explanatory body |
| blob referenced by a retained manifest | never evicted |

---

## 11. Verified when

Against real registries, not stubs:

- `docker pull` of a **multi-arch** image from ghcr.io, docker.io and quay.io
  succeeds through the prefix, and `docker image inspect` reports the same
  digest as a direct pull
- pull **by digest** succeeds, proving manifests are served byte-identical
- a second pull is a cache hit with **zero upstream blob requests** (counted)
- N concurrent pulls of the same cold image cause **one** upstream pull per blob
- a corrupted blob (fault-injected) is rejected, not served
- two images sharing base layers store those layers **once**
- a moved tag is picked up after `XHC_DOCKER_TAG_TTL`, and not before
- mark-and-sweep never deletes a blob referenced by a retained manifest, proven
  by pulling after a forced GC
- pinning an image survives a GC that would otherwise evict its layers
- a denied registry returns `403` and transfers nothing
- with `XHC_DOCKER_AUTH=basic`, `docker login` then `docker pull` works, and an
  unauthenticated pull fails cleanly
- the HF side is byte-for-byte unaffected (full existing regression suite)

---

## 12. Effort and phasing

| phase | scope | rough size |
|---|---|---|
| 1 | routing, manifests, blobs, tags, upstream auth, registry allowlist, metrics | ~700 lines + tests |
| 2 | mark-and-sweep GC, pin closures, orphan retention, capacity | ~300 lines + tests |
| 3 | optional client basic auth, management endpoints, prewarm | ~200 lines + tests |

Phase 1 is independently useful and shippable as `0.5.0-rc`; it just cannot
manage its own disk yet, so it should run with a generous volume until phase 2
lands. Phases 2 and 3 can ship as `0.5.0`.

---

## 13. Not doing, and why

- **Push / write path.** A cache that accepts pushes is a registry, with
  garbage collection, quota and durability obligations. Out of scope, as with
  HF uploads.
- **Transparent mirroring via DNS + private CA.** MITM of public hostnames,
  rejected for the same reason as datasets-server. containerd `hosts.toml` can
  be layered on later against the same server with no code change.
- **Signature and provenance verification** (cosign, notary). Muninn passes
  bytes through unaltered, so signatures keep verifying at the client — which is
  the correct division of responsibility. Verifying *at the cache* is a
  different product.
- **Untagged-manifest garbage collection beyond mark-and-sweep.** Registries
  accumulate untagged manifests; sweeping them safely needs care, and phase 2's
  reference marking covers the case that matters.

---

## 14. Open questions

**Resolved during phase 1 (2026-08-25):**

1. **Registry allowlist default — resolved as `open`.** the maintainer's ruling was parity
   with the HF ingest policy, and the HF side defaults to `open`. My own
   recommendation had been allowlisted; his word governs. The exposure is real
   and different in kind from the HF side, so it is mitigated rather than
   ignored: the server logs a warning at boot when the policy is `open` with no
   registry allowlist, and two env vars close it
   (`XHC_DOCKER_POLICY=allowlist`, `XHC_ALLOW_REGISTRIES=…`). The registry
   allowlist is also honoured in `open` mode, so restricting hosts does not
   require flipping the whole policy.
3. **`/v2/tags/list` caching — resolved as no.** Proxied straight through. It is
   mutable, rarely on a hot path, and a stale tag list is more confusing than a
   slow one.

**Found by live testing, not by the spec:**

- **`GET /v2/<name>/referrers/<digest>` was missing.** Docker 29 queries the
  referrers API on every pull. With no route it fell through to the Hugging Face
  catch-all, which answered 404 — a legal response, arrived at by accident. Now
  proxied upstream (uncached) so cosign and other attestation tooling keep
  working through the cache, and a `/v2/{rest:path}` GET/HEAD catch-all
  guarantees no `/v2/` request can ever reach the HF handler again.
- **A warm `docker pull` still makes exactly one upstream request, and that is
  correct.** Docker probes for attestations via the fallback tag
  `sha256-<digest>` (hyphen, not colon), which is a genuine tag lookup for
  something that legitimately does not exist upstream. Not a cache miss, and
  not fixable without lying.
- **Single-flight was wrong in the first implementation.** The in-flight slot
  was claimed *after* the upstream request was opened, so 8 concurrent cold
  clients made 8 upstream requests and discarded 7. Invisible in bytes
  transferred; multiplies the REQUEST count by the herd size, which defeats the
  Docker Hub rate-limit benefit outright. Measured at 8/8 before the fix and
  1/8 after. **The offline tests could not see this; only a real concurrent
  pull against a real registry could.**

**Still open:**

2. **`XHC_DOCKER_DIR` as a separate volume** — assumed yes, so the two layouts
   stay independent and Docker churn cannot evict models. Confirm.
4. **Is the reference rewrite acceptable in practice**, or should a later phase
   add containerd `hosts.toml` generation as a convenience for nodes that would
   rather have transparency?
