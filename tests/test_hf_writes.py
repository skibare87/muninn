"""Optional, rule-gated writes toward the Hugging Face Hub (XHC_HF_WRITES).

Off by default, and off is tests/test_hf_read_only.py unchanged. On, a named set
of repository writes is forwarded with the cache's token, each only when the
caller's rules grant push -- and delete, for a destructive one -- on the repo.

Every refusal below asserts the fake Hub was never called: a 403 that had
already forwarded the request would be a log line, not a refusal.
"""

from __future__ import annotations

import itertools
import json
import logging
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CACHE_TOKEN = "hf_cache_own_token"
NEW_COMMIT = "c" * 40


def ndjson(*ops: dict) -> bytes:
    """A commit body as huggingface_hub builds it (_commit_api.py)."""
    lines = [{"key": "header", "value": {"summary": "test", "description": ""}}, *ops]
    return b"".join(json.dumps(x).encode() + b"\n" for x in lines)


ADD = {"key": "file", "value": {"content": "aGk=", "path": "a.txt", "encoding": "base64"}}
LFS = {"key": "lfsFile", "value": {"path": "w.bin", "algo": "sha256", "oid": "d" * 64,
                                   "size": 10}}
DEL_FILE = {"key": "deletedFile", "value": {"path": "old.txt"}}
DEL_FOLDER = {"key": "deletedFolder", "value": {"path": "old/"}}
NDJSON = {"content-type": "application/x-ndjson"}


class FakeHub:
    """Records every request, bytes included, and answers like the Hub would."""

    def __init__(self):
        self.calls: list[dict] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.calls.append({"method": request.method, "path": request.url.path,
                           "query": request.url.query.decode(),
                           "headers": dict(request.headers), "body": body})
        path = request.url.path
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "no"})
        if "/preupload/" in path:
            files = json.loads(body)["files"]
            return httpx.Response(200, json={"files": [
                {"path": f["path"], "uploadMode": "regular", "shouldIgnore": False}
                for f in files]})
        if "/commit/" in path:
            return httpx.Response(200, json={
                "commitUrl": f"https://huggingface.co/org/x/commit/{NEW_COMMIT}",
                "commitOid": NEW_COMMIT, "pullRequestUrl": None})
        if request.method == "GET" and "/revision/" in path:
            # repo_info, which preupload_lfs_files always calls in 0.34.4 to
            # decide xet vs LFS. xetEnabled false keeps the client on LFS.
            return httpx.Response(200, json={"id": "org/x", "sha": "a" * 40,
                                             "xetEnabled": False, "siblings": []})
        return httpx.Response(200, json={})

    def writes(self) -> list[dict]:
        return [c for c in self.calls if c["method"] not in ("GET", "HEAD")]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A cache with XHC_HF_AUTH=key, XHC_HF_RULES=enforce and XHC_HF_WRITES=on."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import dockerauth, hfcompat, metrics, refs
    from app.authzstore import AuthzStore
    from app.config import settings

    cache = tmp_path / "cache"
    cache.mkdir()
    db = tmp_path / "authz.db"
    for name, value in {
        "cache_dir": str(cache), "web_root": None, "state_dir": None,
        "authz_db": str(db), "hf_auth": "key", "hf_rules": "enforce",
        "hf_writes": "on", "hf_token": CACHE_TOKEN, "synthesize_repo_info": True,
        "hf_write_max_body": 64 * 1024 * 1024,
    }.items():
        # raising=False so this fixture runs against a build without the
        # setting, and the tests fail there on BEHAVIOUR rather than on setup.
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(dockerauth, "_store", None)
    hub = FakeHub()
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(hub.handler))
    monkeypatch.setattr(hfcompat, "get_client", lambda: upstream)
    metrics.reset()
    refs.clear()
    hfcompat.negative_cache_clear()

    app = FastAPI()
    app.include_router(hfcompat.router)
    client = TestClient(app, raise_server_exceptions=False)
    store = AuthzStore(db)

    class Env:
        pass

    e = Env()
    e.client, e.hub, e.store, e.app = client, hub, store, app
    e.key = lambda rules, scope=(): _key(store, rules, scope)
    return e


