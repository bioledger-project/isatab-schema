"""Tests for the download engine.

All network access is replaced by a fake async client that serves canned
bodies and honours Range headers; subprocess backends (aria2c, SRA-Toolkit)
are replaced by fake processes.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import bioledger_isatab_schema.download as dl
from bioledger_isatab_schema.dataset import DataFile, DataSet
from bioledger_isatab_schema.download import (
    _download_one,
    _download_segment,
    _download_segmented,
    _download_single_stream,
    _download_via_aria2c,
    _download_via_sra,
    _extract_sra_accession,
    _hash_file,
    _ProgressCtx,
    _seed_hashers_from_file,
    _verify_and_record,
    download_manifest,
    download_remote_files,
    manifest_to_datafiles,
)
from bioledger_isatab_schema.manifest import Manifest, ManifestFile

BODY = b"ACGT" * 16384 + b"tail"  # 64KiB+ deterministic payload


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class FakeStreamResponse:
    """Mimics the object returned by ``httpx.AsyncClient.stream(...)``."""

    def __init__(self, status: int, body: bytes = b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=None
            )


class FakeClient:
    """Drop-in for httpx.AsyncClient; *handler(url, headers) -> FakeStreamResponse*."""

    def __init__(self, handler):
        self._handler = handler
        self.requests: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        headers = dict(headers or {})
        self.requests.append((url, headers))
        return self._handler(url, headers)


def simple_handler(body: bytes = BODY, status: int = 200):
    """Handler serving *body*; honours ``Range: bytes=start-end`` with 206."""

    def handler(url, headers):
        rng = (headers or {}).get("Range")
        if rng and status == 200:
            start_s, end_s = rng.removeprefix("bytes=").split("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(body) - 1
            if start >= len(body):
                return FakeStreamResponse(416, b"")
            chunk = body[start : end + 1]
            return FakeStreamResponse(
                206,
                chunk,
                headers={"content-range": f"bytes {start}-{end}/{len(body)}"},
            )
        return FakeStreamResponse(
            status, body, headers={"content-length": str(len(body))}
        )

    return handler


def _progress() -> _ProgressCtx:
    return _ProgressCtx()


def _remote_file(url="https://x/f.fastq.gz", **kw) -> DataFile:
    kw.setdefault("location", url)
    kw.setdefault("is_remote", True)
    return DataFile(**kw)


# ---------------------------------------------------------------------------
# Hashing / verification
# ---------------------------------------------------------------------------


class TestHashing:
    def test_hash_file(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(BODY)
        assert _hash_file(p) == (_sha(BODY), _md5(BODY), len(BODY))

    def test_seed_hashers(self, tmp_path):
        p = tmp_path / "f.bin"
        p.write_bytes(BODY)
        sha, md5, size = _seed_hashers_from_file(p)
        sha.update(b"+more")
        md5.update(b"+more")
        assert size == len(BODY)
        assert sha.hexdigest() == _sha(BODY + b"+more")
        assert md5.hexdigest() == _md5(BODY + b"+more")


class TestVerifyAndRecord:
    def _file_on_disk(self, tmp_path, body=BODY, **file_kw):
        p = tmp_path / "f.bin"
        p.write_bytes(body)
        return p, DataFile(location="u", **file_kw)

    def test_matching_sha256_records(self, tmp_path):
        p, f = self._file_on_disk(tmp_path, sha256=_sha(BODY))
        _verify_and_record(f, p, _sha(BODY), _md5(BODY), len(BODY))
        assert f.downloaded_path == str(p)
        assert f.size_bytes == len(BODY)

    def test_sha256_mismatch_deletes_file(self, tmp_path):
        p, f = self._file_on_disk(tmp_path, sha256="0" * 64)
        with pytest.raises(ValueError, match="sha256 mismatch"):
            _verify_and_record(f, p, _sha(BODY), _md5(BODY), len(BODY))
        assert not p.exists()

    def test_md5_mismatch(self, tmp_path):
        p, f = self._file_on_disk(tmp_path, md5="0" * 32)
        with pytest.raises(ValueError, match="md5 mismatch"):
            _verify_and_record(f, p, _sha(BODY), _md5(BODY), len(BODY))

    def test_skip_both_checksums_still_records(self, tmp_path):
        p, f = self._file_on_disk(
            tmp_path, sha256="dead" * 16, md5="beef" * 8
        )
        _verify_and_record(
            f, p, _sha(BODY), _md5(BODY), len(BODY),
            skip_md5=True, skip_sha256=True,
        )
        # recorded digests replace the stale manifest expectations
        assert f.sha256 == _sha(BODY)
        assert f.md5 == _md5(BODY)
        assert p.exists()

    def test_no_expected_checksums_records(self, tmp_path):
        p, f = self._file_on_disk(tmp_path)
        _verify_and_record(f, p, _sha(BODY), _md5(BODY), len(BODY))
        assert f.sha256 == _sha(BODY)


# ---------------------------------------------------------------------------
# SRA accession detection
# ---------------------------------------------------------------------------


class TestExtractSraAccession:
    def test_explicit_field_wins(self):
        f = _remote_file(sra_accession="SRR999")
        assert _extract_sra_accession(f) == "SRR999"

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://x/SRR7133733_1.fastq.gz", "SRR7133733"),
            # bare "ACC.fastq.gz" (no _N suffix) is NOT detected — the
            # filename must carry an accession_prefix like SRR123_1
            ("https://x/ERR123456.fastq.gz", None),
            ("https://x/ERR123456_2.fastq.gz", "ERR123456"),
            ("ftp://x/DRR42_2.fastq.gz", "DRR42"),
            ("https://x/SRRabc_1.fastq.gz", None),
            ("https://x/XYZ123.fq", None),
            ("https://x/reads.fq", None),
        ],
    )
    def test_url_parsing(self, url, expected):
        assert _extract_sra_accession(_remote_file(url)) == expected


# ---------------------------------------------------------------------------
# Progress context (non-TTY in tests)
# ---------------------------------------------------------------------------


class TestProgressCtx:
    def test_non_tty_returns_none_tasks(self):
        with _ProgressCtx() as prog:
            task = prog.add_task("Downloading x", total=100)
            assert task is None
            prog.update(task, advance=10)  # no-op, must not raise
            prog.update_total(task, 100)

    def test_tty_path_exercises_rich(self, monkeypatch):
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        with _ProgressCtx() as prog:
            task = prog.add_task("x", total=10)
            assert task is not None
            prog.update(task, advance=5)
            prog.update_total(task, 20)


# ---------------------------------------------------------------------------
# Single-stream download
# ---------------------------------------------------------------------------


class TestSingleStream:
    def test_fresh_download(self, tmp_path):
        client = FakeClient(simple_handler())
        local = tmp_path / "f.bin"
        sha, md5, size = asyncio.run(
            _download_single_stream(
                client, "https://x/f.bin", local, "f.bin", _progress(), None
            )
        )
        assert local.read_bytes() == BODY
        assert (sha, md5, size) == (_sha(BODY), _md5(BODY), len(BODY))
        assert client.requests[0][1] == {}  # no Range header

    def test_resume_appends(self, tmp_path):
        split = len(BODY) // 2
        local = tmp_path / "f.bin"
        local.write_bytes(BODY[:split])
        client = FakeClient(simple_handler())
        sha, md5, size = asyncio.run(
            _download_single_stream(
                client, "https://x/f.bin", local, "f.bin", _progress(), None
            )
        )
        assert client.requests[0][1]["Range"] == f"bytes={split}-"
        assert local.read_bytes() == BODY
        assert size == len(BODY)
        assert sha == _sha(BODY)

    def test_resume_ignored_by_server_restarts(self, tmp_path):
        split = len(BODY) // 2
        local = tmp_path / "f.bin"
        local.write_bytes(BODY[:split])

        def no_range_handler(url, headers):
            # Server ignores Range entirely → full 200
            return FakeStreamResponse(
                200, BODY, headers={"content-length": str(len(BODY))}
            )

        client = FakeClient(no_range_handler)
        sha, md5, size = asyncio.run(
            _download_single_stream(
                client, "https://x/f.bin", local, "f.bin", _progress(), None
            )
        )
        assert local.read_bytes() == BODY  # rewritten, not doubled
        assert size == len(BODY)
        assert sha == _sha(BODY)

    def test_416_means_already_complete(self, tmp_path):
        local = tmp_path / "f.bin"
        local.write_bytes(BODY)  # pretend fully downloaded

        def handler(url, headers):
            return FakeStreamResponse(416, b"")

        client = FakeClient(handler)
        sha, md5, size = asyncio.run(
            _download_single_stream(
                client, "https://x/f.bin", local, "f.bin", _progress(), None
            )
        )
        assert sha == _sha(BODY)
        assert size == len(BODY)

    def test_http_error_propagates(self, tmp_path):
        client = FakeClient(simple_handler(status=500))
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(
                _download_single_stream(
                    client, "https://x/f", tmp_path / "f", "f", _progress(), None
                )
            )

    def test_unexpected_status_raises(self, tmp_path):
        client = FakeClient(simple_handler(status=204))
        with pytest.raises(ValueError, match="Unexpected status 204"):
            asyncio.run(
                _download_single_stream(
                    client, "https://x/f", tmp_path / "f", "f", _progress(), None
                )
            )


# ---------------------------------------------------------------------------
# Segments + segmented download
# ---------------------------------------------------------------------------


class TestSegmented:
    def test_segment_writes_range_bytes(self, tmp_path):
        client = FakeClient(simple_handler())
        part = tmp_path / "part0"
        asyncio.run(
            _download_segment(client, "u", part, 10, 19, _progress(), None)
        )
        assert part.read_bytes() == BODY[10:20]

    def test_segment_bad_status_raises(self, tmp_path):
        client = FakeClient(simple_handler(status=404))
        with pytest.raises(ValueError, match="Unexpected status 404"):
            asyncio.run(
                _download_segment(
                    client, "u", tmp_path / "p", 0, 9, _progress(), None
                )
            )

    def test_segmented_assembles_parts(self, tmp_path):
        client = FakeClient(simple_handler())
        local = tmp_path / "big.bin"
        sha, md5, size = asyncio.run(
            _download_segmented(
                client, "u", local, len(BODY), "big.bin",
                _progress(), None, 4,
            )
        )
        assert local.read_bytes() == BODY
        assert (sha, md5, size) == (_sha(BODY), _md5(BODY), len(BODY))
        # parts dir cleaned up
        assert not (tmp_path / ".big.bin.parts").exists()

    def test_segmented_cleans_parts_on_failure(self, tmp_path):
        def flaky(url, headers):
            rng = headers.get("Range", "")
            if rng.startswith("bytes=0-"):
                raise httpx.ConnectError("seg fail")
            return simple_handler()(url, headers)

        client = FakeClient(flaky)
        local = tmp_path / "big.bin"
        with pytest.raises(httpx.ConnectError):
            asyncio.run(
                _download_segmented(
                    client, "u", local, len(BODY), "big.bin",
                    _progress(), None, 2,
                )
            )
        assert not (tmp_path / ".big.bin.parts").exists()

    def test_segment_ranges_cover_whole_file(self, tmp_path):
        # odd size → last segment takes the remainder
        body = b"x" * 101
        client = FakeClient(simple_handler(body))
        local = tmp_path / "f.bin"
        asyncio.run(
            _download_segmented(
                client, "u", local, 101, "f.bin", _progress(), None, 4
            )
        )
        assert local.read_bytes() == body


# ---------------------------------------------------------------------------
# aria2c backend
# ---------------------------------------------------------------------------


class TestAria2c:
    def test_missing_binary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: None)
        with pytest.raises(FileNotFoundError, match="aria2c"):
            asyncio.run(
                _download_via_aria2c("u", tmp_path / "f", "f", _progress())
            )

    def test_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/aria2c")

        async def fake_exec(*cmd, **kw):
            out_dir = Path(cmd[cmd.index("--dir") + 1])
            out_name = cmd[cmd.index("--out") + 1]
            (out_dir / out_name).write_bytes(BODY)
            (out_dir / f"{out_name}.aria2").write_text("ctrl")
            return SimpleNamespace(
                returncode=0,
                communicate=lambda: _coro(b"", b""),
            )

        monkeypatch.setattr(
            "bioledger_isatab_schema.download.asyncio.create_subprocess_exec",
            fake_exec,
        )
        local = tmp_path / "f.bin"
        sha, md5, size = asyncio.run(
            _download_via_aria2c("u", local, "f.bin", _progress())
        )
        assert (sha, md5, size) == (_sha(BODY), _md5(BODY), len(BODY))
        assert not (tmp_path / "f.bin.aria2").exists()  # control file removed

    def test_failure_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/aria2c")

        async def fake_exec(*cmd, **kw):
            return SimpleNamespace(
                returncode=17,
                communicate=lambda: _coro(b"", b"boom"),
            )

        monkeypatch.setattr(
            "bioledger_isatab_schema.download.asyncio.create_subprocess_exec",
            fake_exec,
        )
        with pytest.raises(RuntimeError, match="aria2c failed"):
            asyncio.run(
                _download_via_aria2c(
                    "u", tmp_path / "f", "f", _progress()
                )
            )


async def _coro(a, b):
    return a, b


# ---------------------------------------------------------------------------
# SRA-Toolkit backend
# ---------------------------------------------------------------------------


class TestSraBackend:
    def test_missing_prefetch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: None)
        with pytest.raises(FileNotFoundError, match="prefetch"):
            asyncio.run(
                _download_via_sra(
                    _remote_file(), tmp_path / "f.fastq.gz",
                    "SRR1", "f.fastq.gz", _progress(), tmp_path,
                )
            )

    def test_missing_fasterq_dump(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            shutil,
            "which",
            lambda n: "/usr/bin/prefetch" if n == "prefetch" else None,
        )
        with pytest.raises(FileNotFoundError, match="fasterq-dump"):
            asyncio.run(
                _download_via_sra(
                    _remote_file(), tmp_path / "f.fastq.gz",
                    "SRR1", "f.fastq.gz", _progress(), tmp_path,
                )
            )

    def _patch_tools(self, monkeypatch, fail_prefetch=False, fail_dump=False,
                     produce_name=None):
        monkeypatch.setattr(
            shutil, "which", lambda n: f"/usr/bin/{n}"
        )

        async def fake_exec(*cmd, **kw):
            tool = cmd[0]
            if tool == "prefetch" and fail_prefetch:
                return SimpleNamespace(
                    returncode=1, communicate=lambda: _coro(b"", b"no acc")
                )
            if tool == "fasterq-dump":
                if fail_dump:
                    return SimpleNamespace(
                        returncode=1,
                        communicate=lambda: _coro(b"", b"dump fail"),
                    )
                outdir = Path(cmd[cmd.index("--outdir") + 1])
                accession = cmd[-1]
                (outdir / (produce_name or f"{accession}.fastq")).write_bytes(
                    b"@r\nACGT\n+\nIIII\n"
                )
            return SimpleNamespace(
                returncode=0, communicate=lambda: _coro(b"", b"")
            )

        monkeypatch.setattr(
            "bioledger_isatab_schema.download.asyncio.create_subprocess_exec",
            fake_exec,
        )

    def test_prefetch_failure(self, tmp_path, monkeypatch):
        self._patch_tools(monkeypatch, fail_prefetch=True)
        with pytest.raises(RuntimeError, match="prefetch failed"):
            asyncio.run(
                _download_via_sra(
                    _remote_file(), tmp_path / "f.fastq.gz",
                    "SRR1", "f.fastq.gz", _progress(), tmp_path,
                )
            )

    def test_fasterq_dump_failure(self, tmp_path, monkeypatch):
        self._patch_tools(monkeypatch, fail_dump=True)
        with pytest.raises(RuntimeError, match="fasterq-dump failed"):
            asyncio.run(
                _download_via_sra(
                    _remote_file(), tmp_path / "f.fastq.gz",
                    "SRR1", "f.fastq.gz", _progress(), tmp_path,
                )
            )

    def test_missing_expected_output(self, tmp_path, monkeypatch):
        # fasterq-dump produces a differently-named file
        self._patch_tools(monkeypatch, produce_name="unexpected.fastq")
        with pytest.raises(FileNotFoundError, match="did not produce"):
            asyncio.run(
                _download_via_sra(
                    _remote_file(), tmp_path / "SRR1_1.fastq.gz",
                    "SRR1", "SRR1_1.fastq.gz", _progress(), tmp_path,
                )
            )

    def test_success_gzips_and_hashes(self, tmp_path, monkeypatch):
        self._patch_tools(monkeypatch)
        local = tmp_path / "SRR1.fastq.gz"
        sha, md5, size = asyncio.run(
            _download_via_sra(
                _remote_file(), local, "SRR1", "SRR1.fastq.gz",
                _progress(), tmp_path,
            )
        )
        assert local.exists()
        assert gzip.open(local, "rb").read() == b"@r\nACGT\n+\nIIII\n"
        assert (sha, md5, size) == _hash_file(local)


# ---------------------------------------------------------------------------
# _download_one dispatcher
# ---------------------------------------------------------------------------


def _one(file, client, tmp_path, **kw):
    kw.setdefault("n_segments", 4)
    kw.setdefault("use_sra", False)
    kw.setdefault("use_aria2c", False)
    return asyncio.run(
        _download_one(client, file, tmp_path, _progress(), **kw)
    )


class TestDownloadOne:
    def test_non_http_scheme_rejected(self, tmp_path):
        f = _remote_file("ftp://x/f.fq")
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            _one(f, FakeClient(simple_handler()), tmp_path)

    def test_fresh_single_stream(self, tmp_path):
        f = _remote_file()
        _one(f, FakeClient(simple_handler()), tmp_path)
        written = tmp_path / "f.fastq.gz"
        assert written.read_bytes() == BODY
        assert f.downloaded_path == str(written)
        assert f.sha256 == _sha(BODY)

    def test_expected_filename_overrides_url_name(self, tmp_path):
        f = _remote_file(expected_filename="canon.fastq.gz")
        _one(f, FakeClient(simple_handler()), tmp_path)
        assert (tmp_path / "canon.fastq.gz").exists()

    def test_verified_existing_file_skipped(self, tmp_path):
        (tmp_path / "f.fastq.gz").write_bytes(BODY)
        f = _remote_file(sha256=_sha(BODY))
        client = FakeClient(simple_handler())
        _one(f, client, tmp_path)
        assert client.requests == []  # no HTTP at all
        assert f.downloaded_path == str(tmp_path / "f.fastq.gz")

    def test_stale_checksum_cannot_self_heal(self, tmp_path):
        # An existing file that fails the expected-checksum check is treated
        # as a partial download and *resumed* — a corrupt prefix can't be
        # distinguished from an interrupted transfer, so end-verification
        # fails and the file is deleted. Documented behavior.
        (tmp_path / "f.fastq.gz").write_bytes(b"stale")
        f = _remote_file(sha256=_sha(BODY))
        client = FakeClient(simple_handler())
        with pytest.raises(ValueError, match="sha256 mismatch"):
            _one(f, client, tmp_path)
        assert client.requests  # attempted a resume
        assert not (tmp_path / "f.fastq.gz").exists()

    def test_sra_route(self, tmp_path, monkeypatch):
        calls = []

        async def fake_sra(file, local, acc, name, prog, dd):
            calls.append(acc)
            local.write_bytes(BODY)
            return _sha(BODY), _md5(BODY), len(BODY)

        monkeypatch.setattr(dl, "_download_via_sra", fake_sra)
        # manifest md5 covers different (ENA-side) bytes — SRA path skips it
        f = _remote_file(
            "https://x/SRR7133733_1.fastq.gz",
            sra_accession="SRR7133733",
            md5="0" * 32,
        )
        _one(f, FakeClient(simple_handler()), tmp_path,
             use_sra=True)
        assert calls == ["SRR7133733"]
        assert f.md5 == _md5(BODY)  # recorded, not the stale manifest md5

    def test_sra_disabled_falls_to_http(self, tmp_path):
        f = _remote_file(
            "https://x/SRR7133733_1.fastq.gz", sra_accession="SRR7133733"
        )
        client = FakeClient(simple_handler())
        _one(f, client, tmp_path, use_sra=False)
        assert client.requests  # normal HTTPS download used

    def test_aria2c_route(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/aria2c")

        async def fake_aria(url, local, name, prog):
            calls.append(url)
            local.write_bytes(BODY)
            return _sha(BODY), _md5(BODY), len(BODY)

        monkeypatch.setattr(dl, "_download_via_aria2c", fake_aria)
        f = _remote_file()
        client = FakeClient(simple_handler())
        _one(f, client, tmp_path, use_aria2c=True)
        assert calls == ["https://x/f.fastq.gz"]
        assert client.requests == []  # aria2c did it all

    def test_aria2c_failure_falls_back_to_https(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/aria2c")

        async def boom(url, local, name, prog):
            raise RuntimeError("aria2c exploded")

        monkeypatch.setattr(dl, "_download_via_aria2c", boom)
        f = _remote_file()
        client = FakeClient(simple_handler())
        _one(f, client, tmp_path, use_aria2c=True)
        assert (tmp_path / "f.fastq.gz").read_bytes() == BODY

    def test_segmented_route_when_large_and_ranges_ok(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(dl, "_SEGMENT_THRESHOLD", 10)  # tiny threshold
        calls = []

        async def fake_seg(client, url, local, total, name, prog, task, n):
            calls.append((total, n))
            local.write_bytes(BODY[:total])
            return _sha(BODY[:total]), _md5(BODY[:total]), total

        monkeypatch.setattr(dl, "_download_segmented", fake_seg)
        f = _remote_file()
        _one(f, FakeClient(simple_handler()), tmp_path, n_segments=8)
        assert calls == [(len(BODY), 8)]

    def test_segmented_failure_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dl, "_SEGMENT_THRESHOLD", 10)

        async def boom(*a, **k):
            raise RuntimeError("seg fail")

        monkeypatch.setattr(dl, "_download_segmented", boom)
        f = _remote_file()
        _one(f, FakeClient(simple_handler()), tmp_path)
        assert (tmp_path / "f.fastq.gz").read_bytes() == BODY

    def test_small_file_skips_segmentation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dl, "_SEGMENT_THRESHOLD", len(BODY) + 1)
        seg_calls = []
        monkeypatch.setattr(
            dl, "_download_segmented",
            lambda *a, **k: seg_calls.append(1),
        )
        f = _remote_file()
        _one(f, FakeClient(simple_handler()), tmp_path)
        assert not seg_calls
        assert (tmp_path / "f.fastq.gz").exists()

    def test_probe_exception_falls_to_single_stream(self, tmp_path):
        def exploding(url, headers):
            if headers.get("Range") == "bytes=0-0":
                raise httpx.ConnectError("net down")
            return simple_handler()(url, headers)

        f = _remote_file()
        _one(f, FakeClient(exploding), tmp_path)
        assert (tmp_path / "f.fastq.gz").read_bytes() == BODY

    def test_existing_partial_skips_segmentation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dl, "_SEGMENT_THRESHOLD", 10)
        (tmp_path / "f.fastq.gz").write_bytes(BODY[:100])
        seg_calls = []
        monkeypatch.setattr(
            dl, "_download_segmented",
            lambda *a, **k: seg_calls.append(1),
        )
        f = _remote_file()  # no checksums → won't be "verified", will resume
        _one(f, FakeClient(simple_handler()), tmp_path)
        assert not seg_calls
        assert (tmp_path / "f.fastq.gz").read_bytes() == BODY


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _patch_http_client(monkeypatch, handler):
    """Redirect download_remote_files' internal AsyncClient to a FakeClient."""
    fake_httpx = SimpleNamespace(
        AsyncClient=lambda *a, **k: FakeClient(handler),
        Timeout=httpx.Timeout,
        Limits=httpx.Limits,
    )
    monkeypatch.setattr(dl, "httpx", fake_httpx)


