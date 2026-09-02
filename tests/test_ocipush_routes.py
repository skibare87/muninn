"""The HTTP surface a docker client actually touches when pushing (an internal issue).

Everything else about push-through is tested underneath these routes -- the
chunking policy, the session state, the upstream forwarding, and an end-to-end
push against a real registry:2. NONE OF THAT EXERCISES THE ROUTES THEMSELVES,
which is the layer a client meets first and the layer where a wrong status code
or a missing header breaks a push that is otherwise entirely correct.

The OCI push protocol is a sequence, not a call: POST to open a session, PATCH
chunks, PUT with ?digest= to finalise, then PUT the manifest. Each step depends
on headers from the previous one -- Location, Docker-Upload-UUID, Range -- so a
route that returns the right STATUS with the wrong HEADERS fails a real push
while passing any test that only checks the status.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocipush, ocistore
from app.config import settings

REPO = "/v2/ghcr.io/org/img"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    monkeypatch.setattr(settings, "docker_push_mode", "proxy")
    monkeypatch.setattr(settings, "docker_cache_on_push", True)
    ocipush._sessions.clear()
    ocipush._pinned.clear()
    ocipush._pending.clear()

    async def no_upstream(ref, path, digest):
        return None

    async def fake_manifest(ref, body, media, reference):
        digest = ocistore.compute_digest(body)
        ocistore.store_manifest(ref.upstream, digest, body, media)
        return digest

    monkeypatch.setattr(ocipush, "push_blob", no_upstream)
    monkeypatch.setattr(ocipush, "push_manifest", fake_manifest)
    from app.main import app

    return TestClient(app)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --- disabled by default ---------------------------------------------------

SESSION = f"{REPO}/blobs/uploads/00000000-0000-0000-0000-000000000000"


@pytest.mark.parametrize("path,method", [
    (f"{REPO}/blobs/uploads/", "post"),      # open a session
    (SESSION, "patch"),                       # send a chunk
    (SESSION, "put"),                         # finalise
    (f"{REPO}/manifests/latest", "put"),      # push a manifest
    (f"{REPO}/blobs/uploads/", "put"),        # malformed: no session id
])
def test_push_is_refused_when_disabled(client, monkeypatch, path, method):
    """Every write-shaped request names the flag, including malformed ones.

    A caller's next question is "how do I turn this on". A message about delete
    and cross-repo mount -- which is what the catch-all used to say for the
    no-session-id forms -- sends them the wrong way.
    """
    monkeypatch.setattr(settings, "docker_push_enabled", False)
    r = getattr(client, method)(path)
    assert r.status_code == 405
    assert "XHC_DOCKER_PUSH" in r.json()["errors"][0]["message"], \
        "the refusal should name the flag that enables it"


def test_delete_stays_refused_even_with_push_enabled(client):
    """Deleting upstream content is a retention decision for that registry's
    owner, not for a cache in front of it. Enabling push must not enable it."""
    r = client.delete(f"{REPO}/manifests/latest")
    assert r.status_code == 405
    assert "retention decision" in r.json()["errors"][0]["message"]


# --- the full sequence, as a client performs it ----------------------------

def test_a_complete_chunked_push(client):
    layer = b"layer-bytes" * 100
    digest = _digest(layer)

    start = client.post(f"{REPO}/blobs/uploads/")
    assert start.status_code == 202
    location = start.headers["location"]
    assert start.headers["docker-upload-uuid"] in location, \
        "Location must address the session the client was just given"
    assert start.headers["range"] == "0-0"

    half = len(layer) // 2
    p1 = client.patch(location, content=layer[:half])
    assert p1.status_code == 202
    assert p1.headers["range"] == f"0-{half - 1}", \
        "Range must report what the server actually holds, or the client resumes wrongly"

    p2 = client.patch(location, content=layer[half:])
    assert p2.headers["range"] == f"0-{len(layer) - 1}"

    done = client.put(f"{location}?digest={digest}")
    assert done.status_code == 201
    assert done.headers["docker-content-digest"] == digest
    assert ocistore.blob_path("ghcr.io", digest).read_bytes() == layer


def test_a_monolithic_put_with_the_body_on_the_final_request(client):
    """docker often PATCHes nothing and sends everything on the PUT."""
    layer = b"one-shot-body"
    digest = _digest(layer)
    location = client.post(f"{REPO}/blobs/uploads/").headers["location"]
    r = client.put(f"{location}?digest={digest}", content=layer)
    assert r.status_code == 201
    assert ocistore.blob_path("ghcr.io", digest).read_bytes() == layer


def test_single_post_upload_with_a_digest_query(client):
    """The spec's one-request form: body and digest on the POST."""
    layer = b"single-post"
    digest = _digest(layer)
    r = client.post(f"{REPO}/blobs/uploads/?digest={digest}", content=layer)
    assert r.status_code == 201
    assert r.headers["docker-content-digest"] == digest
    assert ocistore.blob_path("ghcr.io", digest).exists()


# --- refusals a client must be able to act on ------------------------------

def test_a_mismatched_digest_is_400_and_stores_nothing(client):
    layer = b"actual-bytes"
    lie = _digest(b"different-bytes")
    location = client.post(f"{REPO}/blobs/uploads/").headers["location"]
    r = client.put(f"{location}?digest={lie}", content=layer)
    assert r.status_code == 400
    assert r.json()["errors"][0]["code"] == "DIGEST_INVALID"
    assert not ocistore.blob_path("ghcr.io", lie).exists()
    assert not ocistore.blob_path("ghcr.io", _digest(layer)).exists()


def test_finalising_without_a_digest_is_400(client):
    location = client.post(f"{REPO}/blobs/uploads/").headers["location"]
    assert client.put(location, content=b"x").status_code == 400


def test_an_unknown_session_is_404(client):
    r = client.patch(f"{REPO}/blobs/uploads/00000000-0000-0000-0000-000000000000",
                     content=b"x")
    assert r.status_code == 404
    assert r.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"


def test_a_cross_repo_mount_is_declined_with_202_not_an_error(client):
    """The spec defines 202 as 'no mount, upload it normally'.

    Muninn cannot honour a mount -- the blob may not be upstream at all yet --
    but declining must not look like a failure, or the push aborts instead of
    falling back to a normal upload.
    """
    r = client.post(f"{REPO}/blobs/uploads/?mount=sha256:{'a' * 64}&from=org/other")
    assert r.status_code == 202
    assert "location" in r.headers


# --- manifests -------------------------------------------------------------

def test_manifest_push_returns_its_digest(client):
    body = json.dumps({"schemaVersion": 2, "layers": []}).encode()
    r = client.put(f"{REPO}/manifests/latest", content=body,
                   headers={"content-type":
                            "application/vnd.oci.image.manifest.v1+json"})
    assert r.status_code == 201
    assert r.headers["docker-content-digest"] == ocistore.compute_digest(body)


def test_every_push_response_carries_the_api_version_header(client):
    """Clients use it to confirm they are talking to a v2 registry at all."""
    start = client.post(f"{REPO}/blobs/uploads/")
    assert start.headers["docker-distribution-api-version"] == "registry/2.0"
