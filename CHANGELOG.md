# Changelog

**Generated from the annotated git tags — do not edit by hand.**

The tag is the source of truth: it is written at release time and cannot drift
from the commit it names. Regenerate with `scripts/gen_changelog.py > CHANGELOG.md`.

Images are published to `ghcr.io/skibare87/muninn`. Only the full `X.Y.Z` tag is
immutable; `X.Y`, `latest` and `edge` all move.


## v0.9.0 — 2026-09-01

Push-through works end to end against a real registry, and a typo'd image name
no longer accuses the infrastructure.

Everything in 0.8.x was built and tested against registry:2 and a test client.
Pointing it at a different registry implementation, with a real multi-megabyte
layer on the other end, found seven defects. Every one of them passed the
existing suite. That is the release note: the gap was not test coverage, it was
that a fake never disagrees with you.

A NONEXISTENT REPO RETURNED 502 BAD GATEWAY

Docker Hub and ghcr refuse to leak existence. For a repo that does not exist
they issue a VALID anonymous token and then answer 401 to the request carrying
it, so "you mistyped the name" and "this is private and you cannot see it"
arrive as the same response. The cache mapped every upstream 401 to 502, so the
most common mistake anyone makes -- mistyping an image name -- rendered as Bad
Gateway and sent the reader off to check whether the cache, the host or the
registry was down.

An error is a routing instruction for whoever reads it next. 404 costs the
reader nothing; 502 costs them an afternoon. The cache now distinguishes being
refused AFTER authenticating -- an answer, rendered 404 -- from being unable to
authenticate at all, which is a real gateway problem and stays 502, as does a
configured credential that was rejected. A regression from this release's own
three-state error work, caught before it shipped.

THE TWO THAT ONLY A LARGE REAL LAYER COULD FIND

A 3 MiB blob failed with an empty-message ReadError while a 463-byte blob on the
same session succeeded. The cause is challenge-response plus a body: the upload
went out unauthenticated, the registry answered 401 as soon as it had the
headers and closed WITHOUT DRAINING, and the remaining writes failed. A small
body already sits in socket buffers and survives; a large one does not. Once an
upstream has challenged for Basic, credentials now go up front. The first
request to any upstream is still challenge-response, so credentials are never
sent to a registry that has not asked -- there is a test for that specifically,
because the failure mode of getting this wrong in the other direction is
leaking them.

A push stalled for 223 seconds at ~0% CPU. Writing client chunks to disk ran on
the event loop; docker sends a layer as many small writes, and each one blocked
it for a scheduling round trip. Measuring throughput said this was impossible --
80 MiB in 0.105s -- and throughput was the wrong measure. The cost was per-call
latency times call count. Client writes now go to a thread.

THE REST

- A relative upload Location is spec-legal and some registries return one; it was
  being used as an absolute URL. Now resolved against the session base. A relative
  auth realm is a different case and is still refused: it is meaningless, and
  guessing a host to send credentials to is not a recovery.
- store-forward is EVENTUALLY consistent, not immediately consistent. A manifest
  is now held until every blob it references is confirmed upstream, and a blob
  held behind a retry re-enqueues rather than being dropped. A manifest that lands
  before its layers is a tag resolving to a broken image.
- Transport failures retry with backoff (5 attempts, 1/4/15/45s), and outstanding
  forwards are recorded on disk before the client is answered, so a restart
  re-enqueues them instead of losing them. Confirmed in the wild rather than by
  construction: a forward that had exhausted its retries survived a restart and
  was delivered once the upload path could carry it.
- A failure is never reported without a reason. str(ReadError()) is the empty
  string, so the operator-facing text now always leads with the exception type.
- Push limits are logged at boot, including which upstreams are NOT listed and
  will therefore go unchunked.

DOCS

The pending endpoints are now documented -- an undocumented queue is an invisible
one, which is the failure store-forward exists to prevent. GET /_cache/docker/pending
shows state, error, attempts and pin status per outstanding forward; DELETE
abandons one, which is an explicit decision to break the promise made to whoever
pushed and to make the only copy evictable. Nothing does that automatically.

