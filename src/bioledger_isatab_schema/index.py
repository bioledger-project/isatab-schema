"""Generate a static JSON index from an ISA-Tab library directory.

Usage (CLI):
    python -m bioledger_isatab_schema.index /path/to/studies > index.json

The index is a JSON array where each entry contains the metadata needed for
discovery and search, without the full study data. The BioLedger core
``LibraryClient`` fetches this index from GitHub Pages to offer studies.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml


def build_index(studies_dir: Path) -> list[dict]:
    """Walk an ISA-Tab library ``studies/`` directory and extract index entries.

    Expected layout::

        studies/<accession>/manifest.yaml
        studies/<accession>/i_investigation.txt
        ...

    Returns a list of dicts, one per study, suitable for JSON serialisation.
    """
    entries: list[dict] = []

    for manifest_path in sorted(studies_dir.rglob("manifest.yaml")):
        study_dir = manifest_path.parent
        try:
            raw = yaml.safe_load(manifest_path.read_text())
        except Exception:
            continue

        accession = study_dir.name
        study_type = raw.get("study_type", "")
        organism = raw.get("organism", "")
        strain = raw.get("strain", "")
        assembly_accession = raw.get("assembly_accession", "")
        insdc_accession = raw.get("insdc_accession", "")

        files = raw.get("files") or []
        formats = sorted({f.get("format", "") for f in files if f.get("format")})
        file_count = len(files)

        # Collect ISA structural files (*.txt) for download
        isa_files = sorted(
            p.name for p in study_dir.glob("*.txt") if p.is_file()
        )

        # Try to extract title/description from i_investigation.txt
        title = ""
        description = ""
        inv_path = study_dir / "i_investigation.txt"
        if inv_path.exists():
            title, description = _parse_investigation_metadata(inv_path)

        entry = {
            "accession": accession,
            "study_type": study_type,
            "organism": organism,
            "strain": strain,
            "title": title,
            "description": description,
            "assembly_accession": assembly_accession,
            "insdc_accession": insdc_accession,
            "formats": formats,
            "file_count": file_count,
            "isa_files": isa_files,
            "path": str(study_dir.relative_to(studies_dir)),
        }
        entries.append(entry)

    return entries


def _parse_investigation_metadata(inv_path: Path) -> tuple[str, str]:
    """Extract Study Title and Study Description from i_investigation.txt.

    ISA-Tab investigation files are tab-delimited with section headers.
    """
    title = ""
    description = ""
    try:
        for line in inv_path.read_text(errors="replace").splitlines():
            if line.startswith("Study Title"):
                parts = line.split("\t", 1)
                title = parts[1].strip('" \t') if len(parts) > 1 else ""
            elif line.startswith("Study Description"):
                parts = line.split("\t", 1)
                description = parts[1].strip('" \t') if len(parts) > 1 else ""
    except Exception:
        pass
    return title, description


def write_index(studies_dir: Path, output_path: Path) -> None:
    """Build the index and write it to a JSON file."""
    entries = build_index(studies_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(entries, indent=2) + "\n")


def main() -> None:
    """CLI entry point: python -m bioledger_isatab_schema.index <studies_dir> [output.json]"""
    if len(sys.argv) < 2:
        print("Usage: python -m bioledger_isatab_schema.index <studies_dir> [output.json]")
        sys.exit(1)

    studies_dir = Path(sys.argv[1])
    if not studies_dir.is_dir():
        print(f"Error: {studies_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) >= 3:
        output_path = Path(sys.argv[2])
        write_index(studies_dir, output_path)
    else:
        # Write to stdout
        entries = build_index(studies_dir)
        print(json.dumps(entries, indent=2))


if __name__ == "__main__":
    main()
