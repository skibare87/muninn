"""examples/k8s/provision.py: the init container that mints the model pods' key.

`authzctl mint` is not idempotent and the init container runs on every pod
start, so the script decides whether to mint. Each case here is a way that
decision could leak keys, undo a revocation, or strand the model pods with a
key that cannot authenticate. The real authzctl runs against a real database;
only the Kubernetes API is faked.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "examples" / "k8s" / "provision.py"
SUBJECT = "svc:model-pods"
LABEL = "k8s-model-pods"


class FakeApi:
    def __init__(self, secret: dict | None = None, get_status: int | None = None,
                 write_status: int = 201):
        self.ns = "muninn"
        self.secret = secret
        self.get_status = get_status
        self.write_status = write_status
        self.writes: list[tuple[str, str | None]] = []

    def get(self):
        if self.get_status is not None:
            return self.get_status, {"message": "forbidden"}
        if self.secret is None:
            return 404, {"message": "not found"}
        return 200, self.secret

    def write(self, token, key_id, replace_rv):
        self.writes.append((token, replace_rv))
        if self.write_status in (200, 201):
            self.secret = {
                "metadata": {"resourceVersion": "2", "annotations": {"muninn/key-id": key_id}},
                "data": {"HF_TOKEN": base64.b64encode(token.encode()).decode()},
            }
        return self.write_status, {"message": "nope"}


@pytest.fixture
def prov(tmp_path, monkeypatch):
    rules = tmp_path / "rules"
    rules.write_text("# comment\nmodels/Qwen/Qwen2.5-0.5B-Instruct pull\n")
    mint = tmp_path / "mint"
    mint.mkdir()
    monkeypatch.setenv("XHC_AUTHZ_DB", str(tmp_path / "authz.db"))
    monkeypatch.setenv("PROVISION_SUBJECT", SUBJECT)
    monkeypatch.setenv("PROVISION_KEY_LABEL", LABEL)
    monkeypatch.setenv("PROVISION_SECRET_NAME", "muninn-pod-key")
    monkeypatch.setenv("PROVISION_RULES_FILE", str(rules))
    monkeypatch.setenv("PROVISION_MINT_DIR", str(mint))
    # authzctl runs as `python -m app.authzctl`, exactly as in the image's /srv.
    monkeypatch.chdir(ROOT)
    spec = importlib.util.spec_from_file_location("k8s_provision", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.tmp = tmp_path
    return mod


def _keys(mod) -> list[dict]:
    out = subprocess.run([sys.executable, "-m", "app.authzctl", "list"],
                         capture_output=True, text=True, check=True, cwd=ROOT)
    return [k for k in json.loads(out.stdout)["keys"] if k["principal"] == SUBJECT]


def _run(mod, api, monkeypatch):
    monkeypatch.setattr(mod, "Api", lambda: api)
    return mod.main()


def test_first_run_mints_one_key_and_creates_the_secret(prov, monkeypatch, capfd):
    api = FakeApi()
    assert _run(prov, api, monkeypatch) == 0
    (key,) = _keys(prov)
    (token, rv) = api.writes[0]
    assert rv is None
    assert token.startswith(key["key_id"] + ":") and len(token) > len(key["key_id"]) + 16
    out = capfd.readouterr()
    secret = token.split(":", 1)[1]
    assert secret not in out.out and secret not in out.err, "the secret reached a log"
    assert not list((prov.tmp / "mint").iterdir()), "the minted secret file was left behind"


def test_rerun_with_the_secret_present_mints_nothing(prov, monkeypatch):
    api = FakeApi()
    _run(prov, api, monkeypatch)
    for _ in range(3):
        assert _run(prov, api, monkeypatch) == 0
    assert len(_keys(prov)) == 1
    assert len(api.writes) == 1


def test_a_disabled_key_is_left_disabled_not_replaced(prov, monkeypatch):
    api = FakeApi()
    _run(prov, api, monkeypatch)
    (key,) = _keys(prov)
    subprocess.run([sys.executable, "-m", "app.authzctl", "disable-key", key["key_id"]],
                   check=True, cwd=ROOT, capture_output=True)
    assert _run(prov, api, monkeypatch) == 0
    (after,) = _keys(prov)
    assert after["key_id"] == key["key_id"] and after["disabled"] is True
    assert len(api.writes) == 1, "a revocation was undone by re-minting"


def test_a_replaced_database_re_mints_and_replaces_the_secret(prov, monkeypatch):
    api = FakeApi()
    _run(prov, api, monkeypatch)
    old = api.secret["metadata"]["annotations"]["muninn/key-id"]
    Path(prov.os.environ["XHC_AUTHZ_DB"]).unlink()
    assert _run(prov, api, monkeypatch) == 0
    (key,) = _keys(prov)
    assert key["key_id"] != old
    assert api.writes[-1][1] == "2", "the stale Secret was not replaced in place"


def test_a_deleted_secret_rotates_and_removes_the_unheld_key(prov, monkeypatch):
    api = FakeApi()
    _run(prov, api, monkeypatch)
    (old,) = _keys(prov)
    api.secret = None
    assert _run(prov, api, monkeypatch) == 0
    (new,) = _keys(prov)
    assert new["key_id"] != old["key_id"]


def test_an_unreadable_answer_mints_nothing(prov, monkeypatch):
    with pytest.raises(SystemExit, match="HTTP 403"):
        _run(prov, FakeApi(get_status=403), monkeypatch)
    assert _keys(prov) == []


def test_a_failed_secret_write_leaves_no_live_key(prov, monkeypatch):
    with pytest.raises(SystemExit, match="deleted again"):
        _run(prov, FakeApi(write_status=409), monkeypatch)
    assert _keys(prov) == []
    assert not list((prov.tmp / "mint").iterdir())


def test_an_empty_rules_file_is_refused_before_anything_changes(prov, monkeypatch):
    Path(prov.RULES_FILE).write_text("# nothing\n\n")
    with pytest.raises(SystemExit, match="no rules"):
        _run(prov, FakeApi(), monkeypatch)
    assert not Path(prov.os.environ["XHC_AUTHZ_DB"]).exists() or _keys(prov) == []