_subjects = itertools.count(1)
_last: dict[str, str] = {}


def _key(store, rules: list[str], scope=()) -> dict[str, str]:
    """A fresh principal holding `rules` (rule text), and a key narrowed by `scope`."""
    from app.authz import new_secret, parse_rules

    subject = _last["subject"] = f"p{next(_subjects)}"
    store.create_principal(subject)
    store.set_principal_rules(subject, parse_rules(rules))
    key_id, secret = new_secret()
    store.add_key(key_id, secret, subject, parse_rules(list(scope)))
    return {"authorization": f"Bearer {key_id}:{secret}"}


def _commit(e, headers, body, path="/api/models/org/x/commit/main"):
    return e.client.post(path, headers={**headers, **NDJSON}, content=body)


# ---------------------------------------------------------------- default off


def test_writes_are_off_by_default(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("XHC_HF_WRITES", raising=False)
    assert Settings.from_env().hf_writes == "off"
    assert Settings().hf_writes == "off"


def test_off_is_the_old_405_even_for_a_key_holding_push(env, monkeypatch):
    """Writes off: a commit is refused before the rules are ever consulted."""
    from app.config import settings

    h = env.key(["models/org/* pull+push+delete"])
    monkeypatch.setattr(settings, "hf_writes", "off")
    r = _commit(env, h, ndjson(ADD))
    assert r.status_code == 405
    assert "read-only toward the Hugging Face Hub" in r.text
    assert env.hub.calls == []


# ---------------------------------------------------------------- push


def test_push_without_a_grant_is_403_with_the_reason(env):
    h = env.key(["models/org/* pull"])
    r = _commit(env, h, ndjson(ADD))
    assert r.status_code == 403, r.text
    assert "no rule granting push on models/org/x" in r.headers["x-error-message"]
    assert "pull+push" in r.json()["hint"]
    assert env.hub.calls == []


def test_push_with_a_grant_is_forwarded_byte_for_byte_as_the_cache(env):
    h = env.key(["models/org/* pull+push"])
    body = ndjson(ADD, LFS)
    r = _commit(env, h, body, "/api/models/org/x/commit/main?create_pr=1")
    assert r.status_code == 200, r.text
    assert r.json()["commitOid"] == NEW_COMMIT
    [call] = env.hub.calls
    assert call["method"] == "POST"
    assert call["path"] == "/api/models/org/x/commit/main"
    assert call["query"] == "create_pr=1"
    assert call["body"] == body, "the forwarded bytes are not the inspected bytes"
    # Upstream it is the CACHE, never the caller's key.
    assert call["headers"]["authorization"] == f"Bearer {CACHE_TOKEN}"
    assert call["headers"]["content-type"] == "application/x-ndjson"


def test_a_bare_star_grants_no_hub_write(env):
    """`* pull+push` predates Hub writes and meant registry push."""
    h = env.key(["* pull+push"])
    assert _commit(env, h, ndjson(ADD)).status_code == 403
    # ...and still pulls: `*` keeps its meaning on the read side.
    assert env.client.get("/api/models/org/x/refs", headers=h).status_code == 200
    assert env.hub.writes() == []


def test_a_grant_on_one_repo_is_not_a_grant_on_another(env):
    h = env.key(["models/org/x pull+push"])
    assert _commit(env, h, ndjson(ADD), "/api/models/org/y/commit/main").status_code == 403
    assert _commit(env, h, ndjson(ADD), "/api/datasets/org/x/commit/main").status_code == 403
    assert env.hub.calls == []
    assert _commit(env, h, ndjson(ADD)).status_code == 200


# ---------------------------------------------------------------- commit deletes


@pytest.mark.parametrize("op", [DEL_FILE, DEL_FOLDER], ids=["deletedFile", "deletedFolder"])
def test_a_commit_that_deletes_needs_the_delete_grant(env, op):
    h = env.key(["models/org/* pull+push"])
    r = _commit(env, h, ndjson(ADD, op))
    assert r.status_code == 403, r.text
    assert "delete" in r.headers["x-error-message"]
    assert "pull+push+delete" in r.json()["hint"]
    assert env.hub.calls == [], "a refused commit reached the Hub"


def test_a_deletion_buried_after_a_large_body_is_still_found(env):
    """The inspection reads every line, not a prefix."""
    h = env.key(["models/org/* pull+push"])
    big = {"key": "file", "value": {"content": "A" * 2_000_000, "path": "big.txt",
                                    "encoding": "base64"}}
    assert _commit(env, h, ndjson(big, ADD, big, DEL_FILE)).status_code == 403
    assert env.hub.calls == []


def test_a_streamed_commit_is_inspected_as_it_arrives(env):
    """Chunked, no Content-Length, a line split across chunks."""
    h = env.key(["models/org/* pull+push"])
    body = ndjson(ADD, DEL_FILE)

    def chunks():
        for i in range(0, len(body), 7):
            yield body[i:i + 7]

    r = env.client.post("/api/models/org/x/commit/main", headers={**h, **NDJSON},
                        content=chunks())
    assert r.status_code == 403
    assert env.hub.calls == []


def test_a_commit_that_deletes_is_forwarded_with_the_grant(env):
    h = env.key(["models/org/* pull+push+delete"])
    body = ndjson(ADD, DEL_FILE, DEL_FOLDER)
    r = _commit(env, h, body)
    assert r.status_code == 200, r.text
    assert [c["body"] for c in env.hub.calls] == [body]


@pytest.mark.parametrize("body,why", [
    (ndjson({"key": "renamedFile", "value": {}}), "unrecognised commit operation"),
    (ndjson(ADD) + b'{"key": "file", "key": "deletedFile", "value": {"path": "x"}}\n',
     "duplicate key"),
    (ndjson(ADD) + b"{not json\n", "cannot be inspected"),
    (ndjson(ADD) + b'{"key":"file"}\r{"key":"deletedFile","value":{"path":"x"}}\n',
     "cannot be inspected"),
    (ndjson(ADD) + b'["key", "deletedFile"]\n', "object with a string 'key'"),
])
def test_a_commit_line_that_cannot_be_classified_is_refused(env, body, why):
    """An operation this cache cannot read is not assumed harmless -- even for a
    key that COULD delete, since the next unknown key might be anything."""
    h = env.key(["models/org/* pull+push+delete"])
    r = _commit(env, h, body)
    assert r.status_code == 400, r.text
    assert why in r.json()["error"]
    assert env.hub.calls == []


def test_a_commit_that_is_not_ndjson_is_refused(env):
    h = env.key(["models/org/* pull+push+delete"])
    r = env.client.post("/api/models/org/x/commit/main",
                        headers={**h, "content-type": "application/json"},
                        content=json.dumps({"deletedFiles": [{"path": "x"}]}))
    assert r.status_code == 415
    r = env.client.post("/api/models/org/x/commit/main",
                        headers={**h, **NDJSON, "content-encoding": "gzip"},
                        content=b"\x1f\x8b")
    assert r.status_code == 415
    assert env.hub.calls == []


# ---------------------------------------------------------------- size bound


def test_an_oversize_commit_is_413_and_never_forwarded(env, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hf_write_max_body", 1024)
    h = env.key(["models/org/* pull+push+delete"])
    body = ndjson(*[ADD] * 40)
    assert len(body) > 1024
    r = _commit(env, h, body)
    assert r.status_code == 413
    assert "XHC_HF_WRITE_MAX_BODY" in r.json()["error"]

    def chunks():  # no Content-Length: the bound is enforced while reading
        for i in range(0, len(body), 100):
            yield body[i:i + 100]

    r = env.client.post("/api/models/org/x/commit/main", headers={**h, **NDJSON},
                        content=chunks())
    assert r.status_code == 413
    assert env.hub.calls == []


# ---------------------------------------------------------------- destructive endpoints


DESTRUCTIVE = [
    ("DELETE", "/api/models/org/x/branch/dev", None),                    # delete_branch
    ("DELETE", "/api/models/org/x/branch/feature/nested", None),
    ("DELETE", "/api/models/org/x/tag/v1", None),                        # delete_tag
    ("POST", "/api/models/org/x/super-squash/main", {"message": "s"}),   # super_squash
    ("POST", "/api/models/org/x/lfs-files/batch",                        # lfs purge
     {"deletions": {"sha": ["d" * 64], "rewriteHistory": True}}),
    ("DELETE", "/api/repos/delete", {"name": "x", "organization": "org",  # delete_repo
                                     "type": "model"}),
    ("DELETE", "/api/repos/delete", {"name": "x", "organization": "org"}),
    ("POST", "/api/repos/move", {"fromRepo": "org/x", "toRepo": "org/y",  # move_repo
                                 "type": "model"}),
]


@pytest.mark.parametrize("method,path,body", DESTRUCTIVE)
def test_destructive_endpoints_need_the_delete_grant(env, method, path, body):
    h = env.key(["models/org/* pull+push"])
    content = json.dumps(body).encode() if body is not None else None
    r = env.client.request(method, path, headers=h, content=content)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code} {r.text}"
    assert "delete" in r.headers["x-error-message"]
    assert env.hub.calls == []

    h = env.key(["models/org/* pull+push+delete"])
    r = env.client.request(method, path, headers=h, content=content)
    assert r.status_code == 200, f"{method} {path} -> {r.status_code} {r.text}"
    [call] = env.hub.calls
    assert (call["method"], call["path"]) == (method, path)
    assert call["body"] == (content or b"")


NON_DESTRUCTIVE = [
    ("POST", "/api/models/org/x/preupload/main", {"files": []}),
    ("POST", "/api/models/org/x/branch/dev", {}),
    ("POST", "/api/models/org/x/tag/main", {"tag": "v1"}),
    ("POST", "/api/repos/create", {"name": "x", "organization": "org", "type": "model"}),
    ("POST", "/org/x.git/info/lfs/objects/batch", {"operation": "upload", "objects": []}),
    ("POST", "/org/x.git/info/lfs/objects/verify", {"oid": "d" * 64, "size": 1}),
    ("GET", "/api/models/org/x/xet-write-token/main", None),
]


@pytest.mark.parametrize("method,path,body", NON_DESTRUCTIVE)
def test_other_repo_writes_need_push_only(env, method, path, body):
    content = json.dumps(body).encode() if body is not None else None
    r = env.client.request(method, path, headers=env.key(["models/org/* pull"]),
                           content=content)
    assert r.status_code == 403, f"{method} {path} -> {r.status_code}"
    assert env.hub.calls == []
    r = env.client.request(method, path, headers=env.key(["models/org/* pull+push"]),
                           content=content)
    assert r.status_code == 200, f"{method} {path} -> {r.status_code} {r.text}"
    assert len(env.hub.calls) == 1


def test_a_move_needs_delete_on_the_source_and_push_on_the_destination(env):
    body = json.dumps({"fromRepo": "org/x", "toRepo": "other/y", "type": "model"})
    h = env.key(["models/org/* pull+push+delete"])
    r = env.client.post("/api/repos/move", headers=h, content=body)
    assert r.status_code == 403, "moved into a namespace the key cannot push to"
    assert "models/other/y" in r.headers["x-error-message"]
    h = env.key(["models/org/* pull+push+delete", "models/other/* pull+push"])
    assert env.client.post("/api/repos/move", headers=h, content=body).status_code == 200


def test_repo_create_is_authorised_on_the_repo_the_body_names(env):
    h = env.key(["datasets/org/* pull+push"])
    ok = json.dumps({"name": "d", "organization": "org", "type": "dataset"})
    assert env.client.post("/api/repos/create", headers=h, content=ok).status_code == 200
    for bad in ({"name": "d", "organization": "other", "type": "dataset"},
                {"name": "d", "organization": "org"}):   # no type: a MODEL
        r = env.client.post("/api/repos/create", headers=h, content=json.dumps(bad))
        assert r.status_code == 403, bad
    for invalid in ({"name": "org/d", "type": "dataset"}, {"name": "..", "type": "dataset"},
                    {"name": "d", "type": "bucket"}, ["d"]):
        r = env.client.post("/api/repos/create", headers=h, content=json.dumps(invalid))
        assert r.status_code == 400, invalid
    assert len(env.hub.calls) == 1


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/spaces/org/x/restart"),
    ("POST", "/api/spaces/org/x/secrets"),
    ("DELETE", "/api/spaces/org/x/storage"),
    ("PUT", "/api/models/org/x/settings"),
    ("POST", "/api/models/org/x/discussions"),
    ("POST", "/api/models/org/x/discussions/1/merge"),
    ("POST", "/api/collections"),
    ("DELETE", "/api/models/org/x/like"),
    ("POST", "/api/complete_multipart"),
    ("PATCH", "/api/models/org/x"),
])
def test_writes_outside_the_named_set_stay_405_whatever_the_grants(env, method, path):
    h = env.key(["models/org/* pull+push+delete", "spaces/org/* pull+push+delete"])
    r = env.client.request(method, path, headers=h, content=b"{}")
    assert r.status_code == 405, f"{method} {path} -> {r.status_code}"
    assert "XHC_HF_WRITES=on" in r.text
    assert env.hub.calls == []


