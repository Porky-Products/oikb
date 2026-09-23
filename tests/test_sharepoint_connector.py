from __future__ import annotations

import httpx
import pytest
import respx

from oikb.connectors.sharepoint import (
    SharePointConnector,
    _validate_sharepoint_download_url,
)

_SHAREPOINT_ENV_VARS = (
    "SHAREPOINT_TENANT_ID",
    "SHAREPOINT_CLIENT_ID",
    "SHAREPOINT_CLIENT_SECRET",
    "SHAREPOINT_CERTIFICATE_PATH",
    "SHAREPOINT_CERTIFICATE_PASSWORD",
    "SHAREPOINT_CLOUD",
)


@pytest.fixture(autouse=True)
def _clean_sharepoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep host SHAREPOINT_* env vars from leaking into constructor tests."""
    for var in _SHAREPOINT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _mock_sharepoint_backend(
    authority: str, graph_base: str, site_identifier: str, library: str
) -> None:
    """Mock the token, site, and drive lookups SharePointConnector.__init__ makes."""
    respx.post(f"{authority}/tenant/oauth2/v2.0/token").mock(
        return_value=httpx.Response(200, json={"access_token": "token"})
    )
    respx.get(f"{graph_base}/sites/{site_identifier}").mock(
        return_value=httpx.Response(200, json={"id": "site-id"})
    )
    respx.get(f"{graph_base}/sites/site-id/drives").mock(
        return_value=httpx.Response(
            200, json={"value": [{"name": library, "id": "drive-id"}]}
        )
    )


# ── Download-host allowlist (Finding 13) ─────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        # commercial
        "https://contoso.sharepoint.com/_layouts/15/download.aspx?doc=doc.pdf",
        "https://mytenant-mydomain.sharepoint.com/personal/user/file.docx",
        # gcc_high
        "https://contoso.sharepoint.us/_layouts/15/download.aspx?doc=doc.pdf",
        "https://mytenant-mydomain.sharepoint.us/sites/team/file.docx",
        # dod
        "https://contoso.sharepoint-mil.us/_layouts/15/download.aspx?doc=doc.pdf",
        "https://mytenant-mydomain.sharepoint-mil.us/sites/team/file.docx",
    ],
)
def test_download_validation_accepts_sharepoint_hosts_per_cloud(url: str) -> None:
    _validate_sharepoint_download_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://graph.microsoft.com/v1.0/drives/d/items/1/content",
        "https://graph.microsoft.us/v1.0/drives/d/items/1/content",
        "https://dod-graph.microsoft.us/v1.0/drives/d/items/1/content",
    ],
)
def test_download_validation_accepts_graph_hosts_per_cloud(url: str) -> None:
    _validate_sharepoint_download_url(url)


@pytest.mark.parametrize(
    "url",
    [
        # unrelated host
        "https://evil.example.com/file.docx",
        # lookalikes that only contain an allowed suffix after a dot-different separator
        "https://graph.microsoft.us.evil.example.com/drives/d/items/1/content",
        "https://dod-graph.microsoft.us.evil.example.com/drives/d/items/1/content",
        "https://sharepoint.us.evil.example.com/file.docx",
        "https://evil-sharepoint.us/file.docx",
        # allowed host but wrong scheme
        "http://contoso.sharepoint.com/file.docx",
    ],
)
def test_download_validation_rejects_non_allowlisted_hosts(url: str) -> None:
    with pytest.raises(
        ValueError, match="Refusing unexpected SharePoint download URL"
    ):
        _validate_sharepoint_download_url(url)


@respx.mock
def test_read_file_downloads_gcc_high_redirect_target() -> None:
    """A gcc_high tenant's download redirect must pass host validation.

    Regression test: before sharepoint.us was allowlisted, GCC High tenants
    could never download files because every redirect target was rejected.
    """
    _mock_sharepoint_backend(
        authority="https://login.microsoftonline.us",
        graph_base="https://graph.microsoft.us/v1.0",
        site_identifier="contoso.sharepoint.us",
        library="Documents",
    )
    respx.get(
        "https://graph.microsoft.us/v1.0/drives/drive-id/root:/docs/file.txt:/content"
    ).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://contoso.sharepoint.us/docs/file.txt"}
        )
    )
    respx.get("https://contoso.sharepoint.us/docs/file.txt").mock(
        return_value=httpx.Response(200, content=b"file-bytes")
    )

    with SharePointConnector(
        site="contoso.sharepoint.us",
        library="Documents",
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
        cloud="gcc_high",
    ) as connector:
        assert connector.read_file("docs", "file.txt") == b"file-bytes"


# ── __init__ positional compatibility (Finding 16) ───────────────


@respx.mock
def test_init_positional_args_keep_pre_site_path_binding() -> None:
    """Positional args must bind as (site, library, tenant_id, client_id, ...).

    Regression test: site_path was briefly inserted at position 2, which made
    SharePointConnector("site", "docs") bind "docs" to site_path.
    """
    _mock_sharepoint_backend(
        authority="https://login.microsoftonline.com",
        graph_base="https://graph.microsoft.com/v1.0",
        site_identifier="contoso.sharepoint.com",
        library="Docs",
    )

    with SharePointConnector(
        "contoso.sharepoint.com", "Docs", "tenant", "client", "secret"
    ) as connector:
        assert connector.site == "contoso.sharepoint.com"
        assert connector.library == "Docs"
        assert connector.site_path == ""


@respx.mock
def test_init_keyword_args_still_supported() -> None:
    """Keyword callers (e.g. cli.py) keep working after the parameter reorder."""
    site_route = respx.get(
        "https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/TeamSite"
    ).mock(return_value=httpx.Response(200, json={"id": "site-id"}))
    _mock_sharepoint_backend(
        authority="https://login.microsoftonline.com",
        graph_base="https://graph.microsoft.com/v1.0",
        site_identifier="contoso.sharepoint.com:/sites/TeamSite",
        library="Documents",
    )

    with SharePointConnector(
        site="contoso.sharepoint.com",
        site_path="sites/TeamSite",
        library="Documents",
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
    ) as connector:
        assert connector.site == "contoso.sharepoint.com"
        assert connector.site_path == "sites/TeamSite"
        assert connector.library == "Documents"
        assert site_route.called
