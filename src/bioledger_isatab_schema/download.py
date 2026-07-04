"""Download engine for BioLedger study data files.

Backends (in preference order when enabled):
1. **Segmented HTTPS** (default) — splits large files into N byte-range
   requests run concurrently, then concatenates.  Safe for all servers that
   support ``Accept-Ranges: bytes``; falls back to single-stream when ranges
   are not supported.
2. **aria2c** (opt-in, ``BIOLEDGER_USE_ARIA2C=1``) — delegates to ``aria2c``
   for multi-segment HTTPS; only used when the binary is present.
3. **SRA-Toolkit** (opt-in, ``BIOLEDGER_USE_SRA_TOOLKIT=1``) — uses
   ``prefetch`` + ``fasterq-dump`` for files that carry an SRA run accession
   (``ManifestFile.sra_accession``).  *Because the reconstructed FASTQ bytes
   differ from ENA's published archive, the manifest md5 is skipped for these
   files and a fresh sha256 is recorded instead.*

Cross-file concurrency is always active (bounded by ``BIOLEDGER_DOWNLOAD_CONCURRENCY``,
default 3).  Interrupted downloads resume from where they left off via
``Range`` requests; the existing partial bytes are re-hashed before appending.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from rich.progress import DownloadColumn, Progress, TransferSpeedColumn

from .dataset import DataFile, DataSet

if TYPE_CHECKING:
    from .manifest import Manifest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — env-var defaults, overridable per call
# ---------------------------------------------------------------------------

_DEFAULT_CONCURRENCY = int(os.environ.get("BIOLEDGER_DOWNLOAD_CONCURRENCY", "3"))
_DEFAULT_SEGMENTS = int(os.environ.get("BIOLEDGER_DOWNLOAD_SEGMENTS", "4"))
_SEGMENT_THRESHOLD = int(
    os.environ.get("BIOLEDGER_DOWNLOAD_SEGMENT_THRESHOLD_MB", "256")
) * 1024 * 1024
_USE_SRA_TOOLKIT = os.environ.get("BIOLEDGER_USE_SRA_TOOLKIT", "0") == "1"
_USE_ARIA2C = os.environ.get("BIOLEDGER_USE_ARIA2C", "0") == "1"
_CHUNK = 65536

# ---------------------------------------------------------------------------
# Shared verification helper
# ---------------------------------------------------------------------------


def _verify_and_record(
    file: DataFile,
    local_path: Path,
    sha_digest: str,
    md5_digest: str,
    size: int,
    *,
    skip_md5: bool = False,
) -> None:
    """Verify computed checksums against the file record and update it in-place.

    Raises ``ValueError`` on mismatch and deletes the partial file.
    ``skip_md5=True`` is used for the SRA-Toolkit path where the local gzip
    bytes differ from the ENA-published archive that the manifest md5 covers.
    """
    checks = [("sha256", file.sha256, sha_digest)]
    if not skip_md5:
        checks.append(("md5", file.md5, md5_digest))

    for algo, expected, got in checks:
        if expected and expected.lower() != got:
            local_path.unlink(missing_ok=True)
            raise ValueError(
                f"{algo} mismatch for {local_path.name}: expected {expected}, got {got}"
            )

    file.downloaded_path = str(local_path)
    file.size_bytes = size
    file.sha256 = sha_digest
    file.md5 = md5_digest


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> tuple[str, str, int]:
    """Return (sha256_hex, md5_hex, size) for the entire contents of *path*."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), size


def _seed_hashers_from_file(path: Path) -> tuple[hashlib._Hash, hashlib._Hash, int]:
    """Return live (sha256, md5, size) hashers seeded with *path* contents for resuming."""
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha, md5, size


# ---------------------------------------------------------------------------
# Progress context — degrades to logging when not a TTY
# ---------------------------------------------------------------------------