def test_validate_yaml_needs_push_somewhere(env):
    body = json.dumps({"content": "---\nlicense: mit\n---\n", "repoType": "model"})
    r = env.client.post("/api/validate-yaml", headers=env.key(["models/org/* pull"]),
                        content=body)
    assert r.status_code == 403
    r = env.client.post("/api/validate-yaml", headers=env.key(["models/org/x pull+push"]),
                        content=body)
    assert r.status_code == 200


def test_writes_need_a_credential(env):
    r = _commit(env, {}, ndjson(ADD))
    assert r.status_code == 401
    assert env.hub.calls == []


# ---------------------------------------------------------------- scopes


def test_a_scope_narrows_writes_as_it_narrows_pulls(env):
    h = env.key(["models/org/* pull+push+delete"], scope=["models/org/a pull+push"])
    assert _commit(env, h, ndjson(ADD), "/api/models/org/a/commit/main").status_code == 200
    r = _commit(env, h, ndjson(ADD), "/api/models/org/b/commit/main")
    assert r.status_code == 403
    assert "scoped away" in r.headers["x-error-message"]
    r = env.client.delete("/api/models/org/a/branch/dev", headers=h)
    assert r.status_code == 403, "the scope withheld delete"
    assert "scoped away" in r.headers["x-error-message"]
    r = _commit(env, h, ndjson(DEL_FILE), "/api/models/org/a/commit/main")
    assert r.status_code == 403
    assert len(env.hub.calls) == 1


