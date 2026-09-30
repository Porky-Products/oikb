"""SharePoint connector — sync a document library to a Knowledge Base.

Uses Microsoft Graph API. Auth via SHAREPOINT_TENANT_ID, SHAREPOINT_CLIENT_ID,
and one of:
  - SHAREPOINT_CLIENT_SECRET  (client secret auth)
  - SHAREPOINT_CERTIFICATE_PATH  (certificate auth — more secure, recommended
    for production).  Optionally set SHAREPOINT_CERTIFICATE_PASSWORD for
    encrypted PEM keys.

The two auth methods are mutually exclusive.

Select Microsoft Endpoints. Use SHAREPOINT_CLOUD set to
one of:
  - SHAREPOINT_CLOUD=commercial // default
  - SHAREPOINT_CLOUD=gcc_high
  - SHAREPOINT_CLOUD=dod
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from oikb.connectors import BaseConnector, ManifestEntry

_CLOUD_ENDPOINTS: dict[str, dict[str, str]] = {
    "commercial": {
        "authority": "https://login.microsoftonline.com",
        "graph": "https://graph.microsoft.com/v1.0",
    },
    "gcc_high": {
        "authority": "https://login.microsoftonline.us",
        "graph": "https://graph.microsoft.us/v1.0",
    },
    "dod": {
        "authority": "https://login.microsoftonline.us",
        "graph": "https://dod-graph.microsoft.us/v1.0",
    },
}


def _encode_drive_path(path: str) -> str:
    """Encode a SharePoint path for Microsoft Graph's colon-path syntax."""
    return quote(path, safe="/")


# Download hosts for every supported cloud (see _CLOUD_ENDPOINTS):
#   commercial — *.sharepoint.com / graph.microsoft.com
#   gcc_high    — *.sharepoint.us / graph.microsoft.us
#   dod         — *.sharepoint-mil.us / dod-graph.microsoft.us
_ALLOWED_SHAREPOINT_DOWNLOAD_HOSTS = (
    "graph.microsoft.com",
    "graph.microsoft.us",
    "dod-graph.microsoft.us",
    "sharepoint.com",
    "sharepoint.us",
    "sharepoint-mil.us",
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
        site_path: str = "",
        cloud: str | None = None,
    ):
        self.site = site
        self.site_path = site_path.strip("/")
        self.library = library

        tid = tenant_id or os.environ.get("SHAREPOINT_TENANT_ID", "")
        cid = client_id or os.environ.get("SHAREPOINT_CLIENT_ID", "")
        secret = client_secret or os.environ.get("SHAREPOINT_CLIENT_SECRET", "")
        cert_path = certificate_path or os.environ.get("SHAREPOINT_CERTIFICATE_PATH", "")
        cert_password = certificate_password or os.environ.get("SHAREPOINT_CERTIFICATE_PASSWORD", "")

        sharepoint_cloud = cloud or os.environ.get("SHAREPOINT_CLOUD", "commercial")
        if sharepoint_cloud not in _CLOUD_ENDPOINTS:
            raise ValueError(f"SHAREPOINT_CLOUD must be one of {list(_CLOUD_ENDPOINTS)}, got '{sharepoint_cloud}'")
        endpoints = _CLOUD_ENDPOINTS[sharepoint_cloud]

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

        token_url = f"{endpoints['authority']}/{tid}/oauth2/v2.0/token"

        if cert_path:
            access_token = _get_token_via_certificate(
                token_url=token_url,
                client_id=cid,
                certificate_path=cert_path,
                certificate_password=cert_password or None,
                graph_base=endpoints["graph"]
            )
        else:
            access_token = _get_token_via_secret(
                token_url=token_url,
                client_id=cid,
                client_secret=secret,
                graph_base=endpoints["graph"]
            )

        self._http = httpx.Client(
            base_url=endpoints["graph"],
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=60.0,
        )

        # Resolve site ID.
        site_identifier = f"{self.site}:/{self.site_path}" if self.site_path else self.site
        site_resp = self._http.get(f"/sites/{site_identifier}")
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
                # ValueError (not TypeError): cli.py catches ValueError around
                # connector construction to render a clean error message.
                raise ValueError(  # noqa: TRY004
                    "Microsoft Graph collection response was not an object for "
                    f"{next_url}"
                )

            values = payload.get("value")
            if not isinstance(values, list):
                raise ValueError(  # noqa: TRY004
                    "Microsoft Graph collection response contained a non-list "
                    f"value for {next_url}"
                )

            for item in values:
                if not isinstance(item, dict):
                    raise ValueError(  # noqa: TRY004
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


def _get_token_via_secret(token_url: str, client_id: str, client_secret: str, graph_base: str) -> str:
    """Obtain an access token using client ID + client secret."""
    token_resp = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{graph_base.rsplit('/v1.0', 1)[0]}/.default",
        },
    )
    token_resp.raise_for_status()
    return token_resp.json()["access_token"]


