# bioledger-isatab-schema

Pydantic models, validator, manifest layer, and download engine for
**BioLedger-flavored ISA-Tab** assets.

A thin but opinionated layer on top of [`isatools`](https://isa-tools.org/)
that encodes the requirements BioLedger places on ISA-Tab data beyond what
the upstream parser enforces.

## Status

**Extracted and in use (v0.1.0).** The schema/validation code lives here;
in `bioledger` the corresponding `bioledger.forges.isaforge` modules are
re-export shims. The LLM-using siblings (`builder.py`, `agent.py`) stay in
`bioledger` since they depend on the application's LLM/config layer.

## Why a separate repo

- ISA-Tab validation in BioLedger is **not** just a passthrough to
  `isatools`. `validate_isatab` enforces BioLedger-specific requirements:
  at least one assay with data files, organism characteristic in sources,
  assay technology type set, etc. Asset repos must validate against *those*
  opinions, or their assets could pass `isatools.load` and still fail to
  load in BioLedger.
- The validation/dataset/models/manifest/download files have no
  `from bioledger.*` imports, so they can be lifted out cleanly.

## Public API

```python
from bioledger_isatab_schema import (
    # validation
    validate_isatab, ISAValidationResult, ISAValidationIssue, Severity,
    # dataset model
    DataSet, DataFile,
    load_dataset_from_isatab, load_dataset_from_csv,
    parse_csv_samplesheet, ParsedCSV,
    # study spec (used by ISAForge agent for generation)
    ISAStudySpec, OntologySourceSpec, SourceSpec, SampleSpec,
    CharacteristicSpec,
    # remote files
    download_remote_files,
    # manifest layer
    Manifest, ManifestFile, StudyType,
    load_manifest, validate_manifest,
    download_manifest, manifest_to_datafiles,
    # study index
    build_study_index, write_study_index,
)
```

### Download engine

`download_manifest()` resolves a study's `manifest.yaml` — hashes
(sha256 and/or md5), sizes, and transport hints for every remote file —
and `download_remote_files()` fetches them: segmented HTTPS downloads via
`httpx`, optional `aria2c` for large files, and SRA accessions (via
`fasterq-dump` / `prefetch`) when the file's location is an SRA identifier.
Downloads verify checksums and sizes and resume partial files where the
server supports range requests.

### Study index CLI

```bash
python -m bioledger_isatab_schema.index <studies_dir> [output.json]
```

Walks a library directory, builds an index entry for each study, and writes
the JSON consumed by `bioledger study list`/`download` (stdout if no output
path is given).

## Docs

Full guide + an auto-generated field/validation-rule reference (regenerated from `models.py`/`manifest.py`/`validate.py` on every push) are published at <https://bioledger-project.github.io/isatab-schema/>. See [`docs/GUIDE.md`](docs/GUIDE.md) for the source.

## Consumers

| Consumer | Why it depends on this |
|---|---|
| `bioledger` | Loads & validates ISA-Tab at runtime. |
| `bioledger-isatab-library` | CI validates every committed study + manifest. |

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v          # 158 tests, ~97% coverage
```

## Related repos

- **[`bioledger-isatab-library`](../bioledger-isatab-library)** — curated
  ISA-Tab studies (instances of this schema).
- **[`bioledger-toolspec-schema`](../bioledger-toolspec-schema)** — sibling
  schema package for tool specs. Independent versioning.
- **[`bioledger`](../bioledger)** — the application that consumes both.
