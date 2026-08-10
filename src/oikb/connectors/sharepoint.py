"""SharePoint connector — sync a document library to a Knowledge Base.

Uses Microsoft Graph API. Auth via SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID,
and one of:
  - SHAREPOINT_CLIENT_SECRET  (client secret auth)
  - SHAREPOINT_CERTIFICATE_PATH  (certificate auth — more secure, recommended
    for production).  Optionally set SHAREPOINT_CERTIFICATE_PASSWORD for
    encrypted PEM keys.

The two auth methods are mutually exclusive.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


def _encode_drive_path(path: str) -> str:
    """Encode a SharePoint path for Microsoft Graph's colon-path syntax."""
    return quote(path, safe="/")


_ALLOWED_SHAREPOINT_DOWNLOAD_HOSTS = (
    "graph.microsoft.com",
    "sharepoint.com",
    "sharepoint-df.com",
    "sharepointonline.com",
)
_SHAREPOINT_DOWNLOAD_REDIRECT_CODES = {301, 302, 303, 307, 308}
_SHAREPOINT_DOWNLOAD_MAX_REDIRECTS = 5


def _validate_sharepoint_download_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme != "https" or not host:
        raise ValueError(f"Refusing unexpected SharePoint download URL: {url}")
    normalized_host = host.lower()
    if not any(
        normalized_host == allowed
        or normalized_host.endswith(f".{allowed}")
        for allowed in _ALLOWED_SHAREPOINT_DOWNLOAD_HOSTS
    ):
        raise ValueError(f"Refusing unexpected SharePoint download URL: {url}")


