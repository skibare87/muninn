"""A fake S3-compatible store for the tier tests.

Deliberately imports nothing from app/, so a test built on it can run against a
tree that has no tier at all and fail on BEHAVIOUR rather than on an import.

It implements only what the tier uses -- GET, HEAD, PUT, ListObjectsV2 and the
multipart calls -- and records every request, so a test can assert what was
NOT asked (no DELETE of an object, no bucket-level call). It is a fake written
by the same hands as the client, so it shares their reading of the protocol;
the MinIO suite (tests/test_tier_minio.py) is the independent check.

Two adapters over one handler: an httpx.MockTransport for in-process tests,
and a real HTTP server for tests that go through the whole app.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import threading
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

# Captured at import, so a test counting content-hash passes in the app does not
# count the fake's own checks.
_sha256 = hashlib.sha256


@dataclass
class Obj:
    body: bytes
    content_type: str = "application/octet-stream"
    metadata: dict[str, str] = field(default_factory=dict)


class FakeS3:
    def __init__(self, bucket: str = "bkt"):
        self.bucket = bucket
        self.objects: dict[str, Obj] = {}
        self.uploads: dict[str, dict] = {}
        self.requests: list[tuple[str, str | None, dict]] = []
        # Failure injection.
        self.down = False  # every request raises a transport error
        self.status_for_all: int | None = None  # e.g. 401 for a dead credential
        self.missing_status = 404  # 403 imitates a credential without ListBucket
        self.fail_part_once: set[int] = set()  # part numbers that fail once
        self.check_content_sha = True  # imitate S3 verifying x-amz-content-sha256
        self.check_checksum_header = True  # imitate S3 verifying x-amz-checksum-sha256

    # -- helpers for tests ---------------------------------------------------

    def seed(self, key: str, body: bytes, content_type: str = "application/octet-stream"):
        self.objects[key] = Obj(body, content_type)

    def ops(self, method: str | None = None) -> list[tuple[str, str | None, dict]]:
        return [r for r in self.requests if method is None or r[0] == method]

    # -- the handler ---------------------------------------------------------

    def handle(self, method: str, raw_path: str, query: dict[str, str],
               headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        path = unquote(raw_path)
        prefix = f"/{self.bucket}"
        if not (path == prefix or path.startswith(prefix + "/")):
            return 400, {}, b"<Error><Code>WrongBucket</Code></Error>"
        key = path[len(prefix) + 1:] or None
        self.requests.append((method, key, dict(query)))
        auth = headers.get("authorization", "")
        if not (auth.startswith("AWS4-HMAC-SHA256 ") or auth.startswith("Bearer ")):
            return 403, {}, b"<Error><Code>AccessDenied</Code></Error>"
        if self.status_for_all is not None:
            return self.status_for_all, {}, b"<Error><Code>Forced</Code></Error>"

        if key is None:
            if method == "GET" and query.get("list-type") == "2":
                return self._list(query)
            # Any other bucket-level call: the client must never make one.
            return 403, {}, b"<Error><Code>BucketLevelCallRefused</Code></Error>"

        if method in ("GET", "HEAD"):
            obj = self.objects.get(key)
            if obj is None:
                return self.missing_status, {}, b"" if method == "HEAD" else b"<Error/>"
            hdrs = {"content-type": obj.content_type, "content-length": str(len(obj.body))}
            for k, v in obj.metadata.items():
                hdrs[k] = v
            return 200, hdrs, b"" if method == "HEAD" else obj.body

        if method == "PUT" and "uploadId" in query:
            n = int(query["partNumber"])
            if n in self.fail_part_once:
                self.fail_part_once.discard(n)
                raise httpx.ConnectError("injected part failure")
            up = self.uploads.get(query["uploadId"])
            if up is None:
                return 404, {}, b"<Error><Code>NoSuchUpload</Code></Error>"
            up["parts"][n] = body
            return 200, {"etag": f'"{hashlib.md5(body).hexdigest()}"'}, b""

        if method == "PUT":
            declared = headers.get("x-amz-content-sha256", "")
            actual = _sha256(body).hexdigest()
            if self.check_content_sha and len(declared) == 64 and declared != actual:
                return 400, {}, b"<Error><Code>XAmzContentSHA256Mismatch</Code></Error>"
            ck = headers.get("x-amz-checksum-sha256")
            if self.check_checksum_header and ck and base64.b64decode(ck).hex() != actual:
                return 400, {}, b"<Error><Code>BadDigest</Code></Error>"
            meta = {k: v for k, v in headers.items() if k.startswith(("x-amz-meta-", "x-goog-meta-"))}
            self.objects[key] = Obj(body, headers.get("content-type", "application/octet-stream"),
                                    meta)
            return 200, {"etag": '"x"'}, b""

        if method == "POST" and "uploads" in query:
            uid = uuid.uuid4().hex
            self.uploads[uid] = {"key": key, "parts": {},
                                 "ct": headers.get("content-type", "application/octet-stream")}
            return 200, {}, (f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId>"
                             "</InitiateMultipartUploadResult>").encode()

        if method == "POST" and "uploadId" in query:
            up = self.uploads.pop(query["uploadId"], None)
            if up is None:
                return 404, {}, b"<Error><Code>NoSuchUpload</Code></Error>"
            data = b"".join(up["parts"][n] for n in sorted(up["parts"]))
            self.objects[key] = Obj(data, up["ct"])
            return 200, {}, b"<CompleteMultipartUploadResult/>"

        if method == "DELETE" and "uploadId" in query:
            self.uploads.pop(query["uploadId"], None)
            return 204, {}, b""

        # A DELETE of an object: recorded above, refused here. Tests assert it
        # never happens.
        return 405, {}, b"<Error><Code>MethodNotAllowed</Code></Error>"

    def _list(self, query: dict[str, str]):
        pfx = query.get("prefix", "")
        keys = sorted(k for k in self.objects if k.startswith(pfx))
        start = int(query.get("continuation-token") or 0)
        page = keys[start:start + 2]  # tiny pages, so pagination is exercised
        more = start + 2 < len(keys)
        body = "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
        for k in page:
            body += f"<Contents><Key>{k}</Key><Size>{len(self.objects[k].body)}</Size></Contents>"
        body += f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
        if more:
            body += f"<NextContinuationToken>{start + 2}</NextContinuationToken>"
        body += "</ListBucketResult>"
        return 200, {"content-type": "application/xml"}, body.encode()

    # -- adapters ------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        def _h(request: httpx.Request) -> httpx.Response:
            if self.down:
                raise httpx.ConnectError("injected outage", request=request)
            q = dict(parse_qsl(request.url.query.decode(), keep_blank_values=True))
            status, hdrs, body = self.handle(
                request.method, request.url.raw_path.decode().split("?", 1)[0], q,
                {k.lower(): v for k, v in request.headers.items()}, request.read(),
            )
            return httpx.Response(status, headers=hdrs, content=body)

        return httpx.MockTransport(_h)

    def serve(self) -> tuple[str, http.server.ThreadingHTTPServer]:
        fake = self

        class _H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _do(self):
                parts = urlsplit(self.path)
                q = dict(parse_qsl(parts.query, keep_blank_values=True))
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                status, hdrs, out = fake.handle(
                    self.command, parts.path, q,
                    {k.lower(): v for k, v in self.headers.items()}, body,
                )
                self.send_response(status)
                for k, v in hdrs.items():
                    if k != "content-length":
                        self.send_header(k, v)
                length = hdrs.get("content-length") if self.command == "HEAD" else str(len(out))
                self.send_header("content-length", length or "0")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(out)

            do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _do

            def log_message(self, *a):
                return

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{srv.server_address[1]}", srv
