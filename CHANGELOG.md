# Changelog

**Generated from the annotated git tags — do not edit by hand.**

The tag is the source of truth: it is written at release time and cannot drift
from the commit it names. Regenerate with `scripts/gen_changelog.py > CHANGELOG.md`.

Images are published to `ghcr.io/skibare87/muninn`. Only the full `X.Y.Z` tag is
immutable; `X.Y`, `latest` and `edge` all move.


## v0.9.13 — 2026-09-12

v0.9.13 -- a wildcard key scope means no limit

v0.9.12 refused it. That was an error dressed as a control: a key is bounded
by its holder allowlist regardless of scope, so the widest a scope can reach is
what that holder already has. Nothing could escalate through it, and refusing
it only stopped legitimate edits while surprising everyone who tried.

Stored as no limit, which is what it means.


## v0.9.12 — 2026-09-12

v0.9.12 -- a key scope of "*" is refused

"*" is the correct unrestricted value in an ALLOWLIST. In a key SCOPE, which is
a narrowing, it means "narrow to everything" -- a no-op that reads as a
restriction and leaves the row looking configured.

So the value a reader reaches for is the one that silently removes the
protection. That happened: two consumer keys scoped to named registries were
reset to unrestricted through the console, and it was found by a consumer
measuring their own access and reporting that they could reach a private
registry their scope excluded.

Refused now at the API and in the page, including when mixed with real patterns.
An empty list is still accepted -- the guard is against a wildcard PRETENDING to
be a narrowing, not against widening on purpose, which now asks for confirmation
and names what it is removing.

Anyone exposing the console to people who also administer allowlists should
upgrade. The failure is silent and in the permissive direction.


## v0.9.11 — 2026-09-12

v0.9.11 -- key rules narrow instead of widening

A key's effective rules were the UNION of its principal's allowlist and its own.
Union only widens, so a key could be granted more than its holder and never
less. A narrower credential was inexpressible -- which meant every distinct
scope needed its own principal, and machine consumers ended up in the user list
as if they were people, because a principal was the only thing a scope could
hang on.

Principal rules are now the GRANT; key rules are a NARROWING; a request needs
both to permit it.

  empty GRANT       grants nothing      (unchanged)
  empty CONSTRAINT  constrains nothing  (new, and the opposite on purpose)

Strictly more restrictive, so nothing is widened by upgrading, and existing keys
carry no scope and behave exactly as before.

What it buys: a holder can narrow their own key (PUT /_console/keys/{id}/scope),
which is safe only because a scope subtracts -- under the union that endpoint
would have been a way to grant yourself authority. A credential for one job no
longer needs an account invented to hold its scope.

Refusals now name which list refused: "scoped away from" versus "no rule
granting". Same status, different fix.


## v0.9.10 — 2026-09-12

v0.9.10 -- revocation now works when the write came from another process

A key disabled by anything other than the running server KEPT AUTHENTICATING
until that server restarted. A key minted the same way did not work at all.

The read path is an in-memory cache and invalidation only dropped it inside the
writing object, so a migration script, an operator, or a one-shot exec committed
to SQLite and the server never noticed. Observed against a live service: two
freshly minted keys returned 401 while a key disabled seconds earlier returned
200. A restart inverted both.

Freshness is now checked against the database. `PRAGMA data_version` changes
when another connection commits, answered from an open handle with no I/O. The
cache is still a cache -- unchanged data is not reloaded.

Anyone administering an authorisation store out of band should upgrade. The
failure is silent in the direction that matters: the revocation appears to
succeed and the credential keeps working.

ALSO: users can be deleted. The console could grant admin and disable an
account, but a principal created in error stayed in the list forever. Deletion
cascades to keys and allowlist, and refuses on the last administrator or on
yourself.


## v0.9.9 — 2026-09-12

v0.9.9 -- the Hugging Face surface can require a credential, and never forwards yours