Also new: what a failed pull means, by status. The docker CLI discards the body
and headers and prints only the status, so the status is the whole message most
people ever see -- and 404 is the only one that hides itself, rendering as "not
found" with no code at all.

A COUNTER SERIES THAT DOES NOT EXIST READS AS ZERO

Prometheus handles a counter RESET; it cannot handle a series that is not THERE,
and those look identical on a graph. Counters keyed on first increment left a
label absent until its first occurrence after each restart, so any window
containing a restart was full of holes indistinguishable from zero and
increase() could not tell "this never happened" from "the process restarted".
The counters could not answer frequency questions, which is most of what a
counter is for. Every result/kind the code can emit is now seeded to 0 at
startup: a zero means zero, a gap means the process was down. Additive to
/metrics -- nothing is removed or renamed, and /healthz is untouched.

KNOWN GAP, filed not fixed: XHC_DOCKER_TAG_TTL has no value meaning "always
revalidate". 0 means never. The default of 300s is unaffected.


## v0.8.2 — 2026-09-01

Tests for the push HTTP routes, and two fixes they found.

The routes a docker client actually meets had NO tests. Everything about
push-through was verified underneath them -- the chunking policy, the session
state, the upstream forwarding, and an end-to-end push against a real
registry:2 -- and none of it issued an HTTP request to Muninn.

That matters because the OCI push protocol is a sequence, not a call. Each step
depends on headers from the previous one -- Location, Docker-Upload-UUID, Range
-- so a route returning the right status with a wrong header fails a real push
while passing any test that checks only the status.

FIXED, found by those tests: a write-shaped request matching no push route --
PATCH or PUT with no session id -- fell through to the catch-all, which told the
caller about "delete and cross-repo mount". With push disabled that is
misleading; the caller's next question is how to enable it. It now names
XHC_DOCKER_PUSH.

DELETE /_cache/docker/images now says what it did about layers. Dropping a tag
frees every layer no other tag references, but on the NEXT scheduled sweep --
up to XHC_EVICT_INTERVAL away. The old response mentioned only the tag, so
deleting an image and looking at the disk suggested nothing had happened.
`?sweep=1` reclaims immediately and reports blobs_removed, manifests_removed
and freed_bytes; it is opt-in because a sweep walks every blob in the store.

The delete-by-tag guarantee is now pinned by tests, including the half that
would lose data if it broke: a layer SHARED with a surviving tag must survive.
Container images share bases constantly, so an eviction that over-collected
would break every other image on the node. A pinned image also survives its tag
being dropped, because a pin is a root in its own right.


## v0.8.1 — 2026-09-01

store-forward with XHC_DOCKER_CACHE_ON_PUSH=0 is an EPHEMERAL store.

0.8.0 treated that combination as incoherent, kept the content anyway, and
logged that the setting was ignored. It is not incoherent. The two settings
answer different questions:

  XHC_DOCKER_PUSH_MODE        when the client is answered
  XHC_DOCKER_CACHE_ON_PUSH    whether the copy is kept afterwards

With both set, content is held only long enough to be forwarded and is deleted
once upstream confirms -- absorb the upload, push behind, and do not let pushed
images occupy the cache. Deleting before the push would leave nothing to
forward; deleting after was always the right answer.

The deletion is in the success path only, so a push that fails or is cancelled
still keeps its content, pinned and visible.


## v0.8.0 — 2026-09-01

Push-through. OFF BY DEFAULT (XHC_DOCKER_PUSH=1).

    docker push <cache-host>/ghcr.io/you/image:latest

is forwarded to ghcr.io/you/image:latest AND kept in the cache, so the next node
to pull it gets a local hit rather than a cold fetch.

THE POINT IS THE CHUNKING, not that a cache accepts writes. A `docker push` does
a MONOLITHIC PUT and has no chunk-size knob, so a registry behind a
body-size-limiting proxy rejects it outright -- which is why tools like regctl
must be configured per host and why plain docker fails against such a registry
for large layers. Muninn decouples the two: the client pushes normally and what
goes upstream is re-chunked per registry.

  XHC_DOCKER_PUSH_LIMITS   regctl-format file, per host. blobMax is the
                           threshold above which to chunk, blobChunk the piece
                           size. Only those two fields are read; credentials in
                           the file are ignored.
  XHC_DOCKER_BLOB_CHUNK    global fallback, so one registry does not need a file
  adaptive                 on a 413, halve and retry, and log the exact config
                           line to add -- an unconfigured push works, slowly,
                           and tells you how to make it fast

