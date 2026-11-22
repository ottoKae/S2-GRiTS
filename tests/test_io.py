from pathlib import Path

import pytest

from s2grits import io


class FakeResponse:
    def __init__(self, chunks, headers=None):
        self.chunks = chunks
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        yield from self.chunks


def fake_download(monkeypatch, chunks, headers=None):
    def get(url, *, stream, timeout):
        assert url == "https://example.test/asset.tif"
        assert stream is True
        assert timeout == 90
        return FakeResponse(chunks, headers)

    monkeypatch.setattr(io.requests, "get", get)


def test_bounded_download_publishes_complete_file_atomically(tmp_path, monkeypatch):
    target = tmp_path / "asset.tif"
    target.write_bytes(b"existing")

    def chunks():
        yield b"ab"
        assert target.read_bytes() == b"existing"
        yield b""
        yield b"cd"

    fake_download(monkeypatch, chunks(), {"Content-Length": "4"})
    assert io.download_file("https://example.test/asset.tif", target, max_bytes=4) == target
    assert target.read_bytes() == b"abcd"
    assert not target.with_suffix(".tif.part").exists()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "4"])
def test_invalid_limit_rejected_before_request(tmp_path, monkeypatch, limit):
    def unexpected_request(*args, **kwargs):
        pytest.fail("Invalid budget must be rejected before HTTP.")

    monkeypatch.setattr(io.requests, "get", unexpected_request)
    with pytest.raises(ValueError, match="positive integer"):
        io.download_file("https://example.test/asset.tif", tmp_path / "asset.tif", max_bytes=limit)


def test_declared_limit_rejected_without_creating_partial(tmp_path, monkeypatch):
    target = tmp_path / "asset.tif"
    target.write_bytes(b"existing")
    fake_download(monkeypatch, [b"abcde"], {"Content-Length": "5"})
    with pytest.raises(ValueError, match="Content-Length"):
        io.download_file("https://example.test/asset.tif", target, max_bytes=4)
    assert target.read_bytes() == b"existing"
    assert not target.with_suffix(".tif.part").exists()


def test_stream_limit_without_header_preserves_existing_file(tmp_path, monkeypatch):
    target = tmp_path / "asset.tif"
    target.write_bytes(b"existing")
    fake_download(monkeypatch, [b"ab", b"cde"])
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        io.download_file("https://example.test/asset.tif", target, max_bytes=4)
    assert target.read_bytes() == b"existing"
    assert target.with_suffix(".tif.part").read_bytes() == b"ab"


@pytest.mark.parametrize("declared", ["2", "5"])
def test_mismatched_content_length_never_publishes(tmp_path, monkeypatch, declared):
    target = tmp_path / "asset.tif"
    target.write_bytes(b"existing")
    fake_download(monkeypatch, [b"abc"], {"Content-Length": declared})
    with pytest.raises(ValueError, match="differs from Content-Length"):
        io.download_file("https://example.test/asset.tif", target, max_bytes=8)
    assert target.read_bytes() == b"existing"
    assert target.with_suffix(".tif.part").read_bytes() == b"abc"


def test_without_limit_preserves_default_download_behavior(tmp_path, monkeypatch):
    target = tmp_path / "asset.tif"
    fake_download(monkeypatch, [b"abc"], {"Content-Length": "5"})
    assert io.download_file("https://example.test/asset.tif", target) == target
    assert target.read_bytes() == b"abc"