# ---------------------------------------------------------------- audit


def test_every_write_is_audited_and_counted(env, caplog):
    from app import metrics

    h = env.key(["models/org/* pull+push"])
    key_id = h["authorization"].split()[1].split(":")[0]
    with caplog.at_level(logging.INFO, logger="xhc.hfwrites"):
        assert _commit(env, h, ndjson(ADD)).status_code == 200
        assert _commit(env, h, ndjson(DEL_FILE)).status_code == 403
        env.hub.status = 409
        assert _commit(env, h, ndjson(ADD)).status_code == 409
    lines = [r.getMessage() for r in caplog.records if r.name == "xhc.hfwrites"]
    assert len(lines) == 3, lines
    ok, denied, rejected = lines
    for line in lines:
        assert f"key={key_id}" in line and "principal=p" in line
        assert "method=POST path=/api/models/org/x/commit/main" in line
        assert "repo=models/org/x" in line
    assert ok.startswith("hf write forwarded") and "deletes=no" in ok and "status=200" in ok
    assert denied.startswith("hf write denied") and "deletes=yes" in denied
    assert "status=403" in denied and "reason=key" in denied
    assert rejected.startswith("hf write upstream_rejected") and "status=409" in rejected
    snap = metrics.snapshot()["hf_writes"]
    assert snap == {"forwarded": 1, "denied": 1, "upstream_rejected": 1,
                    "upstream_unreachable": 0, "too_large": 0, "invalid": 0}
    body = metrics.render({})
    assert 'muninn_hf_writes_total{result="forwarded"} 1' in body
    assert 'muninn_hf_writes_total{result="too_large"} 0' in body


