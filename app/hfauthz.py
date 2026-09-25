"""Per-key rules on the Hugging Face surface (XHC_HF_RULES).

XHC_HF_AUTH=key answers WHO is asking. This answers whether they may have the
repository they asked for, using the same XHC_AUTHZ_DB rules as /v2 over the
`models/<repo>`, `datasets/<repo>` and `spaces/<repo>` namespace described in
authz.py -- the same shape as XHC_ALLOW_REPOS.

WHY IT MATTERS MORE HERE THAN ON /v2. The cache fetches from the Hub as itself,
with its own token, and that token has accepted gated licences and can see
private repos. Without per-key rules, every live key borrows all of it.

THE DECISION IS TAKEN ONCE, AT THE TOP OF THE CATCH-ALL, FROM THE PATH ALONE --
before hit-or-miss is known, so a hit is refused exactly as a miss is. A check on
the miss path would be absent on every hit after the first fill.

EVERY PATH GETS A DECISION. There is no "not a repo path, let it through":

  * a path that names a repository is authorised against that repository;
  * a listing or search over one repo type (`api/models?search=`) needs a grant
    covering the whole type, `models/*`, because it answers with the cache's
    Hub identity and can name private repos that identity can see;
  * anything else -- whoami, collections, papers, an endpoint added to the Hub
    next month -- needs a bare `*`. It is matched against an internal
    reference no narrower pattern can express (authz.HF_ANY_ENDPOINT).

`*` covers all three, so a `*` holder sees no change. `models/*`, `datasets/*`
and `spaces/*` together cover every repository but not the miscellany.

COUPLED TO THE HANDLERS, as /v2 couples authorisation to `_resolve_or_error`.
The catch-all's own parsers (parse_resolve, parse_repo_info_path, ...) decide
which repo a handler SERVES; this module decides which repo was AUTHORISED. If
the two ever disagree, the handler refuses: `require()` checks that the repo it
is about to serve is one this request was authorised for. A new route that
forgets to authorise fails closed instead of serving.

AMBIGUITY IS RESOLVED BY REQUIRING EVERY READING. `api/models/org/refs` is repo
`org/refs`'s info, or canonical repo `org`'s refs, and Muninn cannot know which
the Hub will choose. A path that reads both ways must be allowed both ways. The
readings come from _SUBRESOURCES: a path is read as `<a>/<b>/<sub>...` when
<sub> is a known sub-resource, as `<a>/<sub>...` when <b> is one, and when
NEITHER is -- a sub-resource this list has never heard of -- as both. So an
unknown endpoint is refused to a narrow key rather than guessed at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from . import authz, dockerauth, viewer
from .config import settings

log = logging.getLogger("xhc.hfauthz")

# Path segments that follow a repo id on the Hub, in the API and on the web.
# Taken from the URLs huggingface_hub builds, plus the web views. A segment
# missing from here does not open anything: see "AMBIGUITY" above.
_SUBRESOURCES = frozenset({
    # api/{type}s/{repo}/...
    "revision", "tree", "paths-info", "refs", "commits", "commit", "compare",
    "preupload", "xet-read-token", "xet-write-token", "discussions", "settings",
    "branch", "tag", "lfs-files", "super-squash", "likers", "like",
    "user-access-request", "auth-check", "storage", "secrets", "variables",
    "runtime", "restart", "pause", "sleeptime", "hardware", "duplicate",
    "parquet", "croissant", "treesize", "notebook", "scan", "jwt", "events",
    "logs", "metrics",
    # {type}s/{repo}/... on the web
    "resolve", "raw", "blob", "blame", "edit", "viewer", "community",
})

_TYPE_PLURALS = {"models": "model", "datasets": "dataset", "spaces": "space"}


class BadPath(ValueError):
    """A path that cannot be authorised because it cannot be read honestly."""


@dataclass(frozen=True)
class Target:
    """What a request path needs. EVERY reference must be granted."""

    references: tuple[str, ...]
    repos: frozenset[tuple[str, str]] = field(default_factory=frozenset)


def enforcing() -> bool:
    return settings.hf_auth == "key" and settings.hf_rules == "enforce"


def _segments(full_path: str) -> list[str]:
    """Split a path, refusing empty and dot segments.

    THE UPSTREAM CLIENT NORMALISES `..` BEFORE SENDING. So
    `org/allowed/resolve/main/../../../org/secret/...` would be authorised as
    `org/allowed` and fetched as `org/secret`. Starlette has already decoded
    `%2E%2E` to `..` by the time the path arrives here, so checking the decoded
    segments covers the encoded forms too. An empty segment (`org//secret`) is
    refused for the same reason: what it means depends on who normalises it.
    """
    segs = full_path.split("/")
    if segs and segs[-1] == "":
        segs = segs[:-1]  # one trailing slash is just a trailing slash
    if full_path and any(s in ("", ".", "..") for s in segs):
        raise BadPath("path contains an empty, '.' or '..' segment")
    return segs


def _readings(segs: list[str]) -> list[str]:
    """Every repo id the Hub could read from segments following the type prefix."""
    if len(segs) == 1:
        return [segs[0]]
    subs = _SUBRESOURCES | viewer.cacheable_endpoints()
    two = len(segs) == 2 or segs[2].lower() in subs
    one = segs[1].lower() in subs
    out = []
    if two or not one:
        out.append(f"{segs[0]}/{segs[1]}")
    if one or not two:
        out.append(segs[0])
    return out


def _repos(repo_type: str, ids: list[str]) -> Target:
    return Target(
        references=tuple(authz.hf_reference(repo_type, i) for i in ids),
        repos=frozenset((repo_type, i.lower()) for i in ids),
    )


def _listing(repo_type: str) -> Target:
    return Target(references=(authz.hf_reference(repo_type),))


_SURFACE = Target(references=(authz.HF_ANY_ENDPOINT,))


def classify(full_path: str, dataset_params: list[str]) -> Target:
    """Map a catch-all path to the references it must be granted.

    `dataset_params` is the request's `dataset` query values, which is where the
    datasets-server proxy carries its repo id.
    """
    segs = _segments(full_path)
    if not segs:
        return _SURFACE
    head = segs[0].lower()

    if head == "api":
        rest = segs[1:]
        if rest and rest[0].lower() in _TYPE_PLURALS:
            repo_type = _TYPE_PLURALS[rest[0].lower()]
            return _repos(repo_type, _readings(rest[1:])) if rest[1:] else _listing(repo_type)
        return _SURFACE

    if head == viewer.DS_SERVER_PREFIX.rstrip("/"):
        if not dataset_params:
            return _listing("dataset")
        for ds in dataset_params:
            parts = ds.split("/")
            if len(parts) > 2 or any(p in ("", ".", "..") for p in parts):
                raise BadPath(f"dataset {ds!r} is not a repository id")
        return _repos("dataset", list(dataset_params))

    if head in ("datasets", "spaces"):
        repo_type = _TYPE_PLURALS[head]
        return _repos(repo_type, _readings(segs[1:])) if segs[1:] else _listing(repo_type)
    if head == "models" and len(segs) == 1:
        return _listing("model")
    return _repos("model", _readings(segs))


def _header_safe(text: str) -> str:
    """A path is attacker-chosen and arrives decoded; a newline in it must not
    become a header line, and a non-latin-1 character must not crash the send."""
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else "?" for ch in text)


def refusal(reason: str, reference: str) -> Response:
    """403 GatedRepo, naming the key and the repository.

    403 and not 404: a 404 sends the user hunting for a typo in a repo id that is
    correct. GatedRepo because that is precisely the situation -- the repo
    exists and this credential is not on its access list -- and because it is
    the code huggingface_hub RE-RAISES from the HEAD every download starts with.
    A 403 without it is swallowed there into "check your connection", which is
    the least useful thing a refusal can say.

    THE REASON GOES ON THE WIRE, unlike /v2, whose 403 has no body: the docker
    CLI prints only the status, so a body there reaches nobody, while
    huggingface_hub prints X-Error-Message. The reason describes only the
    caller's own key -- never granted, or scoped away -- which its holder can
    already read in the console. It never lists rules.
    """
    message = _header_safe(f"refused by this cache's rules: {reason}")
    return JSONResponse(
        {"error": message,
         "hint": f"an administrator can grant it with a rule matching '{reference}', "
                 "e.g. 'models/<org>/* pull'"},
        status_code=403,
        headers={"x-error-code": "GatedRepo", "x-error-message": message,
                 "x-xhc-authz": "denied"},
    )


def authorize(request: Request, full_path: str) -> Response | None:
    """Decide one request. None means allowed. Records what was authorised."""
    if not enforcing():
        return None
    key = getattr(request.state, "authz_key", None)
    if key is None:
        # authenticate_hf runs first and sets this; reaching here without it
        # means something skipped authentication. Not "proceed".
        return dockerauth.hf_unauthorized()
    try:
        target = classify(full_path, request.query_params.getlist("dataset"))
    except BadPath as exc:
        log.info("hf authz: refusing %r: %s", full_path, exc)
        return JSONResponse({"error": _header_safe(str(exc))}, status_code=400)
    for reference in target.references:
        allowed, reason = authz.decide(key, "pull", reference, "hf")
        if not allowed:
            log.info("hf authz deny: %s", reason)
            return refusal(reason, reference)
    request.state.hf_authorised_path = full_path
    request.state.hf_authorised_repos = target.repos
    return None


def require(request: Request, repo_type: str, repo_id: str) -> Response | None:
    """Refuse unless THIS repo was authorised for this request. None means go on.

    Called by every handler that serves a repo, with the repo it is about to
    serve -- which it got from its own parser, not from classify(). Agreement is
    the normal case; disagreement means the two parsers read one path two ways,
    and the answer then is no.
    """
    if not enforcing():
        return None
    repos = getattr(request.state, "hf_authorised_repos", None)
    if repos is not None and (repo_type, repo_id.lower()) in repos:
        return None
    reference = authz.hf_reference(repo_type, repo_id)
    log.error("hf authz: %s served without being authorised (repos=%s)", reference, repos)
    return refusal(f"this request was not authorised for {reference}", reference)


def require_path(request: Request, full_path: str) -> Response | None:
    """The same guarantee for handlers that forward a path rather than a repo."""
    if not enforcing():
        return None
    authorised = getattr(request.state, "hf_authorised_path", None)
    if authorised is not None and authorised.strip("/") == full_path.strip("/"):
        return None
    log.error("hf authz: %r forwarded without being authorised", full_path)
    return refusal("this request was not authorised", authz.HF_ANY_ENDPOINT)
