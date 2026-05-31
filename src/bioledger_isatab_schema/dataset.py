from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class DataFile(BaseModel):
    """A single data file in the dataset.
    Can be local or remote (URL). Tracks format, size, and download status."""

    location: str  # local path or URL (http://, ftp://, s3://, etc.)
    format: str = "unknown"  # inferred from extension or ISA-Tab metadata
    size_bytes: int | None = None  # known size (if local or from HTTP headers)
    is_remote: bool = False  # True if location is a URL
    downloaded_path: str | None = None  # local path after download (if was remote)
    sha256: str | None = None  # checksum (if available)
    sample_name: str = ""  # which sample this file belongs to

    @property
    def is_downloaded(self) -> bool:
        """True if remote file has been downloaded locally."""
        return self.is_remote and self.downloaded_path is not None


class DataSet(BaseModel):
    """Bridge between ISA-Tab and AnalysisForge.

    Loaded from any ISA-Tab source (ISAForge-generated, public repos, user-created).
    Tracks all file formats, organisms, and provides smart tool matching."""

    name: str
    description: str = ""

    # Files
    files: list[DataFile] = []
    file_formats: list[str] = Field(default_factory=list)  # all detected formats (deduplicated)

    # Biological context
    organisms: list[str] = []  # support multi-organism (host-pathogen, etc.)
    assay_type: str = ""  # e.g. "RNA-seq", "WGS", "ChIP-seq"

    # Metadata
    sample_metadata: dict[str, Any] = {}  # sample characteristics, factors, etc.
    isa_tab_dir: Path | None = None  # path to ISA-Tab files on disk

    def remote_files(self) -> list[DataFile]:
        """Return all remote (not yet downloaded) files."""
        return [f for f in self.files if f.is_remote and not f.is_downloaded]

    def local_files(self) -> list[DataFile]:
        """Return all local files (either originally local or downloaded)."""
        return [f for f in self.files if not f.is_remote or f.is_downloaded]

    def files_by_format(self, fmt: str) -> list[DataFile]:
        """Return all files matching a specific format."""
        return [f for f in self.files if f.format == fmt]


_COMPRESSION_EXTS = {".gz", ".bz2", ".xz", ".zst"}

_FORMAT_MAP = {
    "fastq": "fastq",
    "fq": "fastq",
    "fasta": "fasta",
    "fa": "fasta",
    "bam": "bam",
    "sam": "sam",
    "cram": "cram",
    "vcf": "vcf",
    "bcf": "bcf",
    "bed": "bed",
    "gff": "gff",
    "gff3": "gff",
    "gtf": "gtf",
    "bw": "bigwig",
    "bigwig": "bigwig",
    "h5ad": "h5ad",
    "csv": "csv",
    "tsv": "tsv",
    "txt": "tsv",
    "json": "json",
}


def _infer_format(location: str) -> str:
    """Map a filename/path to canonical format name.
    Strips compression extensions (.gz, .bz2, .xz, .zst) first,
    so 'sample.fastq.gz' → 'fastq', not 'unknown'."""
    p = Path(location)
    # Strip compression suffix(es)
    while p.suffix.lower() in _COMPRESSION_EXTS:
        p = p.with_suffix("")
    ext = p.suffix.lstrip(".").lower()
    return _FORMAT_MAP.get(ext, ext or "unknown")


@dataclass
class ParsedCSV:
    """Result of parsing a CSV samplesheet — used by both
    load_dataset_from_csv and csv_to_isatab."""

    rows: list[dict[str, str]]
    fieldnames: list[str]
    sample_col: str | None
    organism_col: str | None
    file_columns: list[str]


def parse_csv_samplesheet(csv_path: Path) -> ParsedCSV:
    """Parse a CSV samplesheet, detecting sample ID, organism, and file columns.

    Expects a CSV with at least a header row. Columns whose values look like
    file paths (contain '.' with a recognized extension) are treated as data
    file references.  A column named 'organism' (case-insensitive) populates
    the organisms list.  A column named 'sample_id' or 'sample_name' is used
    as the sample identifier.
    """
    import csv

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV file appears empty: {csv_path}")

        rows = list(reader)

    if not rows:
        raise ValueError(f"CSV file has headers but no data rows: {csv_path}")

    headers_lower = {h.lower(): h for h in reader.fieldnames}

    # Detect sample ID column
    sample_col = None
    for candidate in ("sample_id", "sample_name", "sample", "name", "id"):
        if candidate in headers_lower:
            sample_col = headers_lower[candidate]
            break

    # Detect organism column
    organism_col = headers_lower.get("organism")

    # Detect file-like columns (values with recognized extensions)
    file_columns: list[str] = []
    for col in reader.fieldnames:
        for row in rows[:5]:  # check first few rows
            val = row.get(col, "")
            if val and "." in val:
                ext = val.rsplit(".", 1)[-1].lower()
                # Strip compression
                if ext in ("gz", "bz2", "xz", "zst"):
                    parts = val.rsplit(".", 2)
                    if len(parts) >= 2:
                        # parts[-2] is the extension before compression (e.g., 'fastq' in 'file.fastq.gz')
                        ext = parts[-2].rsplit(".", 1)[-1].lower() if "." in parts[-2] else parts[-2].lower()
                if ext in _FORMAT_MAP:
                    file_columns.append(col)
                    break

    return ParsedCSV(
        rows=rows,
        fieldnames=list(reader.fieldnames),
        sample_col=sample_col,
        organism_col=organism_col,
        file_columns=file_columns,
    )