class SharePointConnector(BaseConnector):
    """Sync files from a SharePoint document library."""

    def __init__(
        self,
        site: str,
        library: str = "Documents",
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        certificate_path: str | None = None,
        certificate_password: str | None = None,
    ):
        self.site = site
        self.library = library

        tid = tenant_id or os.environ.get("SHAREPOINT_TENANT_ID", "")
        cid = client_id or os.environ.get("SHAREPOINT_CLIENT_ID", "")
        secret = client_secret or os.environ.get("SHAREPOINT_CLIENT_SECRET", "")
        cert_path = certificate_path or os.environ.get("SHAREPOINT_CERTIFICATE_PATH", "")
        cert_password = certificate_password or os.environ.get("SHAREPOINT_CERTIFICATE_PASSWORD", "")

        if not tid or not cid:
            raise ValueError(
                "SharePoint credentials required. Set env vars:\n"
                "  SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID, and either\n"
                "  SHAREPOINT_CLIENT_SECRET or SHAREPOINT_CERTIFICATE_PATH"
            )

        if secret and cert_path:
            raise ValueError(
                "SHAREPOINT_CLIENT_SECRET and SHAREPOINT_CERTIFICATE_PATH are "
                "mutually exclusive. Set one or the other, not both."
            )

        if not secret and not cert_path:
            raise ValueError(
                "SharePoint auth method required. Set one of:\n"
                "  SHAREPOINT_CLIENT_SECRET  (client secret)\n"
                "  SHAREPOINT_CERTIFICATE_PATH  (certificate)"
            )

        token_url = f"https://login.microsoftonline.com/{tid}/oauth2/v2.0/token"

        if cert_path:
            access_token = _get_token_via_certificate(
                token_url=token_url,
                client_id=cid,
                certificate_path=cert_path,
                certificate_password=cert_password or None,
            )
        else:
            access_token = _get_token_via_secret(
                token_url=token_url,
                client_id=cid,
                client_secret=secret,
            )

        self._http = httpx.Client(
            base_url="https://graph.microsoft.com/v1.0",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=60.0,
        )

        # Resolve site ID.
        site_resp = self._http.get(f"/sites/{self.site}")
        site_resp.raise_for_status()
        self._site_id = site_resp.json()["id"]

        # Resolve drive ID.
        self._drive_id = None
        available_drives: list[str] = []
        for drive in self._iter_collection(f"/sites/{self._site_id}/drives"):
            name = drive.get("name")
            if isinstance(name, str):
                available_drives.append(name)
            if drive.get("name") == self.library:
                drive_id = drive.get("id")
                if not isinstance(drive_id, str) or not drive_id:
                    raise ValueError(
                        "Microsoft Graph drive item contained an invalid id for "
                        f"library '{self.library}'"
                    )
                self._drive_id = drive_id
                break
        if not self._drive_id:
            raise ValueError(
                f"Library '{self.library}' not found. Available: {available_drives}"
            )

    def _iter_collection(self, url: str) -> Iterator[dict[str, Any]]:
        """Yield every object from a paginated Microsoft Graph collection."""
        next_url: str | None = url
        requested_urls: set[str] = set()

        while next_url:
            if next_url in requested_urls:
                raise ValueError(
                    "Microsoft Graph pagination repeated @odata.nextLink for "
                    f"{next_url}"
                )
            requested_urls.add(next_url)

            response = self._http.get(next_url)
            response.raise_for_status()
            payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError(
                    "Microsoft Graph collection response was not an object for "
                    f"{next_url}"
                )

            values = payload.get("value")
            if not isinstance(values, list):
                raise ValueError(
                    "Microsoft Graph collection response contained a non-list "
                    f"value for {next_url}"
                )

            for item in values:
                if not isinstance(item, dict):
                    raise ValueError(
                        "Microsoft Graph collection response contained a "
                        f"non-object item for {next_url}"
                    )
                yield item

            next_link = payload.get("@odata.nextLink")
            if next_link is None or next_link == "":
                next_url = None
            elif isinstance(next_link, str):
                next_url = next_link
            else:
                raise ValueError(
                    "Microsoft Graph collection response contained an invalid "
                    f"@odata.nextLink for {next_url}"
                )

    def build_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        self._walk_folder("/", "", entries, set(), set())
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _walk_folder(
        self,
        folder_path: str,
        prefix: str,
        entries: list[ManifestEntry],
        seen_folder_ids: set[str],
        seen_file_paths: set[str],
    ) -> None:
        stack: list[tuple[str, str]] = [(folder_path, prefix)]
        while stack:
            current_folder_path, current_prefix = stack.pop()
            url = (
                f"/drives/{self._drive_id}/root/children"
                if current_folder_path == "/"
                else (
                    f"/drives/{self._drive_id}/root:/"
                    f"{_encode_drive_path(current_folder_path)}:/children"
                )
            )
            for item in self._iter_collection(url):
                if "folder" in item:
                    name = item.get("name")
                    item_id = item.get("id")
                    if not isinstance(name, str) or not name:
                        raise ValueError(
                            "Microsoft Graph folder item contained an invalid name"
                        )
                    if not isinstance(item_id, str) or not item_id:
                        raise ValueError(
                            "Microsoft Graph folder item contained an invalid id"
                        )
                    if item_id in seen_folder_ids:
                        raise ValueError(
                            f"SharePoint repeated folder item id: {item_id}"
                        )
                    seen_folder_ids.add(item_id)

                    sub = f"{current_prefix}/{name}" if current_prefix else name
                    child_path = (
                        f"{current_folder_path}/{name}"
                        if current_folder_path != "/"
                        else name
                    )
                    stack.append((child_path, sub))
                elif "file" in item:
                    name = item.get("name")
                    if not isinstance(name, str) or not name:
                        raise ValueError(
                            "Microsoft Graph file item contained an invalid name"
                        )
                    display_path = (
                        f"{current_prefix}/{name}" if current_prefix else name
                    )
                    if display_path in seen_file_paths:
                        raise ValueError(
                            f"SharePoint duplicate file path: {display_path}"
                        )
                    seen_file_paths.add(display_path)

                    etag = (item.get("eTag") or item.get("cTag", "")).strip('"')
                    entries.append(
                        ManifestEntry(
                            filename=name,
                            path=current_prefix,
                            checksum=etag[:16] if etag else "",
                            size=item.get("size", 0),
                        )
                    )

    def read_file(self, path: str, filename: str) -> bytes:
        file_path = f"{path}/{filename}" if path else filename
        resp = self._http.get(
            f"/drives/{self._drive_id}/root:/{_encode_drive_path(file_path)}:/content",
            follow_redirects=False,
        )
        if resp.status_code in _SHAREPOINT_DOWNLOAD_REDIRECT_CODES:
            location = resp.headers.get("location")
            if not location:
                raise ValueError(
                    "SharePoint download redirect response did not include a location"
                )
            return self._read_sharepoint_download_redirect(
                urljoin(str(resp.request.url), location)
            )
        resp.raise_for_status()
        return resp.content

    def _read_sharepoint_download_redirect(self, url: str) -> bytes:
        _validate_sharepoint_download_url(url)
        with httpx.Client(
            timeout=self._http.timeout,
            follow_redirects=False,
        ) as client:
            next_url = url
            for _ in range(_SHAREPOINT_DOWNLOAD_MAX_REDIRECTS + 1):
                resp = client.get(next_url)
                if resp.status_code not in _SHAREPOINT_DOWNLOAD_REDIRECT_CODES:
                    resp.raise_for_status()
                    return resp.content

                location = resp.headers.get("location")
                if not location:
                    raise ValueError(
                        "SharePoint download redirect response did not include a "
                        "location"
                    )
                next_url = urljoin(str(resp.request.url), location)
                _validate_sharepoint_download_url(next_url)

        raise ValueError("SharePoint download exceeded redirect limit")

    def close(self) -> None:
        self._http.close()


