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

BLOBS AND MANIFESTS ARE SCOPED PER REPOSITORY, as a real registry scopes them.
A blob pushed to (or mounted into) repository A is NOT visible from repository
B: HEAD and GET on B answer 404 until it is uploaded or mounted there. A fake
that kept one global blob table would hide exactly the defect where a blob
present in one repository was assumed present in another.

It also accepts pushes: POST/PATCH/PUT upload sessions (with cross-repository
mount), and manifest PUTs. By default it accepts a manifest WITHOUT checking
that the repository holds what it references -- as some real registries do --
and records, at the moment of every manifest PUT, which referenced blobs were
missing. `events` is one ordered log of every blob HEAD answer and every
manifest PUT, so a test can assert what was confirmed before a manifest arrived.

OAUTH MODE (`require_oauth`, or a FakeMetadata tied to the registry) is how
Google Artifact Registry authenticates: the token endpoint accepts only Basic
`oauth2accesstoken:<t>` for a currently valid OAuth token t, and a bearer token
works only while the OAuth token it was exchanged for is still valid. FakeMetadata
is the metadata server (and Google's token endpoint for the service-account
flow) that issues those OAuth tokens; rotating it revokes the old one.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import socket
import threading
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

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
        self.manifests: dict[str, tuple[bytes, str]] = {}  # content, by digest
        self.blobs: dict[str, bytes] = {}                    # content, by digest
        # What each REPOSITORY holds. Content above is shared storage; whether
        # a repository can see it is decided here, as a registry decides it.
        self.repo_blobs: dict[str, set[str]] = {}
        self.repo_manifests: dict[str, set[str]] = {}
        self.uploads: dict[str, dict] = {}   # session uuid -> {"repo", "data"}
        # Ordered: ("HEAD", repo, digest, status) and
        # ("MANIFEST_PUT", repo, reference, digest, missing-at-that-moment).
        self.events: list[tuple] = []
        self.verify_manifests = False
        # Answer the finalising PUT of the next `n` uploads with `status`. With
        # `commits`, the blob is stored first: a registry that errors AFTER it
        # has committed.
        self.finalise_fail: dict = {"n": 0, "status": 500, "commits": True}
        self.mounts: list[tuple[str, str, str, int]] = []  # (repo, digest, from, status)
        self.lock = threading.Lock()
        self.status: int | None = None  # forced registry answer
        self.code: str | None = None
        self.token_status: int = 200  # token endpoint answer
        self.log: list[str] = []
        self.token_log: list[str] = []
        self.token_url = ""
        # OAUTH MODE, as Google Artifact Registry authenticates. None: off, and
        # the token endpoint issues TOKEN to anyone. A set: the token endpoint
        # accepts only Basic `oauth2accesstoken:<t>` for a t in it, issues a
        # bearer token DERIVED from t, and the registry accepts a bearer token
        # only while the OAuth token it came from is still in this set -- so
        # revoking t (expiry) also kills every bearer token exchanged for it.
        self.oauth_valid: set[str] | None = None
        self.oauth_seen: list[str | None] = []  # the OAuth token each exchange presented
        self.bearer_ttl = 3600

    def bearer_ok(self, header: str | None) -> bool:
        if self.oauth_valid is None:
            return header == f"Bearer {TOKEN}"
        prefix = f"Bearer {TOKEN}+"
        return bool(header and header.startswith(prefix)
                    and header[len(prefix):] in self.oauth_valid)


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
            if not state.bearer_ok(self.headers.get("authorization")):
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
                held = digest in state.repo_manifests.get(repo, set())
                got = state.manifests.get(digest or "") if held else None
                if got is None:
                    return self._send(404, {"content-type": "application/json"},
                                      _err_body("MANIFEST_UNKNOWN"))
                body, media = got
                return self._send(200, {"content-type": media,
                                        "docker-content-digest": digest}, body)
            if "/blobs/" in rest and "/blobs/uploads" not in rest:
                repo, _, digest = rest.rpartition("/blobs/")
                held = digest in state.repo_blobs.get(repo, set())
                body = state.blobs.get(digest) if held else None
                if self.command == "HEAD":
                    state.events.append(("HEAD", repo, digest,
                                         200 if body is not None else 404))
                if body is None:
                    return self._send(404, {"content-type": "application/json"},
                                      _err_body("BLOB_UNKNOWN"))
                return self._send(200, {"content-type": "application/octet-stream",
                                        "docker-content-digest": digest}, body)
            return self._send(404, {}, b"")

        do_GET = do_HEAD = _serve

        # --- push ---------------------------------------------------------

        def _body(self) -> bytes:
            n = int(self.headers.get("content-length") or 0)
            return self.rfile.read(n) if n else b""

        def _authed(self) -> bool:
            if state.bearer_ok(self.headers.get("authorization")):
                return True
            self._body()
            self._send(401, {
                "www-authenticate": (f'Bearer realm="{state.token_url}",'
                                     'service="fake-registry"'),
                "content-type": "application/json"}, _err_body("UNAUTHORIZED"))
            return False

        def _open_session(self, repo: str) -> None:
            u = uuidlib.uuid4().hex
            state.uploads[u] = {"repo": repo, "data": bytearray()}
            self._send(202, {"location": f"/v2/{repo}/blobs/uploads/{u}",
                             "docker-upload-uuid": u, "range": "0-0"})

        def _commit(self, repo: str, data: bytes) -> str:
            d = digest_of(data)
            with state.lock:
                state.blobs[d] = data
                state.repo_blobs.setdefault(repo, set()).add(d)
            return d

        def do_POST(self):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            state.log.append(f"POST {unquote(self.path)}")
            if not self._authed():
                return None
            body = self._body()
            if state.status is not None:
                return self._send(state.status, {"content-type": "application/json"},
                                  _err_body(state.code))
            rest = path[len("/v2/"):]
            if not rest.endswith("/blobs/uploads/"):
                return self._send(404, {}, b"")
            repo = rest[: -len("/blobs/uploads/")]
            q = parse_qs(parsed.query)
            mount, src = q.get("mount", [None])[0], q.get("from", [None])[0]
            if mount:
                if src and mount in state.repo_blobs.get(src, set()):
                    state.repo_blobs.setdefault(repo, set()).add(mount)
                    state.mounts.append((repo, mount, src, 201))
                    return self._send(201, {"location": f"/v2/{repo}/blobs/{mount}",
                                            "docker-content-digest": mount})
                # The spec's answer to a mount it cannot honour: an ordinary
                # upload session, which the client then uses.
                state.mounts.append((repo, mount, src or "", 202))
                return self._open_session(repo)
            want = q.get("digest", [None])[0]
            if want:
                if digest_of(body) != want:
                    return self._send(400, {}, _err_body("DIGEST_INVALID"))
                self._commit(repo, body)
                return self._send(201, {"location": f"/v2/{repo}/blobs/{want}",
                                        "docker-content-digest": want})
            return self._open_session(repo)

        def do_PATCH(self):
            path = unquote(urlparse(self.path).path)
            state.log.append(f"PATCH {path}")
            if not self._authed():
                return None
            body = self._body()
            u = path.rsplit("/", 1)[1]
            sess = state.uploads.get(u)
            if sess is None:
                return self._send(404, {}, _err_body("BLOB_UPLOAD_UNKNOWN"))
            sess["data"] += body
            return self._send(202, {"location": f"/v2/{sess['repo']}/blobs/uploads/{u}",
                                    "range": f"0-{max(len(sess['data']) - 1, 0)}"})

        def do_PUT(self):
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            state.log.append(f"PUT {path}")
            if not self._authed():
                return None
            body = self._body()
            if state.status is not None:
                return self._send(state.status, {"content-type": "application/json"},
                                  _err_body(state.code))
            rest = path[len("/v2/"):]
            if "/manifests/" in rest:
                repo, _, ref = rest.rpartition("/manifests/")
                return self._put_manifest(repo, ref, body)
            if "/blobs/uploads/" in rest:
                u = rest.rsplit("/", 1)[1]
                sess = state.uploads.pop(u, None)
                if sess is None:
                    return self._send(404, {}, _err_body("BLOB_UPLOAD_UNKNOWN"))
                data = bytes(sess["data"]) + body
                want = parse_qs(parsed.query).get("digest", [None])[0]
                if not want or digest_of(data) != want:
                    return self._send(400, {}, _err_body("DIGEST_INVALID"))
                fail = state.finalise_fail
                if fail["n"] > 0:
                    fail["n"] -= 1
                    if fail["commits"]:
                        self._commit(sess["repo"], data)
                    return self._send(fail["status"], {"content-type": "application/json"},
                                      _err_body("UNKNOWN"))
                self._commit(sess["repo"], data)
                return self._send(201, {"location": f"/v2/{sess['repo']}/blobs/{want}",
                                        "docker-content-digest": want})
            return self._send(404, {}, b"")

        def _put_manifest(self, repo: str, ref: str, body: bytes):
            media = self.headers.get("content-type") or MANIFEST_MEDIA
            d = digest_of(body)
            try:
                doc = json.loads(body)
            except ValueError:
                return self._send(400, {}, _err_body("MANIFEST_INVALID"))
            refs = [doc.get("config") or {}, *(doc.get("layers") or [])]
            held = state.repo_blobs.get(repo, set())
            missing = [x["digest"] for x in refs if x.get("digest") and x["digest"] not in held]
            missing += [m["digest"] for m in doc.get("manifests") or []
                        if m.get("digest")
                        and m["digest"] not in state.repo_manifests.get(repo, set())]
            state.events.append(("MANIFEST_PUT", repo, ref, d, tuple(missing)))
            if missing and state.verify_manifests:
                return self._send(400, {"content-type": "application/json"},
                                  _err_body("MANIFEST_BLOB_UNKNOWN"))
            with state.lock:
                state.manifests[d] = (body, media)
                state.repo_manifests.setdefault(repo, set()).add(d)
                if not ref.startswith("sha256:"):
                    state.tags[f"{repo}:{ref}"] = d
            return self._send(201, {"location": f"/v2/{repo}/manifests/{d}",
                                    "docker-content-digest": d})

        def log_message(self, *a):
            return

    return H


