"""Optional, rule-gated writes toward the Hugging Face Hub (XHC_HF_WRITES).

OFF BY DEFAULT, and off is the read-only surface of v0.9.18 unchanged: every
write is a local 405 from hfcompat, and nothing in this module runs.

WHAT `on` MEANS. A NAMED set of repository writes -- the ones `huggingface_hub`
makes to push content -- is forwarded upstream with the CACHE'S Hub token, each
only when the caller's rules grant it on the target repository. Everything else
that writes stays a 405 in every mode: Space controls, discussions, settings,
collections, webhooks, jobs, inference endpoints. Those are not "pushing to a
repository", and a rule grammar about repositories cannot describe them.

EVERY FORWARDED WRITE APPEARS ON THE HUB AS THE CACHE'S ACCOUNT. The Hub never
learns which key asked. The audit line this module logs is the ONLY record of
who did it, which is why it is at INFO and names key, principal, repository and
whether the write deleted anything -- and why the cache's token scopes are the
outer bound of everything a rule can grant.

THE GRANTS, via hfauthz.decide_write -> authz.decide(..., "hf"), the same
decision point as a pull, against the same references hfauthz.classify makes:

    push              every write below
    push + delete     a DESTRUCTIVE write: repo delete, branch delete, tag
                      delete, super-squash, LFS purge, a repo move (on its
                      source), and a commit containing deletedFile or
                      deletedFolder

The destructive endpoints, from huggingface_hub 0.34.4 (hf_api.py line numbers
of the request each makes):

    DELETE api/repos/delete                          delete_repo            3818
    POST   api/repos/move                            move_repo              4016
    DELETE api/{type}s/{repo}/branch/{branch}        delete_branch          6048
    DELETE api/{type}s/{repo}/tag/{tag}              delete_tag             6171
    POST   api/{type}s/{repo}/super-squash/{branch}  super_squash_history   3498
    POST   api/{type}s/{repo}/lfs-files/batch        permanently_delete_lfs_files 3623
    POST   api/{type}s/{repo}/commit/{rev}           create_commit          4330
           ...when a line's key is deletedFile/deletedFolder (_commit_api.py 872)

Destructive calls this module does NOT forward at all, so they stay 405 whatever
the grants: delete_space_secret (7043), delete_space_variable (7134),
delete_space_storage (7533), delete_collection (8524), delete_collection_item
(8710), delete_webhook (9538), delete_inference_endpoint (8125), cancel_job
(10262), unlike (2444), and the discussion changes that close, hide, edit or
merge (_post_discussion_changes, 6552; merge_pull_request writes to a branch).

COMMITS ARE INSPECTED, because file deletion is a commit POST and not an HTTP
DELETE. The body is NDJSON (application/x-ndjson), one operation per line, keyed
header | file | lfsFile | deletedFile | deletedFolder (_commit_api.py 836-890).
The body is read as it streams, each line checked as it completes, up to
XHC_HF_WRITE_MAX_BODY; over that it is a 413 and NOT forwarded. A line that is
not strict JSON, has a duplicated key, or names an operation not in that list
is a 400: an operation this module cannot classify is not assumed harmless. The
bytes forwarded are exactly the bytes inspected -- one buffer, read once.

LFS AND XET UPLOADS. The bytes of a large file never travel in the commit:

    POST {repo}/preupload/{rev}                  through Muninn, push
    POST {repo}.git/info/lfs/objects/batch       through Muninn, push
    PUT  <upload href from the batch response>   DIRECT to the storage URL the
                                                 Hub returned (a presigned
                                                 object-store URL): Muninn
                                                 never sees these bytes
    POST {repo}.git/info/lfs/objects/verify      through Muninn, push -- when
                                                 the Hub returns a Hub URL,
                                                 which huggingface_hub rewrites
                                                 to the cache endpoint
    GET  {repo}/xet-write-token/{rev}            through Muninn, push; the CAS
                                                 upload that follows goes
                                                 direct to the xet service
    POST {repo}/commit/{rev}                     through Muninn, inspected

So the gate on LFS and xet bytes is the batch call (or the xet write token),
which is authorised per repository; the bytes themselves bypass the cache. That
is the protocol, not a gap Muninn can close: the Hub hands the client a URL
signed for one object. What lands in the repository is still only what a
commit names, and the commit is gated here. A multipart completion or any other
URL the Hub returns that is NOT under `{repo}.git/info/lfs/` is not forwarded
(405): it names no repository, so there is nothing to authorise it against.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import authz, hfauthz, metrics, refs
from .config import settings

log = logging.getLogger("xhc.hfwrites")

_TYPE_PLURALS = {"models": "model", "datasets": "dataset", "spaces": "space"}


def enabled() -> bool:
    return settings.hf_writes == "on"


@dataclass(frozen=True)
class Endpoint:
    kind: str
    destructive: bool = False
    # Where the target repository comes from: the PATH, the request BODY (repo
    # create/delete/move), or NOWHERE (validate-yaml).
    target: str = "path"
    # The body is NDJSON commit operations and is inspected line by line.
    commit: bool = False


# (method, sub-resource) -> endpoint, for api/{type}s/{repo}/{sub}/...
# `tail` says what may follow the sub-resource: "any" (non-empty) or an exact list.
_REPO_ENDPOINTS: dict[tuple[str, str], tuple[Endpoint, object]] = {
    ("POST", "commit"): (Endpoint("commit", commit=True), "any"),
    ("POST", "preupload"): (Endpoint("preupload"), "any"),
    ("POST", "branch"): (Endpoint("create_branch"), "any"),
    ("DELETE", "branch"): (Endpoint("delete_branch", destructive=True), "any"),
    ("POST", "tag"): (Endpoint("create_tag"), "any"),
    ("DELETE", "tag"): (Endpoint("delete_tag", destructive=True), "any"),
    ("POST", "super-squash"): (Endpoint("super_squash", destructive=True), "any"),
    ("POST", "lfs-files"): (Endpoint("permanently_delete_lfs_files", destructive=True),
                           ["batch"]),
    # A write credential for the xet CAS service. A GET, but it is what lets a
    # client upload xet bytes as this cache's account, so it needs push.
    ("GET", "xet-write-token"): (Endpoint("xet_write_token"), "any"),
}

_ACCOUNT_ENDPOINTS: dict[tuple[str, str], Endpoint] = {
    ("POST", "repos/create"): Endpoint("create_repo", target="body"),
    ("DELETE", "repos/delete"): Endpoint("delete_repo", destructive=True, target="body"),
    ("POST", "repos/move"): Endpoint("move_repo", destructive=True, target="body"),
    # Called by create_commit before committing a README.md (hf_api.py 4232).
    # Stateless validation; it names no repository.
    ("POST", "validate-yaml"): Endpoint("validate_yaml", target="none"),
}

# {type-prefix}{repo}.git/info/lfs/objects/{batch|verify}. Models have no prefix
# (huggingface_hub REPO_TYPES_URL_PREFIXES). `.git` ends the repo id, so this
# shape is unambiguous where the api/ paths are not.
_LFS = re.compile(
    r"(?:(?P<type>datasets|spaces)/)?(?P<repo>[^/]+(?:/[^/]+)?)\.git/info/lfs/objects/"
    r"(?P<op>batch|verify)"
)


@dataclass(frozen=True)
class Write:
    endpoint: Endpoint
    repo_type: str | None = None
    # Every repo id the PATH can be read as (hfauthz._readings). All must be
    # granted -- the same rule a pull follows.
    repo_ids: tuple[str, ...] = ()


def match(method: str, full_path: str) -> Write | None:
    """The write this request is, or None if it is not one Muninn forwards.

    None for a write method means 405, exactly as with writes off.
    """
    try:
        segs = hfauthz.segments(full_path)
    except hfauthz.BadPath:
        return None
    if not segs:
        return None
    if method == "POST" and (m := _LFS.fullmatch("/".join(segs))):
        repo_type = _TYPE_PLURALS.get(m.group("type") or "models")
        return Write(Endpoint(f"lfs_{m.group('op')}"), repo_type, (m.group("repo"),))
    if segs[0] != "api" or len(segs) < 2:
        return None
    account = _ACCOUNT_ENDPOINTS.get((method, "/".join(segs[1:])))
    if account is not None:
        return Write(account)
    plural = segs[1].lower()
    if plural not in _TYPE_PLURALS or len(segs) < 4:
        return None
    rest = segs[2:]
    readings = hfauthz._readings(rest)
    found: set[Endpoint] = set()
    for repo_id in readings:
        n = repo_id.count("/") + 1
        if len(rest) <= n:
            return None  # one reading is the repo itself, not a sub-resource
        entry = _REPO_ENDPOINTS.get((method, rest[n].lower()))
        if entry is None:
            return None
        endpoint, tail = entry
        after = rest[n + 1:]
        if (tail == "any" and not after) or (tail != "any" and after != tail):
            return None
        found.add(endpoint)
    # Two readings that name different endpoints are refused rather than
    # resolved: authorising one and forwarding the other is the bug this avoids.
    if len(found) != 1:
        return None
    return Write(found.pop(), _TYPE_PLURALS[plural], tuple(readings))


# ---------------------------------------------------------------------------
# Refusals. Each carries the metric result and a reason for the audit line.
# ---------------------------------------------------------------------------


class _Refused(Exception):
    def __init__(self, response: Response, result: str, reason: str) -> None:
        super().__init__(reason)
        self.response = response
        self.result = result
        self.reason = reason


def _invalid(reason: str, status: int = 400) -> _Refused:
    msg = hfauthz._header_safe(reason)
    return _Refused(JSONResponse({"error": msg}, status_code=status,
                                 headers={"x-error-message": msg}), "invalid", reason)


def _too_large(limit: int) -> _Refused:
    reason = (f"write body larger than XHC_HF_WRITE_MAX_BODY ({limit} bytes); not "
              "forwarded, because a body Muninn has not inspected is not forwarded")
    return _Refused(JSONResponse({"error": reason}, status_code=413,
                                 headers={"x-error-message": reason}), "too_large", reason)


def _denied(reason: str, reference: str, verbs: str) -> _Refused:
    return _Refused(hfauthz.write_refusal(reason, reference, verbs), "denied", reason)


def _check(key: authz.Key | None, needs: list[tuple[authz.Operation, str]]) -> None:
    allowed, reason, reference = hfauthz.decide_write(key, needs)
    if not allowed:
        verbs = "pull+push+delete" if any(op == "delete" for op, _ in needs) else "pull+push"
        raise _denied(reason, reference, verbs)


# ---------------------------------------------------------------------------
# Body reading and inspection.
# ---------------------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object with a repeated key is refused. Parsers disagree on which
    copy wins, and `{"key":"file","key":"deletedFile"}` must not be read one way
    here and the other way upstream."""
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _refuse_constant(name: str) -> object:
    raise ValueError(f"non-standard JSON constant {name}")


