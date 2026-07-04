# BioLedger ISA-Tab Guide

`bioledger-isatab-schema` is a thin but opinionated layer on top of [`isatools`](https://isa-tools.org/) that encodes the requirements BioLedger places on ISA-Tab data beyond what the upstream parser enforces. This guide explains the concepts; for the exact field list on every model, see the [API Reference](reference.html) (auto-generated from `models.py`/`manifest.py`, so it's always current).

---

## Why a separate repo

- ISA-Tab validation in BioLedger is **not** just a passthrough to `isatools`. `validate_isatab` enforces BioLedger-specific requirements: at least one assay with data files, an organism characteristic in sources, assay technology type set, etc. Asset repos must validate against *those* opinions, or their assets could pass `isatools.load` and still fail to load in BioLedger.
- A standalone package gives asset repos (like [`bioledger-isatab-library`](https://github.com/bioledger-project/isatab-library)) a way to validate studies without pulling in BioLedger's LLM/application stack.

## The manifest companion file

ISA-Tab does not natively support download URLs in data-file columns — the standard expects plain filenames relative to the dataset. To keep the ISA-Tab portable for any standard ISA tool, all download metadata lives in a companion `manifest.yaml` instead: it is the single source of truth for each file's `url` and checksum. A user can clone the study, download per the manifest into the study directory, and the ISA-Tab works unchanged. See `Manifest` / `ManifestFile` in the [API Reference](reference.html).

- At least one of `sha256` or `md5` is required per file and is verified after download.
- `md5` exists because large public FASTQ files at ENA/SRA are often published with md5 only.
- `sra_accession` optionally tags a file as originating from an SRA run; when the SRA-Toolkit fast-path is enabled, the downloader uses `prefetch` + `fasterq-dump` instead of HTTPS.

## Validation rules

BioLedger validates two things about a study: the ISA-Tab itself (`validate_isatab`) and, if present, the companion manifest (`validate_manifest`). Rather than duplicating the exact rule list here, the [API Reference](reference.html) renders it straight from each function's docstring, so it can't drift from the code.

## Target public API

```python
from bioledger_isatab_schema import (
    # validation
    validate_isatab, ISAValidationResult, ISAValidationIssue, Severity,
    # dataset model
    DataSet, DataFile, load_dataset_from_isatab, load_dataset_from_csv,
    # study spec (used by ISAForge agent for generation)
    ISAStudySpec, OntologySourceSpec, SourceSpec, SampleSpec, CharacteristicSpec,
    # manifest (download metadata)
    Manifest, ManifestFile, StudyType, load_manifest, validate_manifest,
    # remote files
    download_remote_files, download_manifest,
)
```

## Related repos

- **[`bioledger-isatab-library`](https://github.com/bioledger-project/isatab-library)** ([browse](https://bioledger-project.github.io/isatab-library/)) — curated ISA-Tab studies (instances of this schema). CI validates every committed study against `validate_isatab`/`validate_manifest`.
- **[`bioledger-toolspec-schema`](https://github.com/bioledger-project/toolspec-schema)** ([docs](https://bioledger-project.github.io/toolspec-schema/)) — sibling schema package for tool specs. Independent versioning.
- **[`bioledger`](https://github.com/d-callan/bioledger)** — the application that consumes both.
