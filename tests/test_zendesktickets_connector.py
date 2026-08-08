import pytest

from oikb.cli import _resolve_connector
from oikb.connectors.zendesktickets import ZendeskTicketsConnector, parse_zendesktickets_source


def test_parse_zendesktickets_source_requires_subdomain():
    assert parse_zendesktickets_source("zendesktickets:acme") == {"subdomain": "acme"}


def test_resolve_connector_returns_zendesktickets_connector(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = _resolve_connector("zendesktickets:acme")

    assert isinstance(connector, ZendeskTicketsConnector)
    connector.close()


def test_constructor_parses_zendeskticket_env(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")
    monkeypatch.setenv("ZENDESKTICKET_PAGE_SIZE", "25")
    monkeypatch.setenv("ZENDESKTICKET_DOWNLOAD_ATTACHMENTS", "true")
    monkeypatch.setenv("ZENDESKTICKET_INCLUDETAGS", "one,two")
    monkeypatch.setenv("ZENDESKTICKET_EXCLUDETAGS", "three")

    connector = ZendeskTicketsConnector()

    assert connector._subdomain == "acme"
    assert connector._user == "agent@example.com"
    assert connector._token == "secret"
    assert connector._page_size == 25
    assert connector._download_attachments is True
    assert connector._include_tags == ["one", "two"]
    assert connector._exclude_tags == ["three"]
    connector.close()


def test_build_manifest_is_empty(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = ZendeskTicketsConnector()

    assert connector.build_manifest() == []
    connector.close()


def test_read_file_raises_file_not_found(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = ZendeskTicketsConnector()

    with pytest.raises(FileNotFoundError):
        connector.read_file("", "missing.txt")
    connector.close()


def test_close_closes_http_client(monkeypatch):
    monkeypatch.setenv("ZENDESKTICKET_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESKTICKET_USER", "agent@example.com")
    monkeypatch.setenv("ZENDESKTICKET_TOKEN", "secret")

    connector = ZendeskTicketsConnector()
    assert connector._http.is_closed is False

    connector.close()

    assert connector._http.is_closed is True