def _strict_json(raw: bytes) -> object:
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates,
                      parse_constant=_refuse_constant)


# create_commit's operation keys (_commit_api.py 836-890) and whether each is a
# deletion. CommitOperationCopy is sent as file/lfsFile, so it needs no key.
_COMMIT_KEYS = {
    "header": False,
    "file": False,
    "lfsFile": False,
    "deletedFile": True,
    "deletedFolder": True,
}


def commit_line_deletes(raw: bytes) -> bool:
    """Whether one NDJSON commit line deletes. Raises ValueError if it cannot say."""
    line = raw.strip()
    if not line:
        return False
    obj = _strict_json(line)
    if not isinstance(obj, dict) or not isinstance(obj.get("key"), str):
        raise ValueError("a commit line is not an object with a string 'key'")
    if obj["key"] not in _COMMIT_KEYS:
        raise ValueError(f"unrecognised commit operation {obj['key']!r}")
    return _COMMIT_KEYS[obj["key"]]


def _check_encoding(request: Request) -> None:
    enc = (request.headers.get("content-encoding") or "identity").strip().lower()
    if enc != "identity":
        raise _invalid(f"a write body with content-encoding {enc!r} cannot be inspected",
                       415)


async def _read_body(request: Request, on_line=None) -> bytes:
    """The whole body, bounded, calling on_line(bytes) for each line as it
    completes. Raises _Refused for an oversize body -- before reading past the
    bound, and before reading anything at all when Content-Length says so."""
    limit = settings.hf_write_max_body
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    buf = bytearray()
    scanned = 0
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise _too_large(limit)
        if on_line is not None:
            while (nl := buf.find(b"\n", scanned)) != -1:
                on_line(bytes(buf[scanned:nl]))
                scanned = nl + 1
    if on_line is not None and scanned < len(buf):
        on_line(bytes(buf[scanned:]))
    return bytes(buf)


