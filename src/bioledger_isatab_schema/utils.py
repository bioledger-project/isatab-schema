"""Shared utilities for BioLedger ISA-Tab schema."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Known file extensions (compression + bioinformatics formats).
# Stripped iteratively before checking for ambiguous periods.
_KNOWN_EXTS = {
    # compression
    ".gz", ".bz2", ".xz", ".zst",
    # bioinformatics
    ".fastq", ".fq", ".fasta", ".fa", ".fna",
    ".bam", ".sam", ".cram", ".vcf", ".bcf",
    ".bed", ".gff", ".gtf", ".gff3",
    ".bw", ".bigwig", ".h5ad",
    ".csv", ".tsv", ".txt", ".json",
    ".dict", ".fai",
    # general
    ".html", ".png", ".pdf", ".md",
}


def stem_after_compression(name: str) -> str:
    """Return the filename after stripping known suffixes iteratively."""
    stem = name
    while True:
        root, ext = os.path.splitext(stem)
        if ext.lower() not in _KNOWN_EXTS or not root:
            break
        stem = root
    return stem


def warn_on_ambiguous_periods(name: str) -> None:
    """Log a warning when a filename contains periods that are not file extensions.

    Periods inside the basename (outside recognized extensions) are
    misinterpreted by ``os.path.splitext`` and break GATK sidecar naming
    (``.dict``, ``.fai``, etc.).  This warns the user so they can rename the
    source before ingestion.
    """
    basename = Path(name).name
    stem = stem_after_compression(basename)
    # Hidden files (leading dot) are not ambiguous — skip them.
    if stem.startswith("."):
        stem = stem.lstrip(".")
    if "." in stem:
        logger.warning(
            'Filename "%s" contains periods that may be misinterpreted as '
            "file extensions. This can break sidecar/index naming "
            "(e.g. GATK .dict, .fai). Consider renaming the source file.",
            basename,
        )