class _ProgressCtx:
    """Thin wrapper that uses ``rich.Progress`` in interactive sessions and
    falls back to ``logger.info`` messages in non-TTY (agent / CI) contexts."""

    def __init__(self) -> None:
        self._tty = sys.stderr.isatty()
        self._progress: Progress | None = None

    def __enter__(self) -> "_ProgressCtx":
        if self._tty:
            self._progress = Progress(
                *Progress.get_default_columns(),
                DownloadColumn(),
                TransferSpeedColumn(),
                refresh_per_second=4,
                transient=False,
            )
            self._progress.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        if self._progress:
            self._progress.__exit__(*args)

    def add_task(self, description: str, total: int | None) -> object:
        if self._progress:
            return self._progress.add_task(description, total=total)
        logger.info("%s (total: %s bytes)", description, total or "unknown")
        return None

    def update(self, task: object, advance: int) -> None:
        if self._progress and task is not None:
            self._progress.update(task, advance=advance)  # type: ignore[arg-type]

    def update_total(self, task: object, total: int) -> None:
        """Backfill the total for a task once it becomes known."""
        if self._progress and task is not None:
            self._progress.update(task, total=total)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Single-stream download with resume
# ---------------------------------------------------------------------------


async def _download_single_stream(
    client: httpx.AsyncClient,
    url: str,
    local_path: Path,
    expected_name: str,
    progress: _ProgressCtx,
    task: object,
) -> tuple[str, str, int]:
    """Stream-download *url* to *local_path*, resuming if a partial file exists.

    Returns ``(sha256_hex, md5_hex, size_bytes)``.
    """
    resume_pos = 0
    sha = hashlib.sha256()
    md5 = hashlib.md5()
    size = 0

    if local_path.exists():
        existing_size = local_path.stat().st_size
        if existing_size > 0:
            sha, md5, size = await asyncio.to_thread(_seed_hashers_from_file, local_path)
            resume_pos = existing_size
            logger.info("Resuming %s from byte %d", expected_name, resume_pos)

    headers = {}
    if resume_pos:
        headers["Range"] = f"bytes={resume_pos}-"

    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 416:
            # Range Not Satisfiable — file is already fully downloaded
            logger.info("%s already complete (416 Range Not Satisfiable)", expected_name)
            async for _ in response.aiter_bytes():
                pass
            if task is not None:
                existing = local_path.stat().st_size if local_path.exists() else 0
                progress.update_total(task, existing)
                progress.update(task, advance=existing)
            return sha.hexdigest(), md5.hexdigest(), size

        if resume_pos and response.status_code == 200:
            # Server ignored our Range header — restart from scratch
            logger.warning(
                "Server returned 200 (no range support) for %s; restarting", expected_name
            )
            sha = hashlib.sha256()
            md5 = hashlib.md5()
            size = 0
            resume_pos = 0

        response.raise_for_status()

        if response.status_code not in (200, 206):
            raise ValueError(
                f"Unexpected status {response.status_code} downloading {expected_name}"
            )

        remote_content_length = int(response.headers.get("content-length", 0))
        # Total for the progress bar = already-on-disk + bytes still to receive
        total_hint = (resume_pos + remote_content_length) if remote_content_length else None
        if task is None:
            task = progress.add_task(f"Downloading {expected_name}", total=total_hint)
        elif total_hint:
            progress.update_total(task, total_hint)
            if resume_pos:
                # Fast-forward the bar past the already-downloaded bytes
                progress.update(task, advance=resume_pos)

        mode = "ab" if resume_pos else "wb"
        with open(local_path, mode) as fh:
            async for chunk in response.aiter_bytes(chunk_size=_CHUNK):
                fh.write(chunk)
                sha.update(chunk)
                md5.update(chunk)
                size += len(chunk)
                progress.update(task, advance=len(chunk))

    return sha.hexdigest(), md5.hexdigest(), size


# ---------------------------------------------------------------------------
# Segmented multi-connection download
# ---------------------------------------------------------------------------


async def _download_segment(
    client: httpx.AsyncClient,
    url: str,
    part_path: Path,
    start: int,
    end: int,
    progress: _ProgressCtx | None = None,
    task: object = None,
) -> None:
    """Download a single byte-range segment to *part_path*."""
    headers = {"Range": f"bytes={start}-{end}"}
    async with client.stream("GET", url, headers=headers) as response:
        if response.status_code not in (206, 200):
            raise ValueError(
                f"Unexpected status {response.status_code} for range {start}-{end} of {url}"
            )
        with open(part_path, "wb") as fh:
            async for chunk in response.aiter_bytes(chunk_size=_CHUNK):
                fh.write(chunk)
                if progress is not None:
                    progress.update(task, advance=len(chunk))


