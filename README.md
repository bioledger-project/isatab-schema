# bioledger-isatab-schema

Pydantic models and validator for **BioLedger-flavored ISA-Tab** assets.

A thin but opinionated layer on top of [`isatools`](https://isa-tools.org/)
that encodes the requirements BioLedger places on ISA-Tab data beyond what
the upstream parser enforces.

## Status

**Pre-alpha — not yet extracted.** The code currently lives at
`bioledger/src/bioledger/forges/isaforge/` (specifically `validate.py`,
`dataset.py`, `models.py`, and `download.py`). This repo exists to receive
the *pure schema/validation* portion so that
[`bioledger-isatab-library`](../bioledger-isatab-library) and any future
consumer can validate assets without pulling in BioLedger's LLM stack.

## Why a separate repo

- ISA-Tab validation in BioLedger is **not** just a passthrough to
  `isatools`. `bioledger.forges.isaforge.validate.validate_isatab` enforces
  bioledger-specific requirements: at least one assay with data files,
  organism characteristic in sources, assay technology type set, etc.
  Asset repos must validate against *those* opinions, or their assets
  could pass `isatools.load` and still fail to load in BioLedger.
- The validation/dataset/models files have no `from bioledger.*` imports,
  so they can be lifted out cleanly.
- The LLM-using siblings (`builder.py`, `agent.py`) stay in `bioledger`
  since they depend on the application's LLM/config layer.

## Migration plan (out of bioledger)

1. Move these files from `bioledger/src/bioledger/forges/isaforge/` into
   `src/bioledger_isatab_schema/` here:
   - `validate.py` — `validate_isatab`, `ISAValidationResult`, `Severity`
   - `dataset.py` — `DataSet`, `DataFile`, `load_dataset_from_isatab`,
     `load_dataset_from_csv`, format inference helpers
   - `models.py` — `ISAStudySpec`, `OntologySourceSpec`, `SourceSpec`,
     `SampleSpec`, `CharacteristicSpec`
   - `download.py` — `download_remote_files` (uses only `httpx` + `rich`;
     debatable whether this belongs here or in `bioledger`; revisit)
2. Add `pyproject.toml` with deps: `pydantic`, `isatools`, `httpx`. Target
   Python matches `bioledger`'s minimum.
3. In `bioledger`, replace the moved modules with re-export shims so
   existing callsites in `builder.py`, `agent.py`, the analysisforge, etc.
   continue to work unchanged.
4. Wire local development with editable installs:
   ```bash
   pip install -e ../bioledger-isatab-schema
   ```
5. Once stable, publish to PyPI as `bioledger-isatab-schema` and have
   consumers switch to versioned deps.

## Target public API

```python
from bioledger_isatab_schema import (
    # validation
    validate_isatab, ISAValidationResult, ISAValidationIssue, Severity,
    # dataset model
    DataSet, DataFile, load_dataset_from_isatab, load_dataset_from_csv,
    # study spec (used by ISAForge agent for generation)
    ISAStudySpec, OntologySourceSpec, SourceSpec, SampleSpec,
    CharacteristicSpec,
    # remote files
    download_remote_files,
)
```

## Validation contract (what BioLedger requires)

Beyond plain `isatools.load` succeeding, an asset is BioLedger-valid when:

- `i_investigation.txt` exists and parses.
- The investigation has at least one study with a non-empty title.
- The study has at least one assay.
- At least one assay declares data files.
- Sources include an organism characteristic (warning if absent).
- Each assay declares a `technology_type` (warning if absent).

See `validate.py` once moved here for the authoritative list.

## Consumers

| Consumer | Why it depends on this |
|---|---|
| `bioledger` | Loads & validates ISA-Tab at runtime. |
| `bioledger-isatab-library` | CI validates every committed study. |

## Related repos

- **[`bioledger-isatab-library`](../bioledger-isatab-library)** — curated
  ISA-Tab studies (instances of this schema).
- **[`bioledger-toolspec-schema`](../bioledger-toolspec-schema)** — sibling
  schema package for tool specs. Independent versioning.
- **[`bioledger`](../bioledger)** — the application that consumes both.