Until now only /v2 was authenticated. The Hugging Face catch-all -- every model
and dataset byte, and the larger surface by far -- had no gate at all, and
neither did FastAPI's /docs, /redoc and /openapi.json. On a private cache that
is fine. On a public one it is the whole service.

  XHC_HF_AUTH=key   require a key from XHC_AUTHZ_DB on the HF surface
  XHC_DOCS=0        switch off the interactive API docs

Both opt-in; a deployment that changes no variables behaves exactly as before.

The key is presentable as `Bearer <key_id>:<secret>`, so a user sets HF_TOKEN to
that and Hugging Face's own tooling works unchanged -- it has no concept of a
username. Basic works too. The web root stays public, because a homepage nobody
can load is not a homepage.

THE FIX THAT MATTERS MOST IS NOT THE GATE. Upstream requests applied the cache's
own Hub token only when the client had not sent one. So a client authenticating
to the cache -- which the tooling can only do by sending a token -- would have
had THEIR CREDENTIAL FORWARDED TO HUGGING FACE, and the request would then fail
there, because a cache key is not a Hub token.

The same line had a quieter failure that predates this release: a user with
their own HF_TOKEN caused ingest to be authorised as them rather than as the
cache. Which credential a shared cache presented upstream depended on whoever
asked first.

The cache now authenticates to the Hub as itself, always -- matching what the
/v2 surface already did with registry credentials.

Consequence, stated so nobody discovers it: you cannot reach a gated repository
through this cache by supplying your own entitlement. The cache fetches what the
cache can fetch. For shared storage that is the correct answer, because anything
fetched is then served to everyone whose rules cover the path.


## v0.9.8 — 2026-09-12

v0.9.8 -- per-key authorization gains a browser front end, and three holes behind it

Muninn can now be run as a SHARED cache: a credential is a key with rules
rather than a line in a flat htpasswd, users manage their own keys through a
login on the cache's own homepage, and an administrator decides what each
user's keys may do.

All of it is opt-in and unset by default. A deployment that upgrades to this
release and changes no environment variables behaves exactly as before: no
login routes are mounted, no management surface exists, and client auth is the
same htpasswd gate.

  XHC_AUTHZ_DB       per-key push/pull authorisation (SQLite: principals, keys, rules)
  XHC_OIDC_ISSUER    browser login; requires client id, secret, redirect URI,
                     session secret and XHC_AUTHZ_DB, and REFUSES TO START without them
  XHC_METRICS_AUTH   `token` gates /metrics behind XHC_MANAGE_TOKEN
  XHC_WEB_ROOT       serve a homepage from the same hostname as the cache

Rules are patterns over <upstream>/<repository>, allow-only, no precedence, and
an empty list grants nothing. `*` spans `/`, so a single `*` with both verbs is
unrestricted. Patterns never match the tag.

THE PART WORTH READING: three defects this release fixes, none of which
produced an error, a log line or a failing test before it was looked for.

1. A DISABLED CREDENTIAL COULD STILL LOG IN. Authorisation refused a disabled
   key for every operation naming a repository, so pulls and pushes were never
   at risk -- but /v2/ names no repository, never reaches that check, and is
   the endpoint `docker login` calls. A revoked user got "Login Succeeded" and
   learned about the revocation on their next pull. Authentication now refuses
   a disabled key or a disabled principal.

2. AN UPLOAD SESSION WAS NOT BOUND TO THE KEY THAT OPENED IT. A push is four
   requests and only the first is authorised against a repository; the PATCH
   and PUT that follow carry a session uuid and write to the repository the
   SESSION names. Any authenticated caller who learned a uuid could finish
   someone else's upload into a repository they had been refused. Sessions now
   record their opener, and a mismatch is reported identically to a uuid that
   does not exist.

3. A REVOCATION COULD SUCCEED WITHOUT REVOKING ANYTHING. Disabling a principal
   ran a bare UPDATE, so a mistyped subject returned success having changed no
   rows -- the operator's evidence that a revocation happened was a 200 from a
   statement that touched nothing.

Each was found by a test asserting the effect ON THE WIRE rather than the
management API's own response. Asserting the response would have been green for
all three, and would have been testing that a write happened rather than that
the thing it claims stopped working actually stopped.

