"""Per-key rules on the Hugging Face surface (XHC_HF_RULES).

Before this, XHC_HF_AUTH=key was a GATE: a live key pulled anything, whatever
its rules said. The cache's own Hub token accepts gated licences on behalf of
everyone, so "who may pull which repo" has to be answered here or nowhere.

THE PROPERTY THAT MATTERS IS THE HIT. A check performed only when fetching from
upstream is enforced on the first pull and silently absent on every pull after
it. So every refusal below is asserted twice where it can be: once before the
repo is cached, once after -- and every refusal also asserts that nothing was
asked of the Hub, because a refusal that fetched first has already spent the
cache's credential on the caller's behalf.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import Rule, RuleSyntaxError, new_secret, parse_rule

COMMIT = "a" * 40
ETAG = "b" * 64
BODY = b'{"model_type": "test"}'


class _Upstream:
    """Records every request that would have left for the Hub."""

    def __init__(self):
        self.calls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(f"{request.method} {request.url}")
            return httpx.Response(404, json={"error": "not found upstream"})

        return httpx.MockTransport(handler)


@pytest.fixture
def hf(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import dockerauth, hfcompat, refs
    from app.authzstore import AuthzStore
    from app.config import settings

    cache = tmp_path / "cache"
    cache.mkdir()
    db = tmp_path / "authz.db"
    monkeypatch.setattr(settings, "authz_db", str(db))
    monkeypatch.setattr(settings, "cache_dir", str(cache))
    monkeypatch.setattr(settings, "web_root", None)
    monkeypatch.setattr(settings, "hf_auth", "key")
    monkeypatch.setattr(settings, "hf_rules", "enforce")
    monkeypatch.setattr(settings, "synthesize_repo_info", True)
    monkeypatch.setattr(settings, "datasets_server", "https://datasets-server.invalid")
    monkeypatch.setattr(settings, "block_client_xet", False)
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(dockerauth, "_store", None)

    upstream = _Upstream()
    client_for_upstream = httpx.AsyncClient(transport=upstream.transport())
    monkeypatch.setattr(hfcompat, "get_client", lambda: client_for_upstream)

    async def _no_metadata(repo_type, repo_id, revision, filename):
        upstream.calls.append(f"METADATA {repo_type} {repo_id} {filename}")
        from huggingface_hub import errors

        raise errors.EntryNotFoundError("no such file upstream")

    monkeypatch.setattr(hfcompat, "fetch_metadata", _no_metadata)

    async def _never_stale(*_a, **_k):
        return False

    monkeypatch.setattr(refs, "is_stale", _never_stale)
    hfcompat.negative_cache_clear()

    store = AuthzStore(db)

    def key(rules, scope=None, subject=None):
        subject = subject or f"sub-{len(store.list_principals())}"
        store.claim_or_get_principal(subject)
        store.set_principal_rules(subject, [parse_rule(r) for r in rules])
        key_id, secret = new_secret()
        store.add_key(key_id, secret, subject, [])
        if scope:
            store.set_key_scope(key_id, [parse_rule(r) for r in scope])
        return {"authorization": f"Bearer {key_id}:{secret}"}, key_id

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(hfcompat.router)
    return TestClient(app, raise_server_exceptions=False), key, upstream, store, cache


def _seed(cache: Path, repo_type: str, repo_id: str, filename: str = "config.json"):
    """Put a repo on disk in the huggingface_hub layout, as an earlier pull would."""
    base = cache / f"{repo_type}s--{repo_id.replace('/', '--')}"
    (base / "blobs").mkdir(parents=True, exist_ok=True)
    (base / "refs").mkdir(parents=True, exist_ok=True)
    snap = base / "snapshots" / COMMIT
    snap.mkdir(parents=True, exist_ok=True)
    blob = base / "blobs" / ETAG
    blob.write_bytes(BODY)
    (snap / filename).symlink_to(blob)
    (base / "refs" / "main").write_text(COMMIT)


LOCAL = {"x-muninn-local-only": "1"}


# ---------------- the grammar ----------------


def test_an_hf_rule_parses_with_the_existing_grammar():
    assert parse_rule("hf/models/org/model-* pull") == Rule("hf/models/org/model-*", True, False)
    assert parse_rule("hf/datasets/org/*") == Rule("hf/datasets/org/*", True, False)


@pytest.mark.parametrize("line", ["hf/models/org/* push", "hf/models/org/* pull+push",
                                  "HF/datasets/* push+pull"])
def test_push_on_an_hf_pattern_is_refused_because_nothing_could_ever_use_it(line):
    """Muninn never pushes to the Hub. A grant that can never be exercised is a
    rule that misdescribes itself, and the person who typed it believes it
    does something."""
    with pytest.raises(RuleSyntaxError, match="pull"):
        parse_rule(line)


def test_a_bare_star_with_push_is_still_valid():
    assert parse_rule("* pull+push") == Rule("*", True, True)


# ---------------- miss, then hit ----------------


def test_a_matching_key_pulls_and_a_non_matching_one_is_refused_on_miss_and_on_hit(hf):
    """THE TEST THIS FEATURE EXISTS FOR. Refused before the repo is cached, and
    STILL refused after it is -- including by a key that did not cause the fill."""
    client, key, upstream, _, cache = hf
    ok, _ = key(["hf/models/org/model-* pull"])
    other, _ = key(["hf/models/org/model-* pull"])  # a second key, same grant
    no, _ = key(["hf/models/elsewhere/* pull"])

    path = f"/org/model-a/resolve/{COMMIT}/config.json"

    # MISS. The authorised key reaches the Hub; the other key does not.
    r = client.get(path, headers=ok)
    assert r.status_code == 404 and upstream.calls, r.text
    upstream.calls.clear()
    r = client.get(path, headers=no)
    assert r.status_code == 403, r.text
    assert not upstream.calls, "a refused request must not reach the Hub"

    # HIT, after something else filled the cache.
    _seed(cache, "model", "org/model-a")
    r = client.get(path, headers=other)
    assert r.status_code == 200 and r.content == BODY
    assert r.headers.get("x-xhc-cache") == "HIT"
    r = client.get(path, headers=no)
    assert r.status_code == 403, r.text
    assert BODY not in r.content
    assert not upstream.calls


def test_a_non_matching_repo_in_the_same_org_is_refused(hf):
    client, key, upstream, _, cache = hf
    ok, _ = key(["hf/models/org/model-* pull"])
    _seed(cache, "model", "org/secret")
    r = client.get(f"/org/secret/resolve/{COMMIT}/config.json", headers=ok)
    assert r.status_code == 403
    assert not upstream.calls


def test_datasets_and_models_are_separate_namespaces(hf):
    client, key, _, _, cache = hf
    models_only, _ = key(["hf/models/org/* pull"])
    _seed(cache, "dataset", "org/data")
    r = client.get(f"/datasets/org/data/resolve/{COMMIT}/config.json", headers=models_only)
    assert r.status_code == 403
    both, _ = key(["hf/models/org/* pull", "hf/datasets/org/* pull"])
    r = client.get(f"/datasets/org/data/resolve/{COMMIT}/config.json", headers=both)
    assert r.status_code == 200 and r.content == BODY


# ---------------- scope narrows ----------------


def test_a_key_scope_narrows_hf_access_as_it_narrows_registry_access(hf):
    client, key, _, _, cache = hf
    _seed(cache, "model", "org/model-a")
    _seed(cache, "model", "org/model-b")
    scoped, key_id = key(["hf/models/org/* pull"], scope=["hf/models/org/model-a pull"])
    assert client.get(f"/org/model-a/resolve/{COMMIT}/config.json",
                      headers=scoped).status_code == 200
    r = client.get(f"/org/model-b/resolve/{COMMIT}/config.json", headers=scoped)
    assert r.status_code == 403
    # The reason distinguishes "never granted" from "scoped away", as decide() does.
    assert "scoped away" in r.headers.get("x-error-message", "")


def test_a_scope_cannot_widen_past_the_holders_rules(hf):
    client, key, _, _, cache = hf
    _seed(cache, "model", "org/model-b")
    scoped, _ = key(["hf/models/org/model-a pull"], scope=["hf/models/* pull"])
    assert client.get(f"/org/model-b/resolve/{COMMIT}/config.json",
                      headers=scoped).status_code == 403


def test_a_registry_only_scope_takes_the_hf_surface_away_from_a_star_holder(hf):
    """The one way an unscoped-`*` holder is affected, stated as a test: a key
    they deliberately narrowed to a registry now means what it says on HF too."""
    client, key, _, _, cache = hf
    _seed(cache, "model", "org/model-a")
    scoped, _ = key(["* pull+push"], scope=["docker.io/library/* pull"])
    assert client.get(f"/org/model-a/resolve/{COMMIT}/config.json",
                      headers=scoped).status_code == 403


# ---------------- backward compatibility ----------------


def test_star_still_grants_everything_on_every_route(hf):
    client, key, _, _, cache = hf
    star, _ = key(["* pull"])
    _seed(cache, "model", "org/model-a")
    for method, template in ROUTES:
        path = template.format(repo="org/model-a", rev=COMMIT)
        r = client.request(method, path, headers=star)
        assert r.status_code != 403, f"{method} {path} -> {r.status_code} {r.text}"


def test_hf_star_grants_the_whole_hf_surface_and_nothing_on_the_registry(hf):
    client, key, _, store, _ = hf
    hf_all, key_id = key(["hf/* pull"])
    for method, template in ROUTES:
        path = template.format(repo="org/model-a", rev=COMMIT)
        r = client.request(method, path, headers=hf_all)
        assert r.status_code != 403, f"{method} {path} -> {r.status_code} {r.text}"
    from app import authz

    secret = hf_all["authorization"].split(":", 1)[1]
    k = store.resolve(key_id, secret)
    assert k is not None and not authz.decide(k, "pull", "docker.io/library/alpine")[0]


def test_a_registry_only_principal_is_refused_hf_when_enforcing(hf):
    client, key, upstream, _, cache = hf
    _seed(cache, "model", "org/model-a")
    registry_only, _ = key(["docker.io/library/* pull"])
    r = client.get(f"/org/model-a/resolve/{COMMIT}/config.json", headers=registry_only)
    assert r.status_code == 403
    assert not upstream.calls


def test_a_registry_only_principal_is_allowed_hf_when_rules_are_off(hf, monkeypatch):
    from app.config import settings

    client, key, _, _, cache = hf
    monkeypatch.setattr(settings, "hf_rules", "off")
    _seed(cache, "model", "org/model-a")
    registry_only, _ = key(["docker.io/library/* pull"])
    r = client.get(f"/org/model-a/resolve/{COMMIT}/config.json", headers=registry_only)
    assert r.status_code == 200 and r.content == BODY


def test_an_empty_allowlist_is_refused_when_enforcing(hf):
    client, key, _, _, cache = hf
    _seed(cache, "model", "org/model-a")
    empty, _ = key([])
    r = client.get(f"/org/model-a/resolve/{COMMIT}/config.json", headers=empty)
    assert r.status_code == 403
    assert "no rules" in r.headers.get("x-error-message", "")


def test_nothing_changes_when_hf_auth_is_off(hf, monkeypatch):
    """XHC_HF_AUTH=none: no credential, no rules, exactly as before."""
    from app.config import settings

    client, _, _, _, cache = hf
    monkeypatch.setattr(settings, "hf_auth", "none")
    _seed(cache, "model", "org/model-a")
    r = client.get(f"/org/model-a/resolve/{COMMIT}/config.json")
    assert r.status_code == 200 and r.content == BODY


# ---------------- revocation ----------------


def test_a_disabled_key_is_refused_even_on_a_hit(hf):
    client, key, _, store, cache = hf
    _seed(cache, "model", "org/model-a")
    ok, key_id = key(["hf/models/org/* pull"])
    path = f"/org/model-a/resolve/{COMMIT}/config.json"
    assert client.get(path, headers=ok).status_code == 200
    store.set_key_disabled(key_id, True)
    assert client.get(path, headers=ok).status_code == 401


# ---------------- every route ----------------

# Every route on the HF surface that names a repository, by the handler that
# serves it. `{repo}` is substituted with a repo the key may NOT pull.
ROUTES = [
    # serve_file: the bytes
    ("GET", "/{repo}/resolve/{rev}/config.json"),
    ("HEAD", "/{repo}/resolve/{rev}/config.json"),
    ("GET", "/{repo}/resolve/main/config.json"),
    # serve_repo_info: what snapshot_download enumerates
    ("GET", "/api/models/{repo}"),
    ("GET", "/api/models/{repo}/revision/main"),
    # serve_tree: list_repo_files
    ("GET", "/api/models/{repo}/tree/main"),
    ("GET", "/api/models/{repo}/tree/main/sub/dir"),
    # proxy_upstream: sub-resources and web paths that name a repo
    ("GET", "/api/models/{repo}/refs"),
    ("GET", "/api/models/{repo}/commits/main"),
    ("POST", "/api/models/{repo}/paths-info/main"),
    ("GET", "/api/models/{repo}/xet-read-token/main"),
    ("POST", "/{repo}/resolve/main/config.json"),
    ("GET", "/{repo}/raw/main/config.json"),
    ("GET", "/{repo}/blob/main/config.json"),
    ("GET", "/{repo}"),
]

DATASET_ROUTES = [
    ("GET", "/datasets/{repo}/resolve/{rev}/config.json"),
    ("GET", "/api/datasets/{repo}"),
    ("GET", "/api/datasets/{repo}/revision/main"),
    ("GET", "/api/datasets/{repo}/tree/main"),
    # serve_viewer
    ("GET", "/api/datasets/{repo}/parquet"),
    ("GET", "/api/datasets/{repo}/croissant"),
    # serve_datasets_server
    ("GET", "/datasets-server/splits?dataset={repo}"),
    ("GET", "/datasets-server/rows?dataset=org/model-a&dataset={repo}"),
    ("GET", "/datasets/{repo}"),
]

SPACE_ROUTES = [
    ("GET", "/spaces/{repo}/resolve/{rev}/app.py"),
    ("GET", "/api/spaces/{repo}"),
]


@pytest.mark.parametrize("method,path", ROUTES + DATASET_ROUTES + SPACE_ROUTES)
def test_every_repo_route_refuses_a_repo_the_key_may_not_pull(hf, method, path):
    client, key, upstream, _, cache = hf
    # Grants a sibling in every namespace, so a refusal cannot be "this key has
    # no HF rules at all" -- it has to be the repo that was refused.
    k, _ = key(["hf/models/org/model-a pull", "hf/datasets/org/model-a pull",
                "hf/spaces/org/model-a pull"])
    for rtype in ("model", "dataset", "space"):
        _seed(cache, rtype, "org/secret")
    r = client.request(method, path.format(repo="org/secret", rev=COMMIT), headers=k)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code} {r.text}"
    assert not upstream.calls, f"{method} {path} reached the Hub: {upstream.calls}"
    assert b"model_type" not in r.content


@pytest.mark.parametrize("method,path", ROUTES + DATASET_ROUTES + SPACE_ROUTES)
def test_every_repo_route_admits_the_repo_the_key_may_pull(hf, method, path):
    """The negative control for the test above: the same routes, the granted
    repo, and none of them is refused. Without it, a route that refused
    EVERYTHING would pass the refusal test."""
    client, key, _, _, cache = hf
    k, _ = key(["hf/models/org/model-a pull", "hf/datasets/org/model-a pull",
                "hf/spaces/org/model-a pull"])
    for rtype in ("model", "dataset", "space"):
        _seed(cache, rtype, "org/model-a")
    r = client.request(method, path.format(repo="org/model-a", rev=COMMIT), headers=k)
    assert r.status_code != 403, f"{method} {path} -> {r.status_code} {r.text}"


# ---------------- paths that do not name one repository ----------------


@pytest.mark.parametrize("path", ["/api/models", "/api/models?search=llama",
                                  "/api/datasets?author=org", "/api/spaces"])
def test_a_listing_needs_a_grant_over_the_whole_type(hf, path):
    """A search answers with the cache's Hub identity, so it can name PRIVATE
    repos that identity can see. A key allowed one org must not enumerate the
    rest; a key allowed every model may."""
    client, key, upstream, _, _ = hf
    narrow, _ = key(["hf/models/org/* pull", "hf/datasets/org/* pull", "hf/spaces/org/* pull"])
    assert client.get(path, headers=narrow).status_code == 403
    assert not upstream.calls
    wide, _ = key(["hf/models/* pull", "hf/datasets/* pull", "hf/spaces/* pull"])
    assert client.get(path, headers=wide).status_code != 403


@pytest.mark.parametrize("path", ["/api/whoami-v2", "/api/collections", "/api/papers/x",
                                  "/api/organizations/org/members"])
def test_a_path_naming_no_repo_needs_a_grant_over_the_whole_surface(hf, path):
    """whoami-v2 answers with the CACHE's Hub account -- its name, email and org
    memberships -- not the caller's. Downloads never call it."""
    client, key, upstream, _, _ = hf
    narrow, _ = key(["hf/models/* pull", "hf/datasets/* pull"])
    assert client.get(path, headers=narrow).status_code == 403
    assert not upstream.calls
    surface, _ = key(["hf/* pull"])
    assert client.get(path, headers=surface).status_code != 403


