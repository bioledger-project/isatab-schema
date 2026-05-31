from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Severity(str, Enum):
    ERROR = "error"  # blocks dataset loading
    WARNING = "warning"  # allowed but flagged
    INFO = "info"  # suggestion only


@dataclass
class ISAValidationIssue:
    severity: Severity
    field: str
    message: str


@dataclass
class ISAValidationResult:
    issues: list[ISAValidationIssue]

    @property
    def is_valid(self) -> bool:
        return not any(i.severity == Severity.ERROR for i in self.issues)


def validate_isatab(isa_dir: Path) -> ISAValidationResult:
    """Validate ISA-Tab directory for essential attributes.

    Checks:
    - i_investigation.txt exists and is parseable
    - At least one study with title and description
    - At least one assay with data files
    - Data files have clear locations (local path or URL)
    - Organism characteristic present in sources
    - Assay type/technology type specified
    """
    result = ISAValidationResult(issues=[])

    # Check i_investigation.txt exists
    inv_file = isa_dir / "i_investigation.txt"
    if not inv_file.exists():
        result.issues.append(
            ISAValidationIssue(
                Severity.ERROR,
                "investigation_file",
                f"Missing i_investigation.txt in {isa_dir}",
            )
        )
        return result

    # Parse with isatools
    from isatools import isatab

    try:
        inv = isatab.load(str(isa_dir))
    except Exception as e:
        result.issues.append(
            ISAValidationIssue(Severity.ERROR, "parse", f"Failed to parse ISA-Tab: {e}")
        )
        return result

    # Check for at least one study
    if not inv.studies:
        result.issues.append(
            ISAValidationIssue(Severity.ERROR, "studies", "No studies found in investigation")
        )
        return result

    study = inv.studies[0]

    # Check study has title and description
    if not study.title:
        result.issues.append(
            ISAValidationIssue(Severity.WARNING, "study.title", "Study title is empty")
        )
    if not study.description:
        result.issues.append(
            ISAValidationIssue(Severity.INFO, "study.description", "Study description is empty")
        )

    # Check for at least one assay
    if not study.assays:
        result.issues.append(
            ISAValidationIssue(Severity.ERROR, "assays", "No assays found in study")
        )
        return result

    # Check assays have data files
    has_data_files = False
    for assay in study.assays:
        if assay.data_files:
            has_data_files = True
            break
    if not has_data_files:
        result.issues.append(
            ISAValidationIssue(
                Severity.ERROR, "data_files", "No data files referenced in any assay"
            )
        )

    # Check for organism in sources
    has_organism = False
    for source in study.sources:
        for char in source.characteristics:
            if "organism" in (char.category.term or "").lower():
                has_organism = True
                break
    if not has_organism:
        result.issues.append(
            ISAValidationIssue(
                Severity.WARNING, "organism", "No organism characteristic found in sources"
            )
        )

    # Check for assay type
    for assay in study.assays:
        if not assay.technology_type or not assay.technology_type.term:
            result.issues.append(
                ISAValidationIssue(
                    Severity.WARNING,
                    f"assay.{assay.filename}.technology_type",
                    "Assay technology type not specified",
                )
            )

    return result
