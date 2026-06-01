"""Companion ``manifest.yaml`` for BioLedger ISA-Tab studies.

ISA-Tab does not natively support download URLs in data-file columns — the
standard expects plain filenames relative to the dataset. To keep the ISA-Tab
portable for any standard ISA tool, all download metadata lives here instead:
the manifest is the single source of truth for each file's ``url`` and
(required) ``sha256``. A user can clone the study, download per the manifest
into the study directory, and the ISA-Tab works unchanged.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator, model_validator


class StudyType(str, Enum):
    REFERENCE_GENOME = "reference_genome"
    EXPERIMENTAL_DATA = "experimental_data"


class ManifestFile(BaseModel):
    """A single downloadable file in a study manifest.

    At least one checksum (``sha256`` or ``md5``) is required and is verified
    after download. ``md5`` exists because large public FASTQ at ENA/SRA are
    published with md5 only — computing sha256 would require downloading
    multi-GB files. Prefer ``sha256`` where the source provides it.
    """

    filename: str  # canonical local name; must match the ISA-Tab assay table entry
    url: str  # https preferred (the downloader is httpx-based; no FTP support)
    format: str
    sha256: str | None = None  # preferred checksum
    md5: str | None = None  # accepted when the source only publishes md5 (e.g. ENA FASTQ)

    @model_validator(mode="after")
    def _require_checksum(self) -> "ManifestFile":
        if not self.sha256 and not self.md5:
            raise ValueError(
                f"file {self.filename!r} must declare at least one of sha256 or md5"
            )
        return self


class Manifest(BaseModel):
    """Parsed ``manifest.yaml`` for a single study directory."""

    study_type: StudyType
    files: list[ManifestFile]
    # Reference genome fields
    assembly_accession: str | None = None
    # Experimental data fields
    insdc_accession: str | None = None
    # Common fields (human-readable; the ISA-Tab study table is canonical)
    organism: str | None = None
    strain: str | None = None

    @field_validator("study_type", mode="before")
    @classmethod
    def coerce_study_type(cls, v: Any) -> Any:
        if isinstance(v, str):
            return StudyType(v)
        return v

    @property
    def accession(self) -> str | None:
        """The accession appropriate to this study type (matches dir name)."""
        if self.study_type == StudyType.REFERENCE_GENOME:
            return self.assembly_accession
        return self.insdc_accession


def load_manifest(study_dir: Path) -> Manifest | None:
    """Read ``manifest.yaml`` from a study directory.

    Returns None if the file is absent. Raises pydantic ``ValidationError``
    if present but malformed.
    """
    path = study_dir / "manifest.yaml"
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text())
    return Manifest.model_validate(raw)


def validate_manifest(study_dir: Path) -> list[dict[str, str]]:
    """Validate ``manifest.yaml`` for a study directory.

    Returns a list of issue dicts ``{"severity": ..., "field": ..., "message": ...}``
    where ``severity`` is ``"error"`` or ``"warning"``. An empty list means the
    manifest is valid. Callers decide whether warnings should fail.

    Checks:
    - manifest.yaml exists and parses (errors)
    - study_type present and valid (error)
    - assembly_accession / insdc_accession present and matches directory name (error)
    - files list non-empty, each with filename/url/format/sha256 (error)
    - study_type vs accession prefix consistency (warning only)
    """
    issues: list[dict[str, str]] = []
    path = study_dir / "manifest.yaml"

    if not path.exists():
        return [
            {"severity": "error", "field": "manifest", "message": "manifest.yaml is required"}
        ]

    try:
        manifest = load_manifest(study_dir)
    except Exception as e:  # pydantic ValidationError or YAML error
        return [
            {
                "severity": "error",
                "field": "manifest",
                "message": f"Failed to parse manifest.yaml: {e}",
            }
        ]

    assert manifest is not None  # path.exists() checked above

    expected = study_dir.name

    # Accession must match directory name
    if manifest.study_type == StudyType.REFERENCE_GENOME:
        if not manifest.assembly_accession:
            issues.append(
                {
                    "severity": "error",
                    "field": "assembly_accession",
                    "message": "reference_genome study requires assembly_accession",
                }
            )
        elif manifest.assembly_accession != expected:
            issues.append(
                {
                    "severity": "error",
                    "field": "assembly_accession",
                    "message": (
                        f"assembly_accession ({manifest.assembly_accession}) must match "
                        f"directory name ({expected})"
                    ),
                }
            )
        # Prefix consistency (warning only)
        if manifest.assembly_accession and not manifest.assembly_accession.startswith(
            ("GCA_", "GCF_")
        ):
            issues.append(
                {
                    "severity": "warning",
                    "field": "assembly_accession",
                    "message": (
                        "reference_genome study has non-assembly accession: "
                        f"{manifest.assembly_accession}"
                    ),
                }
            )

    elif manifest.study_type == StudyType.EXPERIMENTAL_DATA:
        if not manifest.insdc_accession:
            issues.append(
                {
                    "severity": "error",
                    "field": "insdc_accession",
                    "message": "experimental_data study requires insdc_accession",
                }
            )
        elif manifest.insdc_accession != expected:
            issues.append(
                {
                    "severity": "error",
                    "field": "insdc_accession",
                    "message": (
                        f"insdc_accession ({manifest.insdc_accession}) must match "
                        f"directory name ({expected})"
                    ),
                }
            )
        # Prefix consistency (warning only)
        if manifest.insdc_accession and manifest.insdc_accession.startswith(("GCA_", "GCF_")):
            issues.append(
                {
                    "severity": "warning",
                    "field": "insdc_accession",
                    "message": (
                        "experimental_data study has assembly-like accession: "
                        f"{manifest.insdc_accession}"
                    ),
                }
            )

    # Files list non-empty (pydantic already enforces per-file required fields)
    if not manifest.files:
        issues.append(
            {"severity": "error", "field": "files", "message": "files list must be non-empty"}
        )

    return issues