# ---------------- ambiguity and traversal ----------------


def test_a_canonical_repo_id_is_authorised_as_itself(hf):
    """`gpt2` has no org. Its rule is `hf/models/gpt2`, and the paths
    huggingface_hub builds for it all authorise against exactly that."""
    client, key, _, _, cache = hf
    _seed(cache, "model", "gpt2")
    k, _ = key(["hf/models/gpt2 pull"])
    for path in (f"/gpt2/resolve/{COMMIT}/config.json", "/api/models/gpt2",
                 "/api/models/gpt2/revision/main", "/api/models/gpt2/tree/main"):
        assert client.get(path, headers=k).status_code != 403, path


def test_an_unknown_sub_resource_is_refused_to_a_narrow_key_rather_than_guessed(hf):
    """A Hub endpoint Muninn has never heard of could be `<org>/<name>/<new>` or
    canonical `<org>`'s `<name>`. Guessing wrong would authorise one repo and
    fetch another, so both readings are required -- and a `*` or `hf/*` holder,
    who has both, is unaffected."""
    client, key, upstream, _, _ = hf
    narrow, _ = key(["hf/models/org/model-a pull"])
    r = client.get("/api/models/org/model-a/some-future-endpoint", headers=narrow)
    assert r.status_code == 403
    assert "hf/models/org" in r.headers["x-error-message"]
    assert not upstream.calls
    surface, _ = key(["hf/* pull"])
    assert client.get("/api/models/org/model-a/some-future-endpoint",
                      headers=surface).status_code != 403