async def _download_segmented(
    client: httpx.AsyncClient,
    url: str,
    local_path: Path,
    total: int,
    expected_name: str,
    progress: _ProgressCtx,
    task: object,
    n_segments: int,
) -> tuple[str, str, int]:
    """Download *url* in *n_segments* parallel byte-range requests.

    Returns ``(sha256_hex, md5_hex, size_bytes)``.  Falls back to single-stream
    if any segment fails (e.g. transient error) by propagating the exception —
    the caller catches and retries via :func:`_download_single_stream`.
    """
    seg_size = total // n_segments
    ranges = [
        (i * seg_size, (i + 1) * seg_size - 1 if i < n_segments - 1 else total - 1)
        for i in range(n_segments)
    ]

    tmp_dir = local_path.parent / f".{local_path.name}.parts"
    tmp_dir.mkdir(exist_ok=True)
    part_paths = [tmp_dir / f"part{i}" for i in range(n_segments)]

    try:
        seg_sem = asyncio.Semaphore(n_segments)

        async def _bounded_segment(i: int) -> None:
            async with seg_sem:
                await _download_segment(
                    client, url, part_paths[i], ranges[i][0], ranges[i][1],
                    progress=progress, task=task,
                )

        await asyncio.gather(*[_bounded_segment(i) for i in range(n_segments)])

        # Concatenate parts and compute checksums in one pass (no progress advance —
        # bytes were already counted during segment download above)
        sha = hashlib.sha256()
        md5 = hashlib.md5()
        size = 0
        with open(local_path, "wb") as out:
            for pp in part_paths:
                with open(pp, "rb") as fh:
                    for chunk in iter(lambda: fh.read(_CHUNK), b""):
                        out.write(chunk)
                        sha.update(chunk)
                        md5.update(chunk)
                        size += len(chunk)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return sha.hexdigest(), md5.hexdigest(), size


# ---------------------------------------------------------------------------
# aria2c backend (opt-in)
# ---------------------------------------------------------------------------


async def _download_via_aria2c(
    url: str,
    local_path: Path,
    expected_name: str,
    progress: _ProgressCtx,
) -> tuple[str, str, int]:
    """Download *url* via ``aria2c`` (must be on PATH).

    Returns ``(sha256_hex, md5_hex, size_bytes)`` after verifying the output.
    Raises ``FileNotFoundError`` if ``aria2c`` is not available.
    """
    if not shutil.which("aria2c"):
        raise FileNotFoundError("aria2c not found on PATH")

    cmd = [
        "aria2c",
        "--dir", str(local_path.parent),
        "--out", local_path.name,
        "--continue=true",
        "--split=4",
        "--max-connection-per-server=4",
        "--min-split-size=64M",
        "--quiet",
        url,
    ]
    logger.info("aria2c download: %s", expected_name)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    # Clean up aria2 control files regardless of outcome
    for ctrl in local_path.parent.glob(f"{local_path.name}.aria2"):
        ctrl.unlink(missing_ok=True)

    if proc.returncode != 0:
        raise RuntimeError(
            f"aria2c failed for {expected_name}: {stderr.decode(errors='replace').strip()}"
        )

    sha, md5, size = _hash_file(local_path)
    progress.add_task(f"Verified {expected_name}", total=size)
    return sha.hexdigest(), md5.hexdigest(), size


# ---------------------------------------------------------------------------
# SRA-Toolkit backend (opt-in)
# ---------------------------------------------------------------------------

_SRA_ACCESSION_PREFIXES = ("SRR", "ERR", "DRR")