def _repo_from_body(obj: dict) -> tuple[str, str]:
    """(repo_type, repo_id) from create_repo / delete_repo's JSON (hf_api.py
    3691-3742, 3808-3818): {name, organization, type}."""
    name, org, typ = obj.get("name"), obj.get("organization"), obj.get("type") or "model"
    if typ not in authz.HF_REPO_TYPES:
        raise _invalid(f"unknown repository type {typ!r}")
    parts = [p for p in (org, name) if p is not None]
    if not isinstance(name, str) or any(
        not isinstance(p, str) or p in ("", ".", "..") or "/" in p for p in parts
    ):
        raise _invalid("the body does not name one repository as {name, organization}")
    return typ, "/".join(parts)


def _repo_id(value: object) -> str:
    """A `namespace/name` from move_repo's body (hf_api.py 4000-4016)."""
    if not isinstance(value, str):
        raise _invalid("move_repo needs fromRepo and toRepo as 'namespace/name'")
    parts = value.split("/")
    if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):
        raise _invalid(f"{value!r} is not 'namespace/name'")
    return value


# ---------------------------------------------------------------------------
# The handler.
# ---------------------------------------------------------------------------


def _who(key: authz.Key | None) -> tuple[str, str]:
    if key is None:
        return "-", "-"
    return key.key_id, key.principal


