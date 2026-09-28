from __future__ import annotations

from typing import Any

from oikb.connectors.gdrive import GDriveConnector


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class _FilesResource:
    def __init__(self, listings: dict[str, list[dict[str, str]]]) -> None:
        self.listings = listings
        self.queried_folders: list[str] = []

    def list(self, **kwargs: Any) -> _Request:
        parent_id = kwargs["q"].split("'", 2)[1]
        self.queried_folders.append(parent_id)
        return _Request({"files": self.listings[parent_id]})


class _DriveService:
    def __init__(self, listings: dict[str, list[dict[str, str]]]) -> None:
        self.files_resource = _FilesResource(listings)

    def files(self) -> _FilesResource:
        return self.files_resource


def test_build_manifest_recurses_into_subfolders() -> None:
    service = _DriveService(
        {
            "root-folder": [
                {
                    "id": "root-file",
                    "name": "root.txt",
                    "mimeType": "text/plain",
                    "md5Checksum": "root-checksum",
                    "size": "4",
                },
                {
                    "id": "nested-folder",
                    "name": "nested",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            ],
            "nested-folder": [
                {
                    "id": "nested-file",
                    "name": "child.txt",
                    "mimeType": "text/plain",
                    "md5Checksum": "child-checksum",
                    "size": "5",
                }
            ],
        }
    )
    connector = GDriveConnector.__new__(GDriveConnector)
    connector.folder_id = "root-folder"
    connector._service = service

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == [
        "nested/child.txt",
        "root.txt",
    ]
    assert service.files_resource.queried_folders == [
        "root-folder",
        "nested-folder",
    ]


def _connector(service):
    connector = GDriveConnector.__new__(GDriveConnector)
    connector.folder_id = "root-folder"
    connector._service = service
    connector._file_ids = None
    return connector


def test_nested_special_names_read_by_enumerated_id():
    from unittest.mock import Mock

    for folder_name in ["a/b", "owner's files", "a\\b"]:
        for mime, filename in [("text/plain", "notes.txt"),
                               ("application/vnd.google-apps.document", "notes")]:
            service = _DriveService({
                "root-folder": [{"id": "nested", "name": folder_name,
                                 "mimeType": "application/vnd.google-apps.folder"}],
                "nested": [{"id": "exact-file", "name": filename, "mimeType": mime}],
            })
            resource = service.files_resource
            resource.get = Mock(return_value=_Request({"mimeType": mime}))
            resource.get_media = Mock(return_value=_Request(b"native"))
            resource.export = Mock(return_value=_Request(b"exported"))
            connector = _connector(service)
            entry, = connector.build_manifest()
            assert entry.path == folder_name
            expected = b"native" if mime == "text/plain" else b"exported"
            assert connector.read_file(entry.path, entry.filename) == expected
            resource.get.assert_called_once_with(fileId="exact-file", fields="mimeType", supportsAllDrives=True)
            if mime == "text/plain":
                resource.get_media.assert_called_once_with(fileId="exact-file", supportsAllDrives=True)
            else:
                resource.export.assert_called_once_with(fileId="exact-file", mimeType="text/plain")
            assert resource.queried_folders == ["root-folder", "nested"]


def test_file_cache_refresh_and_cold_lookup():
    service = _DriveService({"root-folder": [
        {"id": "first", "name": "notes.txt", "mimeType": "text/plain"},
    ]})
    connector = _connector(service)
    assert connector._find_file("", "notes.txt") == "first"
    service.files_resource.listings["root-folder"] = [
        {"id": "second", "name": "other.txt", "mimeType": "text/plain"},
    ]
    connector.build_manifest()
    assert connector._find_file("", "notes.txt") is None
    assert connector._find_file("", "other.txt") == "second"


def test_ambiguous_manifest_path_fails_closed():
    import pytest

    service = _DriveService({"root-folder": [
        {"id": "first", "name": "notes.txt", "mimeType": "text/plain"},
        {"id": "second", "name": "notes", "mimeType": "application/vnd.google-apps.document"},
    ]})
    connector = _connector(service)
    with pytest.raises(ValueError, match="Ambiguous Drive file path"):
        connector.build_manifest()
    assert connector._file_ids is None