def test_the_metric_exists_at_zero_before_any_write():
    from app import metrics

    metrics.reset()
    body = metrics.render({})
    for result in metrics._HF_WRITE_SERIES:
        assert f'muninn_hf_writes_total{{result="{result}"}} 0' in body


# ---------------------------------------------------------------- freshness


def test_a_commit_invalidates_the_cached_ref(env, monkeypatch):
    """Without the invalidation a pull of `main` keeps getting the old commit
    until XHC_REF_TTL runs out -- here, an hour."""
    import asyncio

    from app import hfcompat, refs
    from app.config import settings

    monkeypatch.setattr(settings, "ref_ttl_s", 3600.0)
    upstream_main = {"sha": "a" * 40}

    async def fetch(repo_type, repo_id, revision):
        return upstream_main["sha"]

    monkeypatch.setattr(refs, "_fetch_commit", fetch)
    ask = lambda: asyncio.run(refs.upstream_commit("model", "org/x", "main"))  # noqa: E731
    assert ask() == "a" * 40
    upstream_main["sha"] = NEW_COMMIT  # the Hub moved; the cache has not asked yet
    assert ask() == "a" * 40, "precondition: the ref is cached for the TTL"
    hfcompat._negative_cache_put("model", "org/x", "main", "a.txt", Exception("404"))

    h = env.key(["models/org/* pull+push"])
    assert _commit(env, h, ndjson(ADD)).status_code == 200
    assert ask() == NEW_COMMIT, "the commit did not invalidate the cached ref"
    assert hfcompat._negative_cache_get("model", "org/x", "main", "a.txt") is None


