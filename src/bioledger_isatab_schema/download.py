from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from rich.progress import DownloadColumn, Progress, TransferSpeedColumn

from .dataset import DataFile, DataSet

if TYPE_CHECKING:
    from .manifest import Manifest


async def download_remote_files(
    dataset: DataSet,
    download_dir: Path,
    user_confirmed: bool = False,
) -> DataSet:
    """Download all remote files in a dataset to a local directory.

    Args:
        dataset: DataSet with remote files
        download_dir: Where to save downloaded files
        user_confirmed: Must be True to proceed (safety check)

    Returns:
        Updated DataSet with downloaded_path set for all remote files
    """
    if not user_confirmed:
        raise ValueError("User must confirm file downloads before proceeding")

    remote_files = dataset.remote_files()
    if not remote_files:
        return dataset  # nothing to download

    download_dir.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        with Progress(
            *Progress.get_default_columns(),
            DownloadColumn(),
            TransferSpeedColumn(),
        ) as progress:
            for file in remote_files:
                # httpx has no FTP support — fail loudly rather than silently.
                if not file.location.lower().startswith(("http://", "https://")):
                    raise ValueError(
                        f"Unsupported URL scheme for {file.location!r}: only http(s) is "
                        "supported. Use an https mirror (e.g. ENA https FASTQ URLs)."
                    )

                # Honor an expected canonical filename if the caller set one
                # (e.g. from a manifest entry); otherwise derive from the URL.
                expected_name = file.expected_filename or Path(file.location).name
                local_path = download_dir / expected_name

                # Stream download — safe for multi-GB files
                async with client.stream("GET", file.location) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length", 0)) or None
                    task = progress.add_task(f"Downloading {expected_name}", total=total)
                    sha = hashlib.sha256()
                    md5 = hashlib.md5()
                    size = 0

                    with open(local_path, "wb") as fh:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            fh.write(chunk)
                            sha.update(chunk)
                            md5.update(chunk)
                            size += len(chunk)
                            progress.update(task, advance=len(chunk))

                sha_digest = sha.hexdigest()
                md5_digest = md5.hexdigest()

                # Verify against whichever expected checksum(s) were declared.
                for algo, expected, got in (
                    ("sha256", file.sha256, sha_digest),
                    ("md5", file.md5, md5_digest),
                ):
                    if expected and expected.lower() != got:
                        local_path.unlink(missing_ok=True)
                        raise ValueError(
                            f"{algo} mismatch for {expected_name}: expected {expected}, got {got}"
                        )

                # Update file record
                file.downloaded_path = str(local_path)
                file.size_bytes = size
                file.sha256 = sha_digest
                file.md5 = md5_digest

    return dataset


def manifest_to_datafiles(manifest: "Manifest") -> list[DataFile]:
    """Build remote :class:`DataFile` records from a study ``Manifest``.

    Each file carries its canonical ``expected_filename`` and expected
    checksum(s) (``sha256`` and/or ``md5``) so :func:`download_remote_files`
    writes to the right name and verifies integrity.
    """
    return [
        DataFile(
            location=f.url,
            format=f.format,
            is_remote=True,
            sha256=f.sha256,
            md5=f.md5,
            expected_filename=f.filename,
        )
        for f in manifest.files
    ]


async def download_manifest(
    manifest: "Manifest",
    download_dir: Path,
    user_confirmed: bool = False,
) -> DataSet:
    """Download all files declared in a study ``Manifest`` to ``download_dir``.

    Writes each file to its manifest ``filename`` and verifies the required
    ``sha256`` (raising on mismatch). Returns a :class:`DataSet` wrapping the
    downloaded files.
    """
    dataset = DataSet(
        name=manifest.accession or "manifest",
        files=manifest_to_datafiles(manifest),
        organisms=[manifest.organism] if manifest.organism else [],
    )
    return await download_remote_files(dataset, download_dir, user_confirmed=user_confirmed)