Default is no chunking, because the problem is sparse.

MODES. `proxy` (default) confirms upstream BEFORE answering, so a 201 means the
registry really has it. `store-forward` answers as soon as the content is on
disk and pushes behind: faster, retryable, and it TELLS THE CLIENT THE PUSH
SUCCEEDED BEFORE IT HAS. The mode that can lie is the one you ask for.

In store-forward, unconfirmed content is PINNED -- it is the only copy in
existence and cannot be re-fetched, so eviction and GC leave it alone. A push
that FAILS stays pinned and stays visible in the pending view rather than being
tidied away.

XHC_DOCKER_CACHE_ON_PUSH=1 by default: a push is nearly always followed by pulls
from other nodes and the bytes have already crossed the wire.

PUSH IS NOT GATED BY AUTHENTICATION. With client auth off, anyone who can reach
this cache can push to any registry it holds credentials for, under the cache's
identity and with no attribution -- a docker push cannot identify itself. That
is the same trust model as the pull surface rather than an exception to it.
Muninn warns at boot; it does not refuse. Restrict who can reach the port, or
set XHC_DOCKER_HTPASSWD.

Not implemented: delete and cross-repo mount. Removing upstream content is a
retention decision for that registry's owner, not for a cache in front of it.

Verified against a real registry:2 -- monolithic and chunked uploads both land,
a chunked 5 MiB layer fetches back with a matching digest, an existing blob is
not re-uploaded, and a manifest is retrievable by tag afterwards.


## v0.7.0 — 2026-09-01

Optional client auth on the pull surface. OFF BY DEFAULT.

  XHC_DOCKER_AUTH=basic
  XHC_DOCKER_HTPASSWD=/auth/htpasswd     (bcrypt only; htpasswd -B)

Then `docker login <cache-host>` works as usual. XHC_DOCKER_AUTH previously
existed as a knob that was parsed, validated to reject anything but none|basic,
and read by no code path -- a validating no-op reads as implemented. It is now
implemented.

IT IS A GATE, NOT PER-CLIENT ISOLATION, and it will not pretend otherwise.
Everyone who authenticates sees everything the cache holds. A cached hit
consults no credentials at all: it checks the fleet-wide policy and serves off
disk, and the store is keyed by upstream, repo and digest with no principal in
it. Any scheme promising "A cannot read what B pulled" would be enforced on the
miss and silently absent on every hit after it, and be false from the first
cache fill.

Per-host credentials rather than one shared secret, because a `docker pull`
cannot send an identifying header and the OCI path records no principal --
credentials are the only mechanism by which a cache can know which node pulled
what. A shared secret does not defer that, it forecloses it.

FAILS CLOSED. `basic` with a missing, unreadable, empty or non-bcrypt htpasswd
file refuses to start. An absent config means no auth was asked for; an
unreadable one when it was is UNKNOWN, and resolving unknown to permissive is
how a cache silently reopens itself. Distribute credentials first, then enable.

bcrypt only. Apache's other htpasswd formats are unsalted or broken, and
accepting one silently would make a weak file look configured.

GATES /v2/* AND NOTHING ELSE. /healthz and /metrics stay unauthenticated by
design; /_cache keeps its own XHC_MANAGE_TOKEN and is not opened by a pull
credential. That boundary is asserted by test, and it is the advantage over a
blanket reverse-proxy rule, which swallows the health endpoints unless carved
out by hand.

A Muninn 401 carries WWW-Authenticate: Basic realm="muninn". An upstream auth
failure is a 502 with x-xhc-upstream-auth and never a challenge, so the two are
distinguishable from outside without reading a body.

Unknown usernames are compared against a dummy hash, so a bad username and a bad
password cost the same time and latency cannot enumerate valid names.

New runtime dependency: bcrypt==5.0.0.


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

Fix /metrics 500 during ingest.

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
