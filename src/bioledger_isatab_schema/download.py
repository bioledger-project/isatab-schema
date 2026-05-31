from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
from rich.progress import DownloadColumn, Progress, TransferSpeedColumn

from .dataset import DataSet


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
                filename = Path(file.location).name
                local_path = download_dir / filename

                # Stream download — safe for multi-GB files
                async with client.stream("GET", file.location) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length", 0)) or None
                    task = progress.add_task(f"Downloading {filename}", total=total)
                    hasher = hashlib.sha256()
                    size = 0

                    with open(local_path, "wb") as fh:
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            fh.write(chunk)
                            hasher.update(chunk)
                            size += len(chunk)
                            progress.update(task, advance=len(chunk))

                # Update file record
                file.downloaded_path = str(local_path)
                file.size_bytes = size
                file.sha256 = hasher.hexdigest()

    return dataset