def _extract_sra_accession(file: DataFile) -> str | None:
    """Return the SRA run accession for *file* if one can be determined.

    Checks ``file.sra_accession`` first, then tries to parse the URL filename.
    """
    if file.sra_accession:
        return file.sra_accession
    # Try to detect from URL: .../SRR7133733/SRR7133733_1.fastq.gz
    stem = Path(file.location).name.split("_")[0]
    if stem[:3] in _SRA_ACCESSION_PREFIXES and stem[3:].isdigit():
        return stem
    return None


async def _download_via_sra(
    file: DataFile,
    local_path: Path,
    accession: str,
    expected_name: str,
    progress: _ProgressCtx,
    download_dir: Path,
) -> tuple[str, str, int]:
    """Download *accession* via ``prefetch`` + ``fasterq-dump``.

    Produces a gzipped FASTQ at *local_path*.  The manifest md5 is **not**
    verified because local re-compression produces different bytes; a fresh
    sha256 is recorded instead.

    Raises ``FileNotFoundError`` if ``prefetch`` or ``fasterq-dump`` are absent.
    Raises ``RuntimeError`` if either subprocess fails.
    """
    if not shutil.which("prefetch"):
        raise FileNotFoundError("prefetch (SRA-Toolkit) not found on PATH")
    if not shutil.which("fasterq-dump"):
        raise FileNotFoundError("fasterq-dump (SRA-Toolkit) not found on PATH")

    with tempfile.TemporaryDirectory(prefix="bioledger_sra_") as tmp:
        tmp_path = Path(tmp)

        # prefetch — downloads .sra archive
        logger.info("prefetch %s", accession)
        r1 = await asyncio.create_subprocess_exec(
            "prefetch", accession, "--output-directory", tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err1 = await r1.communicate()
        if r1.returncode != 0:
            raise RuntimeError(
                f"prefetch failed for {accession}: {err1.decode(errors='replace').strip()}"
            )

        # fasterq-dump — converts to FASTQ
        logger.info("fasterq-dump %s", accession)
        r2 = await asyncio.create_subprocess_exec(
            "fasterq-dump",
            "--split-files",
            "--outdir", tmp,
            "--temp", tmp,
            accession,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err2 = await r2.communicate()
        if r2.returncode != 0:
            raise RuntimeError(
                f"fasterq-dump failed for {accession}: {err2.decode(errors='replace').strip()}"
            )

        # Match the produced FASTQ to the expected filename.
        # expected_name is e.g. "SRR7133733_1.fastq.gz"; the plain FASTQ is
        # "SRR7133733_1.fastq" or "SRR7133733.fastq" for single-end.
        plain_name = expected_name
        if plain_name.endswith(".gz"):
            plain_name = plain_name[:-3]
        fastq_candidates = list(tmp_path.rglob(plain_name)) or list(
            tmp_path.rglob(f"{accession}.fastq")
        )
        if not fastq_candidates:
            raise FileNotFoundError(
                f"fasterq-dump did not produce expected file {plain_name!r} for {accession}"
            )
        fastq_path = fastq_candidates[0]

        logger.info("gzipping %s -> %s", fastq_path.name, expected_name)
        sha = hashlib.sha256()
        md5 = hashlib.md5()
        size = 0
        with open(fastq_path, "rb") as src, gzip.open(local_path, "wb") as dst:
            for chunk in iter(lambda: src.read(_CHUNK), b""):
                dst.write(chunk)
                sha.update(chunk)   # hash the uncompressed bytes for reproducibility
                md5.update(chunk)
                size += len(chunk)

    progress.add_task(f"SRA converted {expected_name}", total=size)
    # Return computed digests; caller will skip md5 verification.
    return sha.hexdigest(), md5.hexdigest(), size


# ---------------------------------------------------------------------------
# Per-file download dispatcher
# ---------------------------------------------------------------------------


async def _download_one(
    client: httpx.AsyncClient,
    file: DataFile,
    download_dir: Path,
    progress: _ProgressCtx,
    *,
    n_segments: int,
    use_sra: bool,
    use_aria2c: bool,
) -> None:
    """Download a single remote *file* to *download_dir*, choosing backend."""
    if not file.location.lower().startswith(("http://", "https://")):
        raise ValueError(
            f"Unsupported URL scheme for {file.location!r}: only http(s) is "
            "supported. Use an https mirror (e.g. ENA https FASTQ URLs)."
        )

    expected_name = file.expected_filename or Path(file.location).name
    local_path = download_dir / expected_name

    # ---- Skip if already downloaded and verified ----
    if local_path.exists():
        if file.sha256 or file.md5:
            # Run in a thread so we don't block the event loop while hashing GBs
            sha_digest, md5_digest, _ = await asyncio.to_thread(_hash_file, local_path)
            if (file.sha256 and sha_digest == file.sha256) or (
                file.md5 and md5_digest == file.md5
            ):
                file.downloaded_path = str(local_path)
                logger.info("Already downloaded (verified): %s — skipping", expected_name)
                return

    logger.info("Starting download: %s", expected_name)

    # ---- SRA-Toolkit path (opt-in) ----
    sra_acc = _extract_sra_accession(file) if use_sra else None
    if sra_acc:
        with _ProgressCtx() as sub_progress:
            sha_digest, md5_digest, size = await _download_via_sra(
                file, local_path, sra_acc, expected_name, sub_progress, download_dir
            )
        _verify_and_record(
            file, local_path, sha_digest, md5_digest, size, skip_md5=True
        )
        logger.info(
            "SRA download complete: %s (sha256=%s)", expected_name, sha_digest[:16]
        )
        return

    # ---- aria2c path (opt-in) ----
    if use_aria2c and shutil.which("aria2c"):
        try:
            sha_digest, md5_digest, size = await _download_via_aria2c(
                file.location, local_path, expected_name, progress
            )
            _verify_and_record(file, local_path, sha_digest, md5_digest, size)
            return
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning(
                "aria2c failed for %s, falling back to HTTPS: %s", expected_name, exc
            )

    # ---- Decide whether to use segmented download ----
    # We avoid a separate HEAD request (extra RTT, can trigger throttling on ENA/EBI).
    # Instead we send a minimal Range probe (bytes=0-0) to check range support and
    # get the real content-length from Content-Range without downloading the file.
    # If the server doesn't support ranges or the file is below the threshold we fall
    # straight through to single-stream.

    task = progress.add_task(f"Downloading {expected_name}", total=None)

    if n_segments > 1 and not local_path.exists():
        accepts_ranges = False
        content_length = 0
        try:
            async with client.stream(
                "GET", file.location, headers={"Range": "bytes=0-0"}
            ) as probe:
                if probe.status_code == 206:
                    accepts_ranges = True
                    cr = probe.headers.get("content-range", "")
                    total_str = cr.split("/")[-1] if "/" in cr else "0"
                    try:
                        content_length = int(total_str)
                    except ValueError:
                        content_length = 0
                # Always drain the 1-byte body so the connection is reusable
                async for _ in probe.aiter_bytes():
                    pass
        except Exception:
            pass

        if accepts_ranges and content_length >= _SEGMENT_THRESHOLD:
            progress.update_total(task, content_length)
            try:
                sha_digest, md5_digest, size = await _download_segmented(
                    client, file.location, local_path, content_length,
                    expected_name, progress, task, n_segments,
                )
                _verify_and_record(file, local_path, sha_digest, md5_digest, size)
                return
            except Exception as exc:
                logger.warning(
                    "Segmented download failed for %s (%s); falling back to single-stream",
                    expected_name, exc,
                )
                local_path.unlink(missing_ok=True)

    # ---- Single-stream with resume (default / fallback) ----
    sha_digest, md5_digest, size = await _download_single_stream(
        client, file.location, local_path, expected_name, progress, task
    )
    _verify_and_record(file, local_path, sha_digest, md5_digest, size)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def download_remote_files(
    dataset: DataSet,
    download_dir: Path,
    user_confirmed: bool = False,
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    n_segments: int = _DEFAULT_SEGMENTS,
    use_sra_toolkit: bool = _USE_SRA_TOOLKIT,
    use_aria2c: bool = _USE_ARIA2C,
) -> DataSet:
    """Download all remote files in a dataset to a local directory.

    Args:
        dataset: DataSet with remote files.
        download_dir: Where to save downloaded files.
        user_confirmed: Must be True to proceed (safety check).
        concurrency: Max number of files to download in parallel (default 3,
            override via ``BIOLEDGER_DOWNLOAD_CONCURRENCY``).
        n_segments: Number of byte-range segments per file for large files
            (default 4, override via ``BIOLEDGER_DOWNLOAD_SEGMENTS``).
        use_sra_toolkit: Use ``prefetch``/``fasterq-dump`` for SRA-tagged files
            (default False, enable via ``BIOLEDGER_USE_SRA_TOOLKIT=1``).
        use_aria2c: Use ``aria2c`` for multi-segment HTTPS when available
            (default False, enable via ``BIOLEDGER_USE_ARIA2C=1``).

    Returns:
        Updated DataSet with ``downloaded_path`` set for all remote files.

    Raises:
        ValueError: If ``user_confirmed`` is False or a checksum mismatches.
        ExceptionGroup: If one or more files fail to download (Python 3.11+).
    """
    if not user_confirmed:
        raise ValueError("User must confirm file downloads before proceeding")

    remote_files = dataset.remote_files()
    if not remote_files:
        return dataset

    download_dir.mkdir(parents=True, exist_ok=True)

    sem = asyncio.Semaphore(concurrency)

    async def _bounded(f: DataFile) -> None:
        async with sem:
            await _download_one(
                client, f, download_dir, progress,
                n_segments=n_segments,
                use_sra=use_sra_toolkit,
                use_aria2c=use_aria2c,
            )

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=concurrency * n_segments + 4,
            max_keepalive_connections=concurrency * n_segments + 4,
        ),
    ) as client:
        with _ProgressCtx() as progress:
            results = await asyncio.gather(
                *[_bounded(f) for f in remote_files], return_exceptions=True
            )

    # Collect failures, clean up partials, surface aggregate error
    errors: list[BaseException] = []
    for file, result in zip(remote_files, results):
        if isinstance(result, BaseException):
            expected_name = file.expected_filename or Path(file.location).name
            partial = download_dir / expected_name
            partial.unlink(missing_ok=True)
            errors.append(result)

    if errors:
        if len(errors) == 1:
            raise errors[0]
        raise ExceptionGroup(
            f"{len(errors)} file(s) failed to download",
            [e for e in errors if isinstance(e, Exception)],
        )

    return dataset