def test_a_refused_or_failed_write_invalidates_nothing(env, monkeypatch):
    from app import refs

    h = env.key(["models/org/* pull+push"])
    refs._cache[("model", "org/x", "main")] = ("a" * 40, time.monotonic())
    assert _commit(env, h, ndjson(DEL_FILE)).status_code == 403
    env.hub.status = 500
    assert _commit(env, h, ndjson(ADD)).status_code == 500
    assert ("model", "org/x", "main") in refs._cache


# ---------------------------------------------------------------- configuration


@pytest.mark.parametrize("extra,needle", [
    ({"XHC_HF_AUTH": "none"}, "XHC_HF_AUTH=key"),
    ({"XHC_HF_AUTH": "key", "XHC_HF_RULES": "off"}, "XHC_HF_RULES=enforce"),
])
def test_writes_on_refuses_to_start_where_no_write_could_be_authorised(
        monkeypatch, tmp_path, extra, needle):
    from app.config import HfWritesConfigError, Settings

    monkeypatch.setenv("XHC_HF_WRITES", "on")
    monkeypatch.setenv("XHC_AUTHZ_DB", str(tmp_path / "a.db"))
    monkeypatch.delenv("XHC_HF_RULES", raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(HfWritesConfigError, match=needle):
        Settings.from_env()


def test_writes_on_with_key_auth_and_rules_starts(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.setenv("XHC_HF_WRITES", "on")
    monkeypatch.setenv("XHC_HF_AUTH", "key")
    monkeypatch.setenv("XHC_AUTHZ_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("XHC_HF_WRITE_MAX_BODY", "1M")
    s = Settings.from_env()
    assert (s.hf_writes, s.hf_write_max_body) == ("on", 1024 * 1024)


def test_an_unknown_writes_value_is_refused(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("XHC_HF_WRITES", "yes")
    with pytest.raises(ValueError, match="off|on"):
        Settings.from_env()


# ---------------------------------------------------------------- rules


def test_an_hf_push_rule_is_refused_while_writes_are_off(monkeypatch):
    from app import authz
    from app.config import settings

    monkeypatch.setattr(settings, "hf_writes", "off")
    for line in ("models/org/* pull+push", "datasets/org/x push",
                 "models/org/* pull+push+delete"):
        with pytest.raises(authz.RuleSyntaxError, match="XHC_HF_WRITES=on"):
            authz.parse_rule(line)
    # A registry push rule and an HF pull rule are untouched.
    authz.parse_rule("ghcr.io/org/* pull+push")
    authz.parse_rule("models/org/* pull")


def test_the_console_refuses_it_too(monkeypatch):
    """The console submits structured rules and never reaches parse_rule."""
    from fastapi import HTTPException

    from app import console
    from app.config import settings

    monkeypatch.setattr(settings, "hf_writes", "off")
    with pytest.raises(HTTPException) as exc:
        console._checked([console.RuleIn(pattern="models/org/*", push=True)])
    assert "XHC_HF_WRITES" in exc.value.detail
    monkeypatch.setattr(settings, "hf_writes", "on")
    [rule] = console._checked([console.RuleIn(pattern="models/org/*", push=True,
                                              delete=True)])
    assert rule.delete


@pytest.mark.parametrize("line,expect", [
    ("models/org/* pull+push+delete", (True, True, True)),
    ("models/org/* delete+push", (False, True, True)),
    ("models/org/* push+pull", (True, True, False)),
    ("docker.io/* pull+push", (True, True, False)),
    ("models/org/*", (True, False, False)),
])
def test_the_verb_grammar_is_a_set(monkeypatch, line, expect):
    from app import authz
    from app.config import settings

    monkeypatch.setattr(settings, "hf_writes", "on")
    r = authz.parse_rule(line)
    assert (r.pull, r.push, r.delete) == expect


@pytest.mark.parametrize("line,why", [
    ("models/org/* pull+pull", "could not parse"),
    ("models/org/* pull+destroy", "could not parse"),
    ("models/org/* pull+delete", "without push"),
    ("docker.io/* pull+push+delete", "only to Hugging Face"),
    ("* pull+push+delete", "only to Hugging Face"),
])
def test_a_delete_that_could_never_take_effect_is_refused(monkeypatch, line, why):
    from app import authz
    from app.config import settings

    monkeypatch.setattr(settings, "hf_writes", "on")
    with pytest.raises(authz.RuleSyntaxError, match=why):
        authz.parse_rule(line)


def test_a_stored_hf_push_rule_is_inert_once_writes_are_off(env, monkeypatch, caplog):
    """Saved with writes on, then writes turned off: it stays stored, grants
    nothing, and startup counts it."""
    from app import main
    from app.config import settings

    h = env.key(["models/org/* pull+push+delete", "models/org/* pull"])
    monkeypatch.setattr(settings, "hf_writes", "off")
    assert _commit(env, h, ndjson(ADD)).status_code == 405
    assert env.client.delete("/api/models/org/x/branch/dev", headers=h).status_code == 405
    assert env.hub.writes() == []
    # Still stored, still readable, still pulls through the pull rule beside it.
    rules = env.store.get_principal_rules(_last["subject"])
    assert any(r.push and r.delete for r in rules)
    assert env.client.get("/api/models/org/x/refs", headers=h).status_code == 200
    with caplog.at_level(logging.WARNING, logger="xhc"):
        main._describe_hf_writes()
    assert any("grant NOTHING while XHC_HF_WRITES=off" in r.getMessage()
               and r.getMessage().startswith("1 stored rule") for r in caplog.records)


def test_the_delete_grant_survives_the_store_and_old_databases_migrate(tmp_path, monkeypatch):
    import sqlite3

    from app import authzadmin
    from app.authz import Rule
    from app.authzstore import AuthzStore

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE principals (subject TEXT PRIMARY KEY, email TEXT NOT NULL DEFAULT '',
            is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
        CREATE TABLE principal_rules (id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL, pattern TEXT NOT NULL,
            pull INTEGER NOT NULL DEFAULT 1, push INTEGER NOT NULL DEFAULT 0);
        INSERT INTO principals VALUES ('old', '', 0, 0, 'then');
        INSERT INTO principal_rules(subject, pattern, pull, push) VALUES ('old', 'x.io/*', 1, 1);
    """)
    c.commit()
    c.close()
    st = AuthzStore(db)
    assert st.get_principal_rules("old") == [Rule("x.io/*", True, True, False)]
    st.set_principal_rules("old", [Rule("models/o/*", True, True, True)])
    assert st.get_principal_rules("old") == [Rule("models/o/*", True, True, True)]
    # Serialised with `delete` only where it is granted: existing shapes unchanged.
    assert authzadmin.rule_out(Rule("x.io/*", True, True)) == {
        "pattern": "x.io/*", "pull": True, "push": True}
    assert authzadmin.rule_out(Rule("models/o/*", True, True, True))["delete"] is True


# ---------------------------------------------------------------- end to end


def _serve(app):
    import socket
    import threading

    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(250):
        if server.started:
            break
        time.sleep(0.02)
    return server, thread, f"http://127.0.0.1:{port}"


def test_upload_file_end_to_end_through_a_real_socket(env):
    """huggingface_hub's own upload path, unmodified, against the cache."""
    from huggingface_hub import HfApi
    from huggingface_hub.errors import HfHubHTTPError

    granted = env.key(["models/org/* pull+push"])["authorization"].split()[1]
    refused = env.key(["models/org/* pull"])["authorization"].split()[1]
    server, thread, endpoint = _serve(env.app)
    try:
        info = HfApi(endpoint=endpoint, token=granted).upload_file(
            path_or_fileobj=b"hello through the cache\n", path_in_repo="notes/hello.txt",
            repo_id="org/x", commit_message="via muninn")
        assert info.oid == NEW_COMMIT
        paths = [(c["method"], c["path"]) for c in env.hub.calls]
        assert ("POST", "/api/models/org/x/preupload/main") in paths
        assert ("POST", "/api/models/org/x/commit/main") in paths
        commit = next(c for c in env.hub.calls if c["path"].endswith("/commit/main"))
        ops = [json.loads(line) for line in commit["body"].splitlines()]
        assert [o["key"] for o in ops] == ["header", "file"]
        assert ops[1]["value"]["path"] == "notes/hello.txt"
        assert commit["headers"]["authorization"] == f"Bearer {CACHE_TOKEN}"

        before = len(env.hub.writes())
        with pytest.raises(HfHubHTTPError) as exc:
            HfApi(endpoint=endpoint, token=refused).upload_file(
                path_or_fileobj=b"nope\n", path_in_repo="nope.txt", repo_id="org/x")
        assert exc.value.response.status_code == 403
        assert "refused by this cache's rules" in str(exc.value)
        assert len(env.hub.writes()) == before, "a refused upload reached the Hub"

        with pytest.raises(HfHubHTTPError) as exc:
            HfApi(endpoint=endpoint, token=granted).delete_file("notes/hello.txt",
                                                               repo_id="org/x")
        assert exc.value.response.status_code == 403
        assert len(env.hub.writes()) == before, "a refused delete reached the Hub"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