Documentation changed WITH the code, which is the point: the README previously
argued per-client authorization was impossible here, with a correct argument
about an implementation that no longer holds. The replacement leads with the
LIMITS, because the feature overstates itself without them -- it covers /v2
only and not the Hugging Face path, and allowing a path grants whatever is
already cached under it, because the cache holds no per-tenant copies and never
contacts the upstream on a hit. One Muninn does not separate tenants who must
not read each other's private images.

The example homepage ships a login button and a key-management console, with no
framework and no CDN: a cache that exists so machines need not reach the
internet should not need the internet to draw its own homepage, and on a public
hostname every external subresource is a third party collecting visitor IPs.
Eight guards over that page were each made to go red deliberately before being
kept.


## v0.9.7 — 2026-09-12

XHC_WEB_ROOT -- one hostname can be a homepage AND a cache.

Point it at a directory and static files are served at /. Unset by default, so
nothing changes for an existing deployment.

WHY IN MUNINN RATHER THAN IN A REVERSE PROXY

The OCI surface is bounded under /v2 by spec, so a proxy can split docker traffic
from a homepage. It cannot split the Hugging Face surface: HF clients construct
arbitrary top-level paths like /owner/repo/resolve/main/config.json, so there is
no prefix to match on.

Muninn can, because it already knows which paths are HF paths. The discriminator
is a PRECEDENCE RULE rather than a pattern:

  if a file exists under the web root, serve it; otherwise fall through to HF.

That makes "falls through" the property the cache depends on, and it has its own
test.

THE WEB ROOT'S CONTENTS ARE A CLAIM ON THOSE PATHS

A directory named models/ or datasets/ in there would silently shadow real HF
traffic, and the symptom would be "the cache stopped working" rather than "a file
was served". Keep it to a homepage and its assets.

CONTAINMENT IS ENFORCED BY RESOLUTION, NOT BY STRING COMPARISON

A prefix check on the raw request path is the classic bypass: `..` and symlinks
both defeat it. The candidate is fully resolved and then tested for containment,
so a symlink pointing out of the root fails the same check as ../../etc/passwd
without being special-cased. Verified by weakening the guard to a prefix check,
which turns four tests red -- one of them showing a file outside the root being
served.

A configured-but-missing root warns and serves nothing rather than raising. A typo
in one setting must not take down a cache whose main job is unrelated.

ORDERING IS NOW A SECURITY PROPERTY AND IS PINNED

/v2, /healthz, /metrics and /_cache are mounted before the catch-all and cannot be
shadowed by a web root. That was previously an implementation detail; a test now
asserts it, because reordering the mounts would let a web root answer the registry
surface or the unauthenticated health endpoint with nothing else noticing.

Unauthenticated by design: the client-auth gate is on /v2 only. A homepage is
public. Do not put anything there that is not.


## v0.9.6 — 2026-09-12

A SECURITY WARNING'S REMEDIATION CLAUSE NAMED A SETTING THAT DOES NOTHING ALONE.

The push-through warning, logged at boot whenever push is enabled, told the
operator to "set XHC_DOCKER_HTPASSWD to require a credential". The README said
the same. The file alone is ignored: client auth defaults to none and the loader
returns before ever opening it, so XHC_DOCKER_AUTH=basic is also required and
neither sentence mentioned it.

Measured by a peer rather than read. With a valid bcrypt htpasswd mounted and
XHC_DOCKER_AUTH unset, GET /v2/ with no credentials returned 200 and the boot log
said nothing about auth at all. So an operator does exactly what the security
warning says, restarts, sees no error, and serves an unauthenticated
push-through cache. The file is never opened, so a malformed one would not have
complained either.

THE ASYMMETRY WAS THE BUG, AND IT SAT THREE LINES ABOVE THE EARLY RETURN

That loader's own docstring says resolving unknown to permissive is what disarmed
pin protection, and that it must not be possible to lose a password file and
silently return to an open cache. The code honoured that in ONE direction --
auth=basic with no file refuses to start -- and was silent in the other.

One case is "you asked to be closed and cannot be". The other is "you look like
you asked to be closed and did not", and it is the one an operator reaches by
following this project's own instructions. Only the first was guarded.