def test_an_ambiguous_path_needs_both_readings_granted(hf):
    """`api/models/org/refs` is repo `org/refs`'s info, or canonical `org`'s
    refs. Muninn cannot know which the Hub will choose, so both must be allowed."""
    client, key, upstream, _, _ = hf
    k, _ = key(["hf/models/org/* pull"])
    assert client.get("/api/models/org/refs", headers=k).status_code == 403
    assert not upstream.calls
    both, _ = key(["hf/models/org pull", "hf/models/org/* pull"])
    assert client.get("/api/models/org/refs", headers=both).status_code != 403


@pytest.mark.parametrize("path", [
    "/org/model-a/resolve/main/%2E%2E/%2E%2E/%2E%2E/org/secret/resolve/main/config.json",
    "/api/models/org/model-a/%2E%2E/%2E%2E/org/secret",
    "/org/model-a/.%2E/secret/raw/main/config.json",
    "/org//secret/resolve/main/config.json",
])
def test_dot_segments_cannot_walk_out_of_an_authorised_repo(hf, path):
    """The upstream client normalises `..` before sending, so a path that names
    an authorised repo can be walked into one that is not."""
    client, key, upstream, _, _ = hf
    k, _ = key(["hf/models/org/model-a* pull"])
    r = client.get(path, headers=k)
    assert r.status_code == 400, f"{path} -> {r.status_code} {r.text}"
    assert not upstream.calls