def _get_token_via_certificate(
    token_url: str,
    client_id: str,
    certificate_path: str,
    graph_base: str,
    certificate_password: str | None = None,
) -> str:
    """Obtain an access token using client ID + certificate (JWT assertion).

    Reads a PEM file that contains both the private key and the certificate.
    Builds a signed JWT assertion per the Microsoft identity platform spec:
    https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
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

    # Load certificate to extract thumbprint. The x5t header identifies the registered
    # key by its SHA-1 thumbprint, whatever algorithm the certificate itself is signed
    # with — a SHA-256 digest here is rejected with AADSTS700027 ("key was not found").
    cert = x509.load_pem_x509_certificate(pem_data)
    thumbprint = cert.fingerprint(hashes.SHA1())
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
            "scope": f"{graph_base.rsplit('/v1.0', 1)[0]}/.default",
        },
    )
    token_resp.raise_for_status()
    return token_resp.json()["access_token"]


# ── Source parser ───────────────────────────────────────────────

_SITE_PATH_PREFIXES = ("sites", "teams")
_SEPARATOR = "::"

def parse_sharepoint_source(source: str) -> dict[str, str]:
    """Parse a SharePoint source string. Supports:
      sharepoint:<hostname>/<library>
      sharepoint:<hostname>/sites/<site_name>/<library>
      sharepoint:<hostname>/sites/<site_name>/<subsite>::<library>
    """
    source = source.removeprefix("sharepoint:")
    host, _, rest = source.partition("/")
    if not host:
        raise ValueError(
            "Invalid SharePoint source. Expected one of:\n"
            "  sharepoint:<hostname>/<library>\n"
            "  sharepoint:<hostname>/sites/<site_name>/<library>\n"
            "  sharepoint:<hostname>/sites/<site_name>/<subsite>::<library>"
        )

    if _SEPARATOR in rest:
        site_path_str, _, library = rest.partition(_SEPARATOR)
        site_path = site_path_str.strip("/")
        if not library:
            raise ValueError(f"Invalid SharePoint source: '{_SEPARATOR}' must be followed by a library name.")
        return {"site": host, "site_path": site_path, "library": library}

    segments = [s for s in rest.split("/") if s]

    if segments and segments[0] in _SITE_PATH_PREFIXES:
        if len(segments) < 3:
            raise ValueError(
                f"Invalid SharePoint source. '{segments[0]}/...' requires a site name and "
                f"library, e.g. sharepoint:{host}/{segments[0]}/TeamSite/Documents"
            )
        if len(segments) > 3:
            raise ValueError(
                "Ambiguous SharePoint source with a subsite path — separate the site path "
                f"from the library explicitly with '{_SEPARATOR}', e.g.\n"
                f"  sharepoint:{host}/{'/'.join(segments[:-1])}{_SEPARATOR}{segments[-1]}"
            )
        site_path = "/".join(segments[:2])   # e.g. "sites/TeamSite"
        library = segments[2]
    else:
        site_path = ""
        library = "/".join(segments) if segments else "Documents"

    return {"site": host, "site_path": site_path, "library": library}