class TestDownloadRemoteFiles:
    def test_requires_user_confirmation(self, tmp_path):
        ds = DataSet(name="d", files=[_remote_file()])
        with pytest.raises(ValueError, match="confirm"):
            asyncio.run(download_remote_files(ds, tmp_path))

    def test_no_remote_files_is_noop(self, tmp_path):
        ds = DataSet(name="d", files=[DataFile(location="/l/f.bam")])
        out = asyncio.run(
            download_remote_files(ds, tmp_path, user_confirmed=True)
        )
        assert out is ds

    def test_downloads_all_remote(self, tmp_path, monkeypatch):
        _patch_http_client(monkeypatch, simple_handler())
        ds = DataSet(
            name="d",
            files=[
                _remote_file("https://x/a.fq"),
                _remote_file("https://x/b.fq"),
                DataFile(location="/l/f.bam"),
            ],
        )
        out = asyncio.run(
            download_remote_files(ds, tmp_path, user_confirmed=True)
        )
        assert (tmp_path / "a.fq").read_bytes() == BODY
        assert (tmp_path / "b.fq").read_bytes() == BODY
        assert all(f.downloaded_path for f in out.remote_files() + [
            f for f in out.files if f.location.startswith("https")
        ])

    def test_single_failure_raises_bare_exception(
        self, tmp_path, monkeypatch
    ):
        _patch_http_client(monkeypatch, simple_handler(status=500))
        ds = DataSet(name="d", files=[_remote_file()])
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(
                download_remote_files(ds, tmp_path, user_confirmed=True)
            )
        assert not (tmp_path / "f.fastq.gz").exists()  # partial cleaned

    def test_multiple_failures_raise_group(self, tmp_path, monkeypatch):
        def handler(url, headers):
            if "bad" in url:
                return FakeStreamResponse(500, b"")
            return simple_handler()(url, headers)

        _patch_http_client(monkeypatch, handler)
        ds = DataSet(
            name="d",
            files=[
                _remote_file("https://x/bad1.fq"),
                _remote_file("https://x/bad2.fq"),
                _remote_file("https://x/good.fq"),
            ],
        )
        with pytest.raises(ExceptionGroup) as eg:
            asyncio.run(
                download_remote_files(ds, tmp_path, user_confirmed=True)
            )
        assert len(eg.value.exceptions) == 2
        assert (tmp_path / "good.fq").exists()  # good file still written

    def test_scheme_error_is_per_file_failure(self, tmp_path, monkeypatch):
        _patch_http_client(monkeypatch, simple_handler())
        ds = DataSet(name="d", files=[_remote_file("ftp://x/f.fq")])
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            asyncio.run(
                download_remote_files(ds, tmp_path, user_confirmed=True)
            )