# ---------------- the refusal ----------------


def test_the_refusal_is_a_gated_repo_error_naming_the_reason(hf):
    """403, not 404: a 404 sends the user looking for a typo in a repo id that
    is correct. GatedRepo because that is exactly the situation -- the repo
    exists, and this credential is not on its access list -- and it is the
    code huggingface_hub re-raises from a HEAD. A bare 403 there is swallowed
    into "check your connection"."""
    client, key, _, _, _ = hf
    k, key_id = key(["hf/models/org/model-a pull"])
    r = client.get("/org/secret/resolve/main/config.json", headers=k)
    assert r.status_code == 403
    assert r.headers["x-error-code"] == "GatedRepo"
    msg = r.headers["x-error-message"]
    assert key_id in msg and "hf/models/org/secret" in msg
    assert "hf/models/org/secret" in r.json()["error"]


def test_huggingface_hub_raises_gated_repo_error_on_the_refusal(hf):
    """Through the library, not a model of it: the client-side exception type is
    the user's actual experience."""
    from huggingface_hub.errors import GatedRepoError
    from huggingface_hub.utils import hf_raise_for_status

    client, key, _, _, _ = hf
    k, _ = key(["hf/models/org/model-a pull"])
    r = client.head("/org/secret/resolve/main/config.json", headers=k)

    import requests

    resp = requests.Response()
    resp.status_code = r.status_code
    resp.headers.update(r.headers)
    resp.url = "http://cache/org/secret/resolve/main/config.json"
    resp._content = b""
    with pytest.raises(GatedRepoError) as exc:
        hf_raise_for_status(resp)
    assert "hf/models/org/secret" in str(exc.value)


