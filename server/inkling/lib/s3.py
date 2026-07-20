"""Minimal S3 client (SigV4, path-style) for backups (§13.2/§13.3).

Deliberately tiny: PUT/GET/LIST/DELETE against any S3-compatible endpoint
(§19.3 leaves the vendor open), signed with AWS Signature v4, no SDK
dependency. Path-style URLs ({endpoint}/{bucket}/{key}) because every
S3-compatible supports them and virtual-host style needs DNS games.

StubS3 is the in-memory double the backup/restore tests run against; this
client itself is exercised for its signing math only (deterministic test
vectors), since real object storage is a pending human decision.
"""

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree

import httpx

log = logging.getLogger(__name__)


class S3Error(Exception):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_v4(
    *,
    method: str,
    url: str,
    region: str,
    access_key: str,
    secret_key: str,
    payload_hash: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """SigV4 headers for a request with no extra signed headers."""
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    parts = urlsplit(url)
    host = parts.netloc
    canonical_query = "&".join(sorted(parts.query.split("&"))) if parts.query else ""
    canonical_headers = (
        f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([
        method,
        quote(parts.path or "/", safe="/-_.~"),
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{datestamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        _sha256(canonical_request.encode()),
    ])
    signing_key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), datestamp), region), "s3"),
        "aws4_request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }


class S3Client:
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        timeout: float = 120.0,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    def _url(self, key: str = "", query: str = "") -> str:
        url = f"{self.endpoint}/{self.bucket}"
        if key:
            url += f"/{quote(key, safe='/-_.~')}"
        if query:
            url += f"?{query}"
        return url

    async def _request(
        self, method: str, url: str, *, body: bytes = b""
    ) -> httpx.Response:
        headers = sign_v4(
            method=method, url=url, region=self.region,
            access_key=self.access_key, secret_key=self.secret_key,
            payload_hash=_sha256(body),
        )
        response = await self._client.request(method, url, content=body, headers=headers)
        if response.status_code // 100 != 2:
            raise S3Error(f"{method} {url} -> {response.status_code}: {response.text[:300]}")
        return response

    async def put(self, key: str, data: bytes) -> None:
        await self._request("PUT", self._url(key), body=data)

    async def get(self, key: str) -> bytes:
        return (await self._request("GET", self._url(key))).content

    async def delete(self, key: str) -> None:
        await self._request("DELETE", self._url(key))

    async def list(self, prefix: str) -> list[str]:
        """Keys under prefix, ascending — datestamped names sort oldest-first."""
        response = await self._request(
            "GET", self._url(query=f"list-type=2&prefix={quote(prefix, safe='')}")
        )
        root = ElementTree.fromstring(response.text)
        ns = root.tag.partition("}")[0] + "}" if root.tag.startswith("{") else ""
        keys = [
            el.findtext(f"{ns}Key") or ""
            for el in root.iter(f"{ns}Contents")
        ]
        return sorted(k for k in keys if k)


class StubS3:
    """In-memory double with the same surface."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    async def put(self, key: str, data: bytes) -> None:
        self.calls.append(("put", key))
        self.store[key] = data

    async def get(self, key: str) -> bytes:
        self.calls.append(("get", key))
        if key not in self.store:
            raise S3Error(f"GET {key} -> 404")
        return self.store[key]

    async def delete(self, key: str) -> None:
        self.calls.append(("delete", key))
        self.store.pop(key, None)

    async def list(self, prefix: str) -> list[str]:
        self.calls.append(("list", prefix))
        return sorted(k for k in self.store if k.startswith(prefix))


def region_from_endpoint(endpoint: str) -> str:
    """SigV4 region from an `s3.<region>.<vendor>` endpoint host.

    B2 (and AWS itself) reject signatures scoped to the wrong region, so
    us-east-1 only works as a fallback for vendors that ignore it (R2, MinIO).
    """
    host = urlsplit(endpoint).netloc or endpoint
    parts = host.split(".")
    if len(parts) >= 3 and parts[0] == "s3":
        return parts[1]
    return "us-east-1"


def stub_or_real(config: Any) -> S3Client | None:
    """An S3Client when §19.3's bucket decision has been made, else None."""
    if not (config.backup_s3_endpoint and config.backup_s3_bucket):
        return None
    return S3Client(
        endpoint=config.backup_s3_endpoint,
        bucket=config.backup_s3_bucket,
        access_key=config.backup_s3_key,
        secret_key=config.backup_s3_secret,
        region=region_from_endpoint(config.backup_s3_endpoint),
    )
