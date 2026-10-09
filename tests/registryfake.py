"""A fake OCI registry with a separate bearer-token endpoint, for the restore tests.

Deliberately imports nothing from app/, so a test built on it runs against a
tree with no OCI restore at all and fails on BEHAVIOUR rather than on an import.

Two real HTTP servers, because the token endpoint and the registry fail
independently in the world and the cache classifies them differently:

- the REGISTRY answers /v2/<repo>/manifests/<ref> and /v2/<repo>/blobs/<digest>,
  and challenges any request without the token the token endpoint issues;
- the TOKEN ENDPOINT issues that token, or refuses (401/403), or fails (5xx),
  or stops listening.

Each can be flipped to answer a chosen status and OCI error code, or to stop
accepting connections on the same port (so keys carrying the upstream's name do
not change).

The image is a real multi-arch shape: an OCI index naming two platform
manifests, each with a config blob and one layer, every digest computed here
from the bytes.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import socket
import threading
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

INDEX_MEDIA = "application/vnd.oci.image.index.v1+json"
MANIFEST_MEDIA = "application/vnd.oci.image.manifest.v1+json"
CONFIG_MEDIA = "application/vnd.oci.image.config.v1+json"
LAYER_MEDIA = "application/vnd.oci.image.layer.v1.tar+gzip"
# What docker 29 sends, as one comma-joined line.
DOCKER_ACCEPT = ", ".join([
    INDEX_MEDIA, MANIFEST_MEDIA,
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
TOKEN = "issued-by-the-fake-token-endpoint"


def digest_of(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _canon(d: dict) -> bytes:
    return json.dumps(d, separators=(",", ":"), sort_keys=True).encode()


@dataclass
class Image:
    """One multi-arch image: an index, two platform manifests, their blobs."""

    seed: str = "v1"
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifests: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    index: bytes = b""
    index_digest: str = ""
    platforms: dict[str, str] = field(default_factory=dict)  # arch -> manifest digest

    def __post_init__(self):
        children = []
        for arch in ("amd64", "arm64"):
            config = _canon({"architecture": arch, "os": "linux", "seed": self.seed})
            layer = (f"layer {arch} {self.seed} ".encode() * 3000)[:30_000]
            for b in (config, layer):
                self.blobs[digest_of(b)] = b
            m = _canon({
                "schemaVersion": 2, "mediaType": MANIFEST_MEDIA,
                "config": {"mediaType": CONFIG_MEDIA, "digest": digest_of(config),
                           "size": len(config)},
                "layers": [{"mediaType": LAYER_MEDIA, "digest": digest_of(layer),
                            "size": len(layer)}],
            })
            self.manifests[digest_of(m)] = (m, MANIFEST_MEDIA)
            self.platforms[arch] = digest_of(m)
            children.append({"mediaType": MANIFEST_MEDIA, "digest": digest_of(m),
                             "size": len(m),
                             "platform": {"architecture": arch, "os": "linux"}})
        self.index = _canon({"schemaVersion": 2, "mediaType": INDEX_MEDIA,
                             "manifests": children})
        self.index_digest = digest_of(self.index)
        self.manifests[self.index_digest] = (self.index, INDEX_MEDIA)


class _State:
    def __init__(self):
        self.tags: dict[str, str] = {}  # "<repo>:<tag>" -> manifest digest
        self.manifests: dict[str, tuple[bytes, str]] = {}
        self.blobs: dict[str, bytes] = {}
        self.status: int | None = None  # forced registry answer
        self.code: str | None = None
        self.token_status: int = 200  # token endpoint answer
        self.log: list[str] = []
        self.token_log: list[str] = []
        self.token_url = ""


def _err_body(code: str | None) -> bytes:
    if not code:
        return b""
    return json.dumps({"errors": [{"code": code, "message": code.lower()}]}).encode()


def _make_registry_handler(state: _State):
    class H(http.server.BaseHTTPRequestHandler):
        def _send(self, status, headers, body=b""):
            self.send_response(status)
            headers = {"content-length": str(len(body)), **headers}
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _serve(self):
            path = unquote(urlparse(self.path).path)
            state.log.append(f"{self.command} {path}")
            if self.headers.get("authorization") != f"Bearer {TOKEN}":
                return self._send(401, {
                    "www-authenticate": (f'Bearer realm="{state.token_url}",'
                                         'service="fake-registry"'),
                    "content-type": "application/json"}, _err_body("UNAUTHORIZED"))
            if state.status is not None:
                return self._send(state.status, {"content-type": "application/json"},
                                  _err_body(state.code))
            if not path.startswith("/v2/"):
                return self._send(404, {}, b"")
            rest = path[len("/v2/"):]
            if "/manifests/" in rest:
                repo, _, ref = rest.rpartition("/manifests/")
                digest = ref if ref.startswith("sha256:") else state.tags.get(f"{repo}:{ref}")
                got = state.manifests.get(digest or "")
                if got is None:
                    return self._send(404, {"content-type": "application/json"},
                                      _err_body("MANIFEST_UNKNOWN"))
                body, media = got
                return self._send(200, {"content-type": media,
                                        "docker-content-digest": digest}, body)
            if "/blobs/" in rest:
                _, _, digest = rest.rpartition("/blobs/")
                body = state.blobs.get(digest)
                if body is None:
                    return self._send(404, {"content-type": "application/json"},
                                      _err_body("BLOB_UNKNOWN"))
                return self._send(200, {"content-type": "application/octet-stream",
                                        "docker-content-digest": digest}, body)
            return self._send(404, {}, b"")

        do_GET = do_HEAD = _serve

        def log_message(self, *a):
            return

    return H


def _make_token_handler(state: _State):
    class T(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state.token_log.append(self.path)
            if state.token_status != 200:
                body = b'{"details":"refused"}'
                self.send_response(state.token_status)
            else:
                body = json.dumps({"token": TOKEN, "expires_in": 300}).encode()
                self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            return

    return T


class _Server:
    """A ThreadingHTTPServer that can stop listening and come back on the same port."""

    def __init__(self, handler):
        self.handler = handler
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.srv.server_address[1]
        self.up = False
        self.start()

    def start(self):
        if self.up:
            return
        if self.srv is None:
            self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), self.handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.up = True

    def refuse(self):
        if self.up:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None
            self.up = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class FakeRegistry:
    def __init__(self):
        self.state = _State()
        self.registry = _Server(_make_registry_handler(self.state))
        self.tokens = _Server(_make_token_handler(self.state))
        self.state.token_url = f"{self.tokens.url}/token"

    @property
    def url(self) -> str:
        return self.registry.url

    @property
    def log(self) -> list[str]:
        return self.state.log

    def push(self, repo: str, tag: str, image: Image) -> None:
        self.state.manifests.update(image.manifests)
        self.state.blobs.update(image.blobs)
        self.state.tags[f"{repo}:{tag}"] = image.index_digest

    def delete_tag(self, repo: str, tag: str) -> None:
        self.state.tags.pop(f"{repo}:{tag}", None)

    def answer(self, status: int | None, code: str | None = None) -> None:
        """Every authenticated registry request gets this status and error code."""
        self.state.status, self.state.code = status, code

    def token_answer(self, status: int) -> None:
        self.state.token_status = status

    def refuse(self) -> None:
        self.registry.refuse()

    def refuse_tokens(self) -> None:
        self.tokens.refuse()

    def close(self) -> None:
        self.registry.refuse()
        self.tokens.refuse()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