def test_hf_rules_default_is_enforce(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("XHC_HF_RULES", raising=False)
    assert Settings.from_env().hf_rules == "enforce"


def test_hf_rules_rejects_an_unknown_value(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("XHC_HF_RULES", "warn")
    with pytest.raises(ValueError, match="XHC_HF_RULES"):
        Settings.from_env()


def test_hf_hub_download_surfaces_the_refusal_as_gated_repo_error(hf, tmp_path):
    """END TO END through huggingface_hub over a real socket: the exception a
    user actually sees, on a repo that is already cached. Also asserts the
    authorised key downloads the same file through the same server, so the
    refusal cannot be a server that refuses everything."""
    import socket
    import threading
    import time

    import uvicorn
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import GatedRepoError

    client, key, _, _, cache = hf
    _seed(cache, "model", "org/model-a")
    _seed(cache, "model", "org/secret")
    ok, _ = key(["hf/models/org/model-a pull"])

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        endpoint = f"http://127.0.0.1:{port}"
        token = ok["authorization"].split(" ", 1)[1]
        got = hf_hub_download("org/model-a", "config.json", revision=COMMIT,
                              endpoint=endpoint, token=token, cache_dir=tmp_path / "c1")
        assert Path(got).read_bytes() == BODY
        with pytest.raises(GatedRepoError) as exc:
            hf_hub_download("org/secret", "config.json", revision=COMMIT,
                            endpoint=endpoint, token=token, cache_dir=tmp_path / "c2")
        assert "hf/models/org/secret" in str(exc.value)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
