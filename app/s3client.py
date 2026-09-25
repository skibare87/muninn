"""A minimal S3-compatible object client on httpx, for the second tier.

Covers exactly what the tier needs and nothing else: GET (streamed), HEAD, PUT,
ListObjectsV2 under a prefix, and multipart create / part / complete / abort.
It speaks SigV4 for static keys (AWS, R2, MinIO, B2, GCS HMAC interop) and an
OAuth bearer token for the GCS XML API under workload identity.

WHAT IT NEVER DOES, BY CONSTRUCTION: a bucket-level call. There is no
CreateBucket, HeadBucket or ListBuckets method to call. A token scoped to one
bucket fails those by design (403 on R2), and a client that issues one reports
an error naming an operation nobody asked for. ListObjectsV2 is addressed to the
bucket but is a read of keys under a prefix; it is the one exception, and the
tier needs list permission anyway (see the probe in app/tier.py).

WHY NOT boto3: it is synchronous, it would add tens of MB to a public image,
and the tier needs a response it can stream into the same hash-while-writing
loop the OCI path uses. This returns httpx.Response.

AWS role credentials (IRSA, instance profiles) are not implemented.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

import httpx

log = logging.getLogger("xhc.s3client")

# Captured at import: signing and payload hashes are not content hashes, and a
# test that counts content-hash passes must not see them.
_sha256 = hashlib.sha256
EMPTY_SHA256 = _sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
GCP_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
# Refresh this long before the token's stated expiry, so a request never
# carries a token that expires in flight.
_TOKEN_SLACK_S = 300.0


class TierAuthError(Exception):
    """The store answered 401: the credential is dead, not merely scoped."""


class TierHTTPError(Exception):
    def __init__(self, status: int, op: str, body: str = ""):
        super().__init__(f"{op} returned {status}: {body[:300]}")
        self.status = status
        self.op = op


# ---------------------------------------------------------------------------
# SigV4
# ---------------------------------------------------------------------------


def _uri_encode(s: str, safe: str) -> str:
    # RFC 3986 unreserved characters are never encoded; everything else is.
    return quote(s, safe="-_.~" + safe)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), _sha256).digest()


def sign_v4(
    *,
    method: str,
    host: str,
    canonical_uri: str,
    query: dict[str, str] | None,
    headers: dict[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    amz_date: str,
    sign_payload_header: bool = True,
) -> dict[str, str]:
    """Return the headers to add for a SigV4-signed request.

    `canonical_uri` is the path exactly as it will go on the wire (already
    percent-encoded, once). `headers` are the request's own headers; every one
    of them is signed, plus host and x-amz-date. With `sign_payload_header` the
    payload hash is also sent and signed as x-amz-content-sha256, which S3
    requires; the published generic test vectors do not carry it.
    """
    datestamp = amz_date[:8]
    all_headers = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    all_headers["host"] = host
    all_headers["x-amz-date"] = amz_date
    if sign_payload_header:
        all_headers["x-amz-content-sha256"] = payload_hash
    names = sorted(all_headers)
    canonical_headers = "".join(f"{n}:{all_headers[n]}\n" for n in names)
    signed_headers = ";".join(names)
    q = query or {}
    canonical_query = "&".join(
        f"{_uri_encode(k, '')}={_uri_encode(v, '')}" for k, v in sorted(q.items())
    )
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            _sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    k = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    k = _hmac(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), _sha256).hexdigest()
    out = {
        "x-amz-date": amz_date,
        "authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if sign_payload_header:
        out["x-amz-content-sha256"] = payload_hash
    return out


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------


class StaticKeys:
    kind = "static"

    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self._secret = secret_key

    @property
    def secret_key(self) -> str:
        return self._secret

    def __repr__(self) -> str:  # never print the secret
        return f"StaticKeys({self.access_key[:4]}...)"


class GcpMetadataToken:
    """An OAuth token from the GKE/GCE metadata server (workload identity).

    Refreshed before it expires. The XML API is then called with
    `Authorization: Bearer`, with no SigV4 at all.
    """

    kind = "gcp-metadata"

    def __init__(self, client: httpx.AsyncClient, url: str = GCP_TOKEN_URL):
        self._client = client
        self._url = url
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at - _TOKEN_SLACK_S:
                return self._token
            r = await self._client.get(self._url, headers={"Metadata-Flavor": "Google"})
            if r.status_code != 200:
                raise TierAuthError(f"metadata server returned {r.status_code} for a token")
            body = r.json()
            self._token = body["access_token"]
            self._expires_at = time.time() + float(body.get("expires_in", 0))
            return self._token

    def __repr__(self) -> str:
        return "GcpMetadataToken()"


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------


@dataclass
class ListedObject:
    key: str
    size: int


_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _find(el: ET.Element, name: str) -> ET.Element | None:
    found = el.find(_S3_NS + name)
    return found if found is not None else el.find(name)


def _findall(el: ET.Element, name: str) -> list[ET.Element]:
    return el.findall(_S3_NS + name) or el.findall(name)


class S3Client:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        path_style: bool,
        creds: StaticKeys | GcpMetadataToken,
        client: httpx.AsyncClient,
        gcs: bool = False,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.path_style = path_style
        self.creds = creds
        self.gcs = gcs
        self._client = client
        scheme, rest = self.endpoint.split("://", 1)
        self._scheme = scheme
        self._host = rest if path_style else f"{bucket}.{rest}"

    @property
    def meta_prefix(self) -> str:
        """User-metadata header prefix. GCS's XML API under OAuth uses its own."""
        return "x-goog-meta-" if self.gcs and self.creds.kind == "gcp-metadata" else "x-amz-meta-"

    def _path(self, key: str | None) -> str:
        enc = _uri_encode(key, "/") if key else ""
        if self.path_style:
            return f"/{self.bucket}/{enc}" if key is not None else f"/{self.bucket}"
        return f"/{enc}" if key is not None else "/"

    async def _auth_headers(
        self, method: str, path: str, query: dict[str, str] | None, headers: dict[str, str],
        payload_hash: str,
    ) -> dict[str, str]:
        if isinstance(self.creds, GcpMetadataToken):
            return {"authorization": f"Bearer {await self.creds.token()}"}
        amz_date = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        return sign_v4(
            method=method,
            host=self._host,
            canonical_uri=path,
            query=query,
            headers=headers,
            payload_hash=payload_hash,
            access_key=self.creds.access_key,
            secret_key=self.creds.secret_key,
            region=self.region,
            service="s3",
            amz_date=amz_date,
        )

    async def _request(
        self,
        method: str,
        key: str | None,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        payload_hash: str | None = None,
        stream: bool = False,
    ) -> httpx.Response:
        path = self._path(key)
        hdrs = dict(headers or {})
        if payload_hash is None:
            payload_hash = _sha256(content).hexdigest() if content else EMPTY_SHA256
        hdrs.update(await self._auth_headers(method, path, query, hdrs, payload_hash))
        url = f"{self._scheme}://{self._host}{path}"
        if query:
            url += "?" + "&".join(
                f"{_uri_encode(k, '')}={_uri_encode(v, '')}" for k, v in sorted(query.items())
            )
        # httpx.URL keeps an already-encoded path as given; the signature was
        # computed over exactly this string.
        req = self._client.build_request(method, url, headers=hdrs, content=content)
        resp = await self._client.send(req, stream=stream)
        if resp.status_code == 401:
            if stream:
                await resp.aclose()
            raise TierAuthError(f"{method} {key or ''} returned 401: the credential is dead")
        return resp

    # -- object operations -------------------------------------------------

    async def get(self, key: str) -> httpx.Response:
        """Streamed GET. The caller MUST aclose() the response."""
        return await self._request("GET", key, stream=True)

    async def get_bytes(self, key: str) -> httpx.Response:
        return await self._request("GET", key)

    async def head(self, key: str) -> httpx.Response:
        return await self._request("HEAD", key)

    async def put(
        self,
        key: str,
        body: bytes,
        *,
        sha256_hex: str | None = None,
        checksum_header: bool = False,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> httpx.Response:
        """PUT from memory.

        `sha256_hex` is the body's hash when the caller already knows it -- for
        content it is the object's NAME, so no second hash pass happens here.
        SigV4 sends it as x-amz-content-sha256, and a server that checks it
        refuses a body that does not match. `checksum_header` additionally sends
        x-amz-checksum-sha256 (base64 of the same digest).
        """
        headers: dict[str, str] = {}
        if content_type:
            headers["content-type"] = content_type
        for k, v in (metadata or {}).items():
            headers[self.meta_prefix + k] = v
        digest_hex = sha256_hex or _sha256(body).hexdigest()
        if checksum_header:
            headers["x-amz-checksum-sha256"] = base64.b64encode(bytes.fromhex(digest_hex)).decode()
        return await self._request("PUT", key, headers=headers, content=body,
                                   payload_hash=digest_hex)

    async def list_prefix(self, prefix: str, page_size: int | None = None
                          ) -> AsyncIterator[ListedObject]:
        """ListObjectsV2 under a prefix, following continuation tokens."""
        token: str | None = None
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if page_size:
                q["max-keys"] = str(page_size)
            if token:
                q["continuation-token"] = token
            r = await self._request("GET", None, query=q)
            if r.status_code != 200:
                raise TierHTTPError(r.status_code, "ListObjectsV2", r.text)
            root = ET.fromstring(r.content)
            for c in _findall(root, "Contents"):
                k = _find(c, "Key")
                s = _find(c, "Size")
                if k is not None and k.text:
                    yield ListedObject(k.text, int(s.text) if s is not None and s.text else 0)
            trunc = _find(root, "IsTruncated")
            nxt = _find(root, "NextContinuationToken")
            if trunc is not None and trunc.text == "true" and nxt is not None and nxt.text:
                token = nxt.text
                continue
            return

    # -- multipart ---------------------------------------------------------

    async def create_multipart(self, key: str, content_type: str | None = None) -> str:
        headers = {"content-type": content_type} if content_type else {}
        r = await self._request("POST", key, query={"uploads": ""}, headers=headers)
        if r.status_code != 200:
            raise TierHTTPError(r.status_code, "CreateMultipartUpload", r.text)
        el = _find(ET.fromstring(r.content), "UploadId")
        if el is None or not el.text:
            raise TierHTTPError(r.status_code, "CreateMultipartUpload", "no UploadId")
        return el.text

    async def upload_part(self, key: str, upload_id: str, number: int, body: bytes) -> str:
        # UNSIGNED-PAYLOAD: signing the part would mean hashing it a second
        # time. The whole-object hash is checked before CompleteMultipartUpload.
        r = await self._request(
            "PUT", key, query={"partNumber": str(number), "uploadId": upload_id},
            content=body, payload_hash=UNSIGNED_PAYLOAD,
        )
        if r.status_code != 200:
            raise TierHTTPError(r.status_code, "UploadPart", r.text)
        etag = r.headers.get("etag")
        if not etag:
            raise TierHTTPError(r.status_code, "UploadPart", "no ETag on the part")
        return etag

    async def complete_multipart(self, key: str, upload_id: str, parts: list[str]) -> None:
        body = "<CompleteMultipartUpload>" + "".join(
            f"<Part><PartNumber>{i}</PartNumber><ETag>{etag}</ETag></Part>"
            for i, etag in enumerate(parts, start=1)
        ) + "</CompleteMultipartUpload>"
        r = await self._request("POST", key, query={"uploadId": upload_id},
                                content=body.encode(), headers={"content-type": "application/xml"})
        # S3 can answer 200 with an <Error> body on Complete.
        if r.status_code != 200 or b"<Error>" in r.content:
            raise TierHTTPError(r.status_code, "CompleteMultipartUpload", r.text)

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        r = await self._request("DELETE", key, query={"uploadId": upload_id})
        if r.status_code not in (200, 204, 404):
            raise TierHTTPError(r.status_code, "AbortMultipartUpload", r.text)


def describe(resp: httpx.Response) -> str:
    """One line for a log: status and the store's error code, if any."""
    code = ""
    try:
        el = _find(ET.fromstring(resp.content), "Code") if resp.content else None
        code = el.text if el is not None and el.text else ""
    except (ET.ParseError, httpx.ResponseNotRead):
        pass
    return f"{resp.status_code} {code}".strip()


def to_json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