# ── Auth helpers ────────────────────────────────────────────────


def _get_token_via_secret(token_url: str, client_id: str, client_secret: str) -> str:
    """Obtain an access token using client ID + client secret."""
    token_resp = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    token_resp.raise_for_status()
    return token_resp.json()["access_token"]


def _get_token_via_certificate(
    token_url: str,
    client_id: str,
    certificate_path: str,
    certificate_password: str | None = None,
) -> str:
    """Obtain an access token using client ID + certificate (JWT assertion).

    Reads a PEM file that contains both the private key and the certificate.
    Builds a signed JWT assertion per the Microsoft identity platform spec:
    https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        raise ImportError(
            "Certificate auth requires the 'cryptography' package.\n"
            "Install it with:  pip install oikb[sharepoint-cert]"
        )

    try:
        import jwt
    except ImportError:
        raise ImportError(
            "Certificate auth requires the 'PyJWT' package.\n"
            "Install it with:  pip install oikb[sharepoint-cert]"
        )

    # Load PEM file.
    pem_path = os.path.expanduser(certificate_path)
    if not os.path.isfile(pem_path):
        raise FileNotFoundError(f"Certificate file not found: {pem_path}")

    with open(pem_path, "rb") as f:
        pem_data = f.read()

    password_bytes = certificate_password.encode() if certificate_password else None

    # Load private key.
    private_key = serialization.load_pem_private_key(pem_data, password=password_bytes)

    # Load certificate to extract thumbprint.
    cert = x509.load_pem_x509_certificate(pem_data)
    thumbprint = cert.fingerprint(cert.signature_hash_algorithm or x509.hashes.SHA256())
    x5t = base64.urlsafe_b64encode(thumbprint).rstrip(b"=").decode("ascii")

    # Build JWT assertion.
    now = int(time.time())
    claims = {
        "aud": token_url,
        "iss": client_id,
        "sub": client_id,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 600,  # 10 minute validity
    }
    headers = {
        "x5t": x5t,
    }

    assertion = jwt.encode(claims, private_key, algorithm="RS256", headers=headers)

    # Exchange assertion for access token.
    token_resp = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    token_resp.raise_for_status()
    return token_resp.json()["access_token"]


# ── Source parser ───────────────────────────────────────────────


def parse_sharepoint_source(source: str) -> dict[str, str | None]:
    """Parse sharepoint:site/library or sharepoint:site."""
    source = source.removeprefix("sharepoint:")
    parts = source.split("/", 1)
    site = parts[0]
    library = parts[1] if len(parts) > 1 else "Documents"
    if not site:
        raise ValueError("Invalid SharePoint source. Expected: sharepoint:<site>[/library]")
    return {"site": site, "library": library}