class TestManifestHelpers:
    def _manifest(self):
        return Manifest.model_validate({
            "study_type": "reference_genome",
            "assembly_accession": "GCF_1.1",
            "organism": "Org",
            "files": [
                {
                    "filename": "ref.fna.gz",
                    "url": "https://x/ref.fna.gz",
                    "format": "fasta",
                    "sha256": "a" * 64,
                    "md5": "b" * 32,
                    "sra_accession": "SRR1",
                }
            ],
        })

    def test_manifest_to_datafiles(self):
        files = manifest_to_datafiles(self._manifest())
        (f,) = files
        assert f.location == "https://x/ref.fna.gz"
        assert f.format == "fasta"
        assert f.is_remote
        assert f.sha256 == "a" * 64
        assert f.md5 == "b" * 32
        assert f.expected_filename == "ref.fna.gz"
        assert f.sra_accession == "SRR1"

    def test_download_manifest_wraps(self, tmp_path, monkeypatch):
        captured = {}

        async def fake_download(dataset, dd, **kw):
            captured["dataset"] = dataset
            captured["dd"] = dd
            captured["kw"] = kw
            return dataset

        monkeypatch.setattr(dl, "download_remote_files", fake_download)
        m = self._manifest()
        out = asyncio.run(
            download_manifest(m, tmp_path, user_confirmed=True, concurrency=1)
        )
        assert captured["dd"] == tmp_path
        assert captured["kw"]["user_confirmed"] is True
        assert captured["kw"]["concurrency"] == 1
        assert captured["dataset"].name == "GCF_1.1"
        assert captured["dataset"].organisms == ["Org"]
        assert len(captured["dataset"].files) == 1
        assert out is captured["dataset"]

    def test_download_manifest_without_organism(self, tmp_path, monkeypatch):
        m = self._manifest()
        m.organism = None

        async def fake(dataset, dd, **kw):
            return dataset

        monkeypatch.setattr(dl, "download_remote_files", fake)
        asyncio.run(
            download_manifest(m, tmp_path, user_confirmed=True)
        )  # organisms=[] — no crash