def manifest_to_datafiles(manifest: "Manifest") -> list[DataFile]:
    """Build remote :class:`DataFile` records from a study ``Manifest``.

    Each file carries its canonical ``expected_filename``, expected
    checksum(s), and optional ``sra_accession`` so the downloader can route
    tagged files to the SRA-Toolkit fast-path.
    """
    return [
        DataFile(
            location=f.url,
            format=f.format,
            is_remote=True,
            sha256=f.sha256,
            md5=f.md5,
            expected_filename=f.filename,
            sra_accession=f.sra_accession,
        )
        for f in manifest.files
    ]


async def download_manifest(
    manifest: "Manifest",
    download_dir: Path,
    user_confirmed: bool = False,
    *,
    concurrency: int = _DEFAULT_CONCURRENCY,
    n_segments: int = _DEFAULT_SEGMENTS,
    use_sra_toolkit: bool = _USE_SRA_TOOLKIT,
    use_aria2c: bool = _USE_ARIA2C,
) -> DataSet:
    """Download all files declared in a study ``Manifest`` to ``download_dir``.

    Writes each file to its manifest ``filename`` and verifies checksums.
    Returns a :class:`DataSet` wrapping the downloaded files.
    All keyword args are forwarded to :func:`download_remote_files`.
    """
    dataset = DataSet(
        name=manifest.accession or "manifest",
        files=manifest_to_datafiles(manifest),
        organisms=[manifest.organism] if manifest.organism else [],
    )
    return await download_remote_files(
        dataset,
        download_dir,
        user_confirmed=user_confirmed,
        concurrency=concurrency,
        n_segments=n_segments,
        use_sra_toolkit=use_sra_toolkit,
        use_aria2c=use_aria2c,
    )