def load_dataset_from_csv(csv_path: Path) -> DataSet:
    """Load a DataSet from a CSV samplesheet.

    This is a lightweight loader for quick exploration. For proper provenance
    tracking, use csv_to_isatab() in isaforge.builder to convert to ISA-Tab
    first, then load with load_dataset_from_isatab().
    """
    parsed = parse_csv_samplesheet(csv_path)

    # Build files and metadata
    files: list[DataFile] = []
    seen_formats: set[str] = set()
    file_formats: list[str] = []
    organisms: list[str] = []
    sample_metadata: dict[str, dict] = {}

    csv_dir = csv_path.parent

    for row in parsed.rows:
        sample_name = row.get(parsed.sample_col, "") if parsed.sample_col else ""

        # Collect organism
        if parsed.organism_col:
            org = row.get(parsed.organism_col, "").strip()
            if org and org not in organisms:
                organisms.append(org)

        # Collect file references
        for col in parsed.file_columns:
            location = row.get(col, "").strip()
            if not location:
                continue
            is_remote = location.startswith(("http://", "https://", "ftp://", "s3://"))
            fmt = _infer_format(location)
            if fmt not in seen_formats:
                seen_formats.add(fmt)
                file_formats.append(fmt)

            if not is_remote:
                resolved = csv_dir / location
                location = str(resolved) if resolved.exists() else location

            files.append(
                DataFile(
                    location=location,
                    format=fmt,
                    is_remote=is_remote,
                    sample_name=sample_name,
                )
            )

        # Build sample metadata from non-file columns
        meta_cols = [c for c in parsed.fieldnames if c not in parsed.file_columns]
        sample_metadata[sample_name or f"row_{parsed.rows.index(row)}"] = {
            col: row.get(col, "") for col in meta_cols
        }

    # Also register the CSV itself as a file
    csv_ref = DataFile(
        location=str(csv_path.resolve()),
        format="csv",
        sample_name="",
    )
    if "csv" not in seen_formats:
        file_formats.insert(0, "csv")
    files.insert(0, csv_ref)

    dataset_name = csv_path.stem.replace("_", " ").replace("-", " ").title()

    return DataSet(
        name=dataset_name,
        description=f"Loaded from CSV samplesheet: {csv_path.name}",
        files=files,
        file_formats=file_formats,
        organisms=organisms,
        sample_metadata=sample_metadata,
    )


def load_dataset_from_isatab(isa_dir: Path, validate: bool = True) -> DataSet:
    """Load a DataSet from any ISA-Tab directory.

    Args:
        isa_dir: Path to directory containing i_investigation.txt
        validate: If True, run validation and raise on errors

    Returns:
        DataSet with all metadata and file references

    Raises:
        ValueError: If validation fails (when validate=True)
    """
    from isatools import isatab

    from .validate import validate_isatab

    isa_dir = isa_dir.resolve()

    # Validate first
    if validate:
        result = validate_isatab(isa_dir)
        if not result.is_valid:
            errors = [i.message for i in result.issues if i.severity.value == "error"]
            raise ValueError(f"ISA-Tab validation failed: {'; '.join(errors)}")

    # Load with isatools
    inv = isatab.load(str(isa_dir))
    study = inv.studies[0] if inv.studies else None

    if not study:
        raise ValueError("No study found in ISA-Tab")

    # Extract organisms (support multiple)
    organisms = []
    for source in study.sources:
        for char in source.characteristics:
            if "organism" in (char.category.term or "").lower():
                org = char.value.term if hasattr(char.value, "term") else str(char.value)
                if org and org not in organisms:
                    organisms.append(org)

    # Extract assay type
    assay_type = ""
    if study.assays:
        assay_type = (
            study.assays[0].technology_type.term if study.assays[0].technology_type else ""
        )

    # Extract files — both ISA-Tab structural files and assay data files
    files: list[DataFile] = []
    seen_formats: set[str] = set()
    file_formats: list[str] = []

    # Include ISA-Tab structural files (investigation, study, assay tables)
    for isa_file in sorted(isa_dir.glob("*.txt")):
        if isa_file.is_file():
            fmt = _infer_format(isa_file.name)
            if fmt not in seen_formats:
                seen_formats.add(fmt)
                file_formats.append(fmt)
            files.append(
                DataFile(
                    location=str(isa_file),
                    format=fmt,
                    is_remote=False,
                )
            )

    for assay in study.assays:
        for df in assay.data_files:
            location = df.filename
            is_remote = location.startswith(("http://", "https://", "ftp://", "s3://"))

            # Infer format from extension (strip compression first)
            fmt = _infer_format(location)
            if fmt not in seen_formats:
                seen_formats.add(fmt)
                file_formats.append(fmt)

            # If local, resolve path relative to ISA-Tab dir
            if not is_remote:
                location = str(isa_dir / location)

            files.append(
                DataFile(
                    location=location,
                    format=fmt,
                    is_remote=is_remote,
                    sample_name=df.label if hasattr(df, "label") else "",
                )
            )

    # Extract sample metadata
    sample_metadata = {}
    for sample in study.samples:
        sample_metadata[sample.name] = {
            "characteristics": [
                {
                    "term": c.category.term,
                    "value": c.value.term if hasattr(c.value, "term") else str(c.value),
                }
                for c in sample.characteristics
            ]
        }

    return DataSet(
        name=study.title or inv.title or "unknown",
        description=study.description or "",
        files=files,
        file_formats=file_formats,
        organisms=organisms,
        assay_type=assay_type,
        sample_metadata=sample_metadata,
        isa_tab_dir=isa_dir,
    )