def _audit(result: str, request: Request, full_path: str, key: authz.Key | None,
           repos: list[str], deletes: str, status: int | str, reason: str = "") -> None:
    key_id, principal = _who(key)
    metrics.record_hf_write(result)
    log.info(
        "hf write %s: key=%s principal=%s method=%s path=/%s repo=%s deletes=%s "
        "status=%s%s",
        result, key_id, principal, request.method, hfauthz._header_safe(full_path),
        ",".join(repos) or "-", deletes, status,
        f" reason={hfauthz._header_safe(reason)}" if reason else "",
    )


async def handle(request: Request, full_path: str, write: Write) -> Response:
    """Authorise, inspect and forward one write. Every path out is audited."""
    key = getattr(request.state, "authz_key", None)
    ep = write.endpoint
    repos: list[tuple[str, str]] = [(write.repo_type, r) for r in write.repo_ids] \
        if write.repo_type else []
    # For the audit line. A commit starts "unknown" and becomes yes/no once its
    # body has been read -- or stays unknown when it is refused before that.
    deletes = "yes" if ep.destructive else ("unknown" if ep.commit else "no")
    seen_delete = False
    try:
        if not hfauthz.enforcing():
            # Unreachable with a valid configuration -- config refuses to start
            # without key auth and enforced rules. Refused, not assumed.
            raise _denied("writes need XHC_HF_AUTH=key and XHC_HF_RULES=enforce",
                          authz.HF_ANY_ENDPOINT, "pull+push")
        _check_encoding(request)

        # 1. What the PATH names is decided before a byte of body is read.
        if ep.target == "path":
            needs: list[tuple[authz.Operation, str]] = [
                ("push", authz.hf_reference(t, r)) for t, r in repos]
            if ep.destructive:
                needs += [("delete", authz.hf_reference(t, r)) for t, r in repos]
            _check(key, needs)

        # 2. The body, read once and bounded. A commit is inspected line by line
        #    and refused at the first deletion its caller may not make.
        if ep.commit:
            ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype != "application/x-ndjson":
                raise _invalid(f"a commit must be application/x-ndjson to be inspected, "
                               f"got {ctype or 'no content-type'!r}", 415)
            def on_line(raw: bytes) -> None:
                nonlocal seen_delete, deletes
                try:
                    is_delete = commit_line_deletes(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise _invalid(f"commit body cannot be inspected: {exc}") from None
                if is_delete and not seen_delete:
                    seen_delete, deletes = True, "yes"
                    _check(key, [("delete", authz.hf_reference(t, r)) for t, r in repos])

            body = await _read_body(request, on_line)
            deletes = "yes" if seen_delete else "no"
        else:
            body = await _read_body(request)

        # 3. Targets named by the BODY.
        if ep.target == "body":
            try:
                obj = _strict_json(body)
            except (ValueError, UnicodeDecodeError) as exc:
                raise _invalid(f"body is not strict JSON: {exc}") from None
            if not isinstance(obj, dict):
                raise _invalid("body is not a JSON object")
            if ep.kind == "move_repo":
                typ = obj.get("type") or "model"
                if typ not in authz.HF_REPO_TYPES:
                    raise _invalid(f"unknown repository type {typ!r}")
                src, dst = _repo_id(obj.get("fromRepo")), _repo_id(obj.get("toRepo"))
                repos = [(typ, src), (typ, dst)]
                _check(key, [("push", authz.hf_reference(typ, src)),
                             ("delete", authz.hf_reference(typ, src)),
                             ("push", authz.hf_reference(typ, dst))])
            else:
                typ, repo_id = _repo_from_body(obj)
                repos = [(typ, repo_id)]
                ref = authz.hf_reference(typ, repo_id)
                _check(key, [("push", ref)] + ([("delete", ref)] if ep.destructive else []))
        elif ep.target == "none" and not hfauthz.may_push_somewhere(key):
            raise _denied(f"key {_who(key)[0]} may not push to any Hugging Face repository",
                          authz.HF_ANY_ENDPOINT, "pull+push")
    except _Refused as refused:
        _audit(refused.result, request, full_path, key,
               [authz.hf_reference(t, r) for t, r in repos], deletes,
               refused.response.status_code, refused.reason)
        return refused.response

    return await _forward(request, full_path, write, key, body, repos, deletes)


async def _forward(request: Request, full_path: str, write: Write, key: authz.Key | None,
                   body: bytes, repos: list[tuple[str, str]], deletes: str) -> Response:
    from . import hfcompat  # hfcompat imports this module

    url = f"{settings.upstream}/{quote(full_path)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in hfcompat._HOP_BY_HOP}
    headers = hfcompat._apply_upstream_auth(headers)
    headers.setdefault("accept-encoding", "identity")
    client = hfcompat.get_client()
    req = client.build_request(request.method, url, headers=headers, content=body or None)
    refs_named = [authz.hf_reference(t, r) for t, r in repos]
    try:
        # Read whole, not streamed: every answer to a write is a small JSON
        # document (commit info, upload modes, LFS actions, a token), and the
        # status has to be known for the audit line before anything is sent on.
        resp = await client.send(req)
    except httpx.HTTPError as exc:
        _audit("upstream_unreachable", request, full_path, key, refs_named, deletes,
               "error", str(exc))
        return JSONResponse({"error": f"upstream error: {exc}"}, status_code=502)

    ok = resp.status_code < 400
    if ok:
        # FRESHNESS. A write can move a ref (commit, branch, tag, squash) or
        # create a repo that a negative-cache entry says is absent, so what this
        # process remembers about the repo is dropped. Whole repo, not just the
        # revision in the path: a commit with create_pr=1 moves refs/pr/N, not
        # the revision it names, and one extra ref lookup is cheap.
        for t, r in repos:
            refs.invalidate_repo(t, r)
            hfcompat.negative_cache_drop_repo(t, r)
    _audit("forwarded" if ok else "upstream_rejected", request, full_path, key,
           refs_named, deletes, resp.status_code)
    return hfcompat._passthrough(resp)


def describe_rules(store) -> tuple[int, int]:
    """(rules granting push on HF, of which granting delete), across principal
    grants and key narrowings, for the startup log."""
    push = delete = 0
    for _table, _owner, rule in store.all_rules():
        if authz.is_hf_pattern(rule.pattern) and rule.push:
            push += 1
            delete += int(rule.delete)
    return push, delete