WHAT CHANGED

Both strings name both variables, and the README says the file alone is ignored
and why. The loader now warns when a credential file is set while auth is none,
stating that the file is IGNORED and that /v2/* is UNAUTHENTICATED.

There was already a good positive control -- "client auth enabled for N user(s)
on /v2/*" -- and no negative one. So silence meant both "correctly open" and
"you tried to close it and failed", which are the two states an operator most
needs told apart.

Two tests, and the second matters as much as the first. One asserts the warning
fires and names both the current state and the setting that changes it, verified
able to fail by deleting the warning. The other asserts it does NOT fire when
neither is set, because a warning on every default deployment is how a real
signal gets trained out of a log.

NOT TAKEN HERE, AND RECORDED AS A DECISION

Whether that combination should REFUSE to start, symmetric with its sibling and
with the docstring's own principle, is a real trade rather than an obvious
improvement: refusing turns a stale environment variable into an outage on
upgrade, for a live service other teams deploy. The warning closes the
information gap without that risk.

No behaviour change to the auth path itself. An operator who had working auth
still has it; an operator who thought they did now finds out at boot.


## v0.9.5 — 2026-09-06

SNAPSHOT INGEST IS VERIFIED TOO.

v0.9.3 verified the single-file ingest path and left the snapshot path trusting
whatever landed -- so everything a prewarm brought in was in exactly the state
every HF file used to be in. A prewarm is the primary path into this cache, so
that was the larger exposure of the two, not the smaller one.

snapshot_download offers no per-file hook, so verification runs once the tree
has landed. Mismatched blobs are already deleted by then; failing the job is
what stops the rest being treated as a good prewarm.

TWO PROPERTIES THAT KEEP IT FROM BECOMING THE WRONG CHECK

Deduplicated by inode. The Hugging Face layout points many snapshot entries at
one blob, so hashing per ENTRY would repeat the same work precisely on the
repos where content is shared -- the normal case, not an edge one.

Skips blobs that were not written on this run, using the same mtime filter the
byte accounting already uses and for the same reason: a repeat prewarm must not
re-hash the half it already had.

That second property also fixes what this check IS. It is an INGEST check, not
a scrub. On-disk rot in a blob nobody re-fetched is a different problem and is
not covered, and calling this a scrub would be the adjacent-measure mistake --
a check that is true, green, and not a test of the thing its name implies.

Costs about 0.6s per GB actually fetched, on the same measurement that put
sha256 at roughly 8.8x the rate bytes arrive.


## v0.9.4 — 2026-09-06

A CORRECTION TO WHAT v0.9.3 CLAIMED ABOUT ITSELF. No behaviour change.

v0.9.3 shipped byte verification for HF ingest and said, in the README, in its
own tag message and in the decision record, that "the Xet download path is not
covered by this check and has not been measured here."

The second half was true. THE FIRST HALF WAS WRONG.

Verification runs on the file after the download returns, so it hashes whatever
landed regardless of which transport delivered it. Measured against the real
Hub: the download takes the Xet path -- xet_get called, http_get not -- the
blob's filename is the sha256 of the bytes, and verification returns VERIFIED.

So the guarantee is BROADER than v0.9.3 claimed. Both transports are covered.

HOW THE WRONG SENTENCE HAPPENED, because it is the more useful half.

I asserted a limitation of my own code without running it. A limitation is a
negative claim, and negative claims get accepted without evidence -- so a
one-command check went unrun and the sentence propagated to three documents in
the session where the same rule was being written down for something else.

It understated the guarantee. That is the direction that gets least scrutiny:
nobody audits a claim whose author is being modest, and an overstated limitation
reads as diligence. It is the mirror of severity inflation in a confession.

Note also that the first probe pointed the wrong way. A manual HEAD against the
resolve URL showed no Xet headers on any candidate file, which looked like
evidence the path was not used. The library's own request differs from a
hand-rolled one, so the probe answered a question about my probe. Observing
which branch the REAL client took settled it -- the same lesson as reading a
repeated header with a client that only sends one.

WHAT REMAINS UNMEASURED, stated precisely so it is not overstated again: whether
hf_xet independently detects a corrupt chunk during reconstruction. That is
defence in depth, not coverage. A corrupt reconstruction fails the post-ingest
hash whatever hf_xet did or did not notice.

tests/test_xet_path_is_verified.py asserts both halves together -- the transport
taken AND the verification result -- because asserting only the second would
pass identically on the plain HTTP path and prove nothing about Xet. It skips
rather than passes when the Hub is unreachable, because a test that quietly
passes offline restores the unverified claim it exists to prevent. The negative
control is a single bit-flip: same length, so no length check could see it.


## v0.9.3 — 2026-09-06

Both protocols are content-addressed. Only one of them checked.

THE HF PATH TRUSTED THE UPSTREAM ETAG AND NEVER VERIFIED BYTES

An OCI blob is hashed as it is ingested and refused if it does not match its
digest. A Hugging Face file was written under whatever ETag the Hub declared and
nothing recomputed it -- the blob's filename IS that ETag, inherited from
huggingface_hub's on-disk layout. So a corrupt upstream response was cached
faithfully and re-served forever, and every check here stayed green, because
they all key on the same ETag that was never independently checked.

The integrity check and the corrupt source shared their only reference.

MEASURED FIRST, AND IT WAS WORSE IN ONE DIRECTION THAN ASSUMED

Driven end to end against a local stand-in for the Hub, because the whole
download belongs to huggingface_hub rather than to code here and mocking its
internals would have measured a model of the library instead of the library.

  - bytes contradicting the ETag are cached silently when the length matches
  - an UNDER-declared Content-Length truncates the file and is accepted, with no
    error at all: the client reads exactly the declared count and discards the
    remainder, so the consistency check compares that count against itself and
    can never fire in this direction

The refusal that did exist -- more bytes declared than sent -- comes from the
HTTP transport as an IncompleteRead, one layer below huggingface_hub's own
consistency check. That is a property of the connection and is not integrity
checking. Length guards against a sender that DROPS, not against one that lies.

BOTH ARE CLOSED BY ONE MECHANISM

Each ingested HF file is hashed against its ETag and refused on a mismatch,
which deletes the blob rather than leaving a file whose NAME asserts a digest
its bytes do not have. Truncated bytes fail the hash too.

UNVERIFIABLE IS NOT VERIFIED

The Hub returns a sha256 for LFS files and a git object id for the rest. Only
the former can be checked, and a file that cannot be checked is counted
UNVERIFIABLE rather than passed off as verified. An unreadable blob refuses for
the same reason. Collapsing "could not check" into "checked" is the same
fail-open as an unreadable state file reading as an empty one.

muninn_ingest_verify_total{result=VERIFIED|UNVERIFIABLE|MISMATCH}, all three
seeded at zero so a zero means zero and a gap means the process was down.

metrics.reset() now re-seeds inside the lock. It cleared without seeding, so a
reset left every series absent rather than zero -- destroying the distinction
the seeding exists to preserve. Found by a test, not by reading the code.

DEFAULT ON, AND THE COST WAS MEASURED RATHER THAN ASSUMED

sha256 runs about 8.8x faster than bytes arrive from upstream on the host this
was measured on. XHC_HF_VERIFY=0 turns it off. Measure it on your own hardware
before assuming the ratio holds there.

TWO LIMITS, DOCUMENTED RATHER THAN LEFT TO BE DISCOVERED

Under the stream miss policy the first caller may already have received the bad
bytes; this stops a bad blob being KEPT and cannot retract what was sent. Use
wait if that matters more than first-byte latency.

The Xet download path is NOT covered by this check and has not been measured
here. It reconstructs from content-addressed chunks and is likely sound by
construction -- but that is a reading of someone else's code rather than a
measurement, and it is marked as such wherever it appears.


## v0.9.2 — 2026-09-02

A pull failed with 404 through the cache while working directly against the same
registry. Neither auth nor, at root, a status-mapping problem.

SIX OF SEVEN ACCEPT VALUES WERE SILENTLY DROPPED

HTTP allows a repeated header. Starlette's Headers.get() returns only the FIRST
occurrence, so reading Accept that way discards the rest and says nothing.

regctl sends SEVEN separate Accept lines -- both OCI types, four docker types,
the OCI artifact type. This cache forwarded the first alone. A registry holding
a DOCKER manifest list then has nothing acceptable to return and answers

    400 MANIFEST_INVALID: Schema 2 manifest not supported by client

a content-negotiation refusal entirely of the cache's making, which was then
rendered to the client as 404. The client was told the image does not exist when
the REQUEST was the problem, and 404 is the answer most likely to stop someone
looking.

WHY EVERY HAND-RUN PROBE PASSED: curl and docker send ONE comma-joined Accept
line, so nothing is dropped. Only a client using repeated headers loses values,
so the defect was invisible to exactly the checks anyone would reach for -- and
invisible from both ends, since the client saw a plausible answer about the image
and the registry saw a request that genuinely did not accept what it held.

AN UPSTREAM STATUS IS NO LONGER RELABELLED AS "NOT FOUND"

400 and 406 pass through as a content-negotiation failure naming the cause, 429
passes through, 5xx becomes 502, and a genuine upstream 404 stays 404. A 400 is
an answer about the REQUEST; 404 is an answer about the RESOURCE; neither is
"the cache is broken".

This is the third instance in three releases of distinct states collapsed into
one rendering -- after every upstream 401 becoming 502, and _fetch_token
returning None for five different failures. Same shape, different layer each
time.

Verified end to end against a real client rather than only in unit tests,
because every test that did not use one passed throughout.


## v0.9.1 — 2026-09-02

Four fixes, and three of them are the same defect: distinct states collapsed
into one rendering, so a reader was told the wrong thing about what happened.

A MISTYPED GHCR IMAGE NAME ACCUSED THE INFRASTRUCTURE TOO

v0.9.0 fixed that for Docker Hub and not for ghcr, because the two registries
refuse in different places. Docker Hub issues an anonymous token for a repo that
does not exist and then 401s the request carrying it; ghcr refuses at the token
endpoint, so the cache never authenticates and the earlier branch could not
fire. Same user error, same user-visible failure, and the diagnosis differed by
which registry the name was mistyped against.

The cause was one layer down and in the same file: _fetch_token returned None
for five distinct states -- no realm, an unusable realm, a transport error, a
non-200 from the token endpoint, and a 200 with no token -- so the caller could
not tell an ANSWER from NO ANSWER. It now reports which. A 401/403 from the auth
service with no credentials held renders 404; with credentials it stays 502
rejected; a 5xx or a timeout is its own 502 naming the token endpoint.

The negative control matters more than the fix: a token endpoint returning 500
must never become 404, or an upstream outage renders as "you typed it wrong".

A FULL DISK DEGRADES THE PULL INSTEAD OF BREAKING IT

Eviction compares the cache's own size against its own budget and never consults
free space, so on a shared filesystem anything else can fill the volume while
the cache sits far under budget. Every ingest then failed MID-STREAM, after the
client already had a 2xx, as a truncated body and a digest mismatch -- and there
is no fallback for the client to take, because its image reference was rewritten
to point here, so the cache IS its registry.

Below XHC_DOCKER_MIN_FREE (default 1 GiB) a miss is now streamed straight
through and not cached, carrying `x-xhc-cache: BYPASS-NO-SPACE`. The pull gets
slower; it does not break.

It does NOT evict to make room. On a filesystem with no quota or reservation,
space freed here goes to whatever is filling the volume -- the cache would
shrink, evict again, and end small AND still failing, having destroyed warm data
to get there. An unreadable filesystem keeps caching, which is the opposite of
how pin state resolves unknown and deliberately so: the risk here is a slowdown,
not data loss.

XHC_DOCKER_TAG_TTL HAD THREE REGIMES AND EXPOSED TWO

`0` means NEVER revalidate -- the value an operator reaches for wanting the
strictest behaviour, selecting the loosest. `always` now means check every
request. `0` is unchanged because deployments rely on it, so upgrading changes
nothing. A negative value reads as `always` rather than `never`, because that is
what someone guesses when they want no caching.

The boot log prints the resolved regime in words rather than the raw number:
`tag_ttl=0s` reads like "no delay" and gave an operator no way to tell which of
the three they had.

THE DOCS GUARD ONLY COVERED CONFIG VARIABLES

Because both examples that prompted it were config variables. Routes are the
instance that got through -- the pending endpoints shipped undocumented for
three releases while that test went green, and its green is why nobody looked.
It now reads the management surface off the router. The sweep was shown to find
the historical miss before being trusted, and has a negative control, because a
sweep returning an empty set passes vacuously and looks identical to a clean one.


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

BEHAVIOURS OTHER TEAMS ASSERT ARE NOW PINNED HERE

Twice in one night a change broke something another team had published as a
check, and both times it was found by luck: /healthz's BODY is matched by a
blackbox monitoring probe rather than just its 200, and a bogus repository name
returning 404 is the line used to validate a deployment. Neither side has a
mechanism for "who asserts this behaviour", and searching the other's documents
does not scale -- it makes correctness depend on remembering to go and look.

So the assertion moves to where the change happens: a contract someone else
depends on becomes a test in the repo that can break it.

Pinned, read out of the consumer's live monitoring configuration rather than
guessed: the healthz body as their probe's own regexp matches it -- the key must
be literally `ok` and the value the JSON boolean `true`, so {"ok": 1} and
{"healthy": true} pass review and fail the probe -- an exact 200, healthz and
metrics staying unauthenticated AND served locally rather than falling through
to the Hub proxy, the durable gauge names, the path-prefix resolution rule with
all four bare-name forms, a nonexistent repo answering 404, and push defaulting
to off.

The path separation is the load-bearing one: everything that is not /v2/* or
/_cache/* proxies to the Hub, so an endpoint moved behind that catch-all would
return 200 with Hub HTML and read healthy forever.

Each guard was verified by breaking the thing it protects -- renaming the "ok"
key, flipping the push default, dropping `localhost` from the resolution rule.

Writing them also surfaced something nobody had asserted: /healthz and /metrics
share cachefs.disk_stats(), and only /healthz catches OSError. There is no
shared readiness gate, but for that one failure mode two alerts believed to be
independent have a common cause. Recorded rather than silently changed.

UNDER BUDGET IS NOT THE SAME AS HAVING ROOM

Eviction compares this cache's own size against its own budget and never
consults free space. On a shared filesystem those come apart: if anything else
on the volume fills it, the cache sits under budget, declines to evict, and
every write fails with ENOSPC while the evictor reports "under high water" --
a true statement about the wrong limit. Below 5% free, a pass that declines
because it is under budget now says so, naming which limit it is looking at and
that the space is going to something outside this cache.

Acting on it is deliberately NOT done here: freeing our own data may not recover
space consumed elsewhere, so evicting on free-space pressure can destroy warm
cache for nothing. That decision is written out rather than taken in a hurry.
muninn_disk_free_bytes already carries the true figure and is the only signal
that catches this class, since every budget-derived number reads healthy.

"RUNNING HOT" MEANS NEAR CAPACITY, NOT OUT OF SPACE

A claim nobody had measured -- "running hot on disk is recoverable" -- was true
of running near capacity and got read as "a full disk is fine", which it is not.
It spread from a code comment here into another team's alerting, where it
justified a severity that routed to null; the sentence did not mislabel those
alerts, it silenced them.

The measurement now sits next to the claim instead: an ingest failure returns
mid-stream, so the client gets a truncated body on an already-sent 2xx and sees
a digest mismatch, and there is NO fallback to upstream -- a client using this
cache has had its reference rewritten, so the cache IS its registry. Only
XHC_MISS_POLICY=redirect sends a client upstream, and it is not the default.

Nothing marked that claim UNVERIFIED anywhere. It was written down, and written
down reads as established.

KNOWN GAPS, filed not fixed: XHC_DOCKER_TAG_TTL has no value meaning "always
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
