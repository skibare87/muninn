"""ListObjectsV2 parsing against the response shapes real services send.

MinIO and AWS use one XML namespace; GCS's XML API uses another for the same
document, plus single-quoted declarations and extra elements. The fake store
and MinIO both speak the S3 shape, so neither could reveal that a GCS listing
parsed as EMPTY. These fixtures are the services' own shapes, not the fake's.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import s3client

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"
GCS_NS = "http://doc.s3.amazonaws.com/2006-03-01"


def _page(ns: str, keys: list[tuple[str, int]], token: str | None, quote: str = "'") -> bytes:
    q = quote
    items = "".join(
        f"<Contents><Key>{k}</Key><Generation>1700000000000000</Generation>"
        f"<MetaGeneration>1</MetaGeneration><LastModified>2026-09-25T18:27:17.982Z</LastModified>"
        f'<ETag>"abc-1"</ETag><Size>{n}</Size></Contents>'
        for k, n in keys
    )
    more = (f"<NextContinuationToken>{token}</NextContinuationToken><IsTruncated>true</IsTruncated>"
            if token else "<IsTruncated>false</IsTruncated>")
    return (
        f"<?xml version={q}1.0{q} encoding={q}UTF-8{q}?>"
        f"<ListBucketResult xmlns={q}{ns}{q}><Name>b</Name><Prefix>p/</Prefix>"
        f"<KeyCount>{len(keys)}</KeyCount><MaxKeys>2</MaxKeys>{more}{items}</ListBucketResult>"
    ).encode()


def _client(pages: dict[str | None, bytes]) -> s3client.S3Client:
    def handler(request: httpx.Request) -> httpx.Response:
        tok = request.url.params.get("continuation-token")
        return httpx.Response(200, content=pages[tok])

    return s3client.S3Client(
        endpoint="https://storage.example", bucket="b", region="auto", path_style=True,
        creds=s3client.StaticKeys("AK", "SK"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _list(c: s3client.S3Client) -> list[tuple[str, int]]:
    return [(o.key, o.size) async for o in c.list_prefix("p/")]


@pytest.mark.parametrize("ns,quote", [(S3_NS, '"'), (GCS_NS, "'")], ids=["s3", "gcs"])
def test_both_namespaces_list_every_key_across_pages(ns, quote):
    pages = {
        None: _page(ns, [("p/a", 10), ("p/b", 20)], "T1", quote),
        "T1": _page(ns, [("p/c", 30)], None, quote),
    }
    assert asyncio.run(_list(_client(pages))) == [("p/a", 10), ("p/b", 20), ("p/c", 30)]


def test_the_exact_gcs_shape_reported_from_a_real_bucket():
    body = (
        b"<?xml version='1.0' encoding='UTF-8'?><ListBucketResult "
        b"xmlns='http://doc.s3.amazonaws.com/2006-03-01'><Name>bkt</Name>"
        b"<Prefix>muninn/v1/content/</Prefix><KeyCount>1</KeyCount><MaxKeys>2</MaxKeys>"
        b"<IsTruncated>false</IsTruncated><Contents><Key>muninn/v1/content/hf/x/sha256/60b6</Key>"
        b"<Generation>1</Generation><MetaGeneration>1</MetaGeneration>"
        b"<LastModified>2026-09-25T18:27:17.982Z</LastModified><ETag>\"a118-153\"</ETag>"
        b"<Size>10264229896</Size></Contents></ListBucketResult>"
    )
    assert asyncio.run(_list(_client({None: body}))) == [
        ("muninn/v1/content/hf/x/sha256/60b6", 10264229896)
    ]


def test_a_listing_that_declares_keys_but_yields_none_is_refused():
    body = (
        b"<?xml version='1.0'?><ListBucketResult xmlns='urn:unknown'><KeyCount>3</KeyCount>"
        b"<IsTruncated>false</IsTruncated><Entry><Key>k</Key></Entry></ListBucketResult>"
    )
    with pytest.raises(s3client.TierHTTPError, match="KeyCount=3"):
        asyncio.run(_list(_client({None: body})))


def test_a_genuinely_empty_listing_is_empty():
    assert asyncio.run(_list(_client({None: _page(GCS_NS, [], None)}))) == []