def _oauth_from_basic(header: str | None) -> str | None:
    """The OAuth token in `Basic base64(oauth2accesstoken:<t>)`, else None."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        user, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except ValueError:
        return None
    return password if user == "oauth2accesstoken" else None


def _make_token_handler(state: _State):
    class T(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            state.token_log.append(self.path)
            oauth = None
            if state.oauth_valid is not None:
                oauth = _oauth_from_basic(self.headers.get("authorization"))
                state.oauth_seen.append(oauth)
            if state.token_status != 200:
                body = b'{"details":"refused"}'
                self.send_response(state.token_status)
            elif state.oauth_valid is not None and oauth not in state.oauth_valid:
                body = b'{"details":"invalid or expired oauth2accesstoken"}'
                self.send_response(401)
            elif state.oauth_valid is not None:
                body = json.dumps({"token": f"{TOKEN}+{oauth}",
                                   "expires_in": state.bearer_ttl}).encode()
                self.send_response(200)
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

    @property
    def events(self) -> list[tuple]:
        return self.state.events

    def push(self, repo: str, tag: str, image: Image) -> None:
        """Seed `repo` with the whole image. Only that repository can see it."""
        self.state.manifests.update(image.manifests)
        self.state.blobs.update(image.blobs)
        self.state.repo_manifests.setdefault(repo, set()).update(image.manifests)
        self.state.repo_blobs.setdefault(repo, set()).update(image.blobs)
        self.state.tags[f"{repo}:{tag}"] = image.index_digest

    def has_blob(self, repo: str, digest: str) -> bool:
        return digest in self.state.repo_blobs.get(repo, set())

    def manifest_puts(self) -> list[tuple]:
        return [e for e in self.state.events if e[0] == "MANIFEST_PUT"]

    def delete_tag(self, repo: str, tag: str) -> None:
        self.state.tags.pop(f"{repo}:{tag}", None)

    def answer(self, status: int | None, code: str | None = None) -> None:
        """Every authenticated registry request gets this status and error code."""
        self.state.status, self.state.code = status, code

    def token_answer(self, status: int) -> None:
        self.state.token_status = status

    def require_oauth(self, *valid: str) -> None:
        """Artifact-Registry mode: only these OAuth tokens are accepted."""
        self.state.oauth_valid = set(valid)

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


class FakeMetadata:
    """A GCE/GKE metadata server's token endpoint, plus a Google OAuth token
    endpoint for the service-account flow. One real HTTP server.

    GET  /computeMetadata/v1/instance/service-accounts/default/token
         needs `Metadata-Flavor: Google`; answers the CURRENT token.
    POST /token  the JWT-bearer grant; records each form and answers the
         current token.

    `rotate()` issues a new current token and, when tied to a FakeRegistry,
    revokes the old one there -- which is what expiry means to the registry.
    `delay_s` holds every answer, so concurrent callers overlap; `status`
    forces an error answer; `body` replaces a 200's body.
    """

    PATH = "/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(self, registry: FakeRegistry | None = None, ttl: int = 3600):
        self.registry = registry
        self.ttl = ttl
        self.n = 0
        self.current = ""
        self.status = 200
        self.body: bytes | None = None
        self.delay_s = 0.0
        self.requests: list[str] = []
        self.forms: list[dict] = []
        self.rotate()
        fake = self

        class M(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                if fake.delay_s:
                    time.sleep(fake.delay_s)
                if fake.status != 200:
                    body, status = b'{"error":"forced"}', fake.status
                else:
                    body = fake.body if fake.body is not None else json.dumps(
                        {"access_token": fake.current, "expires_in": fake.ttl,
                         "token_type": "Bearer"}).encode()
                    status = 200
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.requests.append(f"GET {self.path}")
                if (urlparse(self.path).path != FakeMetadata.PATH
                        or self.headers.get("metadata-flavor") != "Google"):
                    self.send_response(403)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                self._answer()

            def do_POST(self):
                fake.requests.append(f"POST {self.path}")
                n = int(self.headers.get("content-length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
                fake.forms.append(form)
                self._answer()

            def log_message(self, *a):
                return

        self.server = _Server(M)

    def rotate(self) -> str:
        old = self.current
        self.n += 1
        self.current = f"oauth-{self.n}"
        if self.registry is not None:
            valid = self.registry.state.oauth_valid or set()
            valid.discard(old)
            valid.add(self.current)
            self.registry.state.oauth_valid = valid
        return self.current

    @property
    def token_url(self) -> str:
        return f"{self.server.url}{self.PATH}"

    @property
    def oauth_url(self) -> str:
        return f"{self.server.url}/token"

    def close(self) -> None:
        self.server.refuse()
