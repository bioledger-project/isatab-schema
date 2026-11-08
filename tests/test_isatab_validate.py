"""Tests for validate_isatab — real fixture for the happy path, fabricated
isatools objects for edge cases."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from bioledger_isatab_schema.validate import (
    ISAValidationResult,
    Severity,
    validate_isatab,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "valid_isatab"


def _errors(result):
    return [i for i in result.issues if i.severity == Severity.ERROR]


def _warnings(result):
    return [i for i in result.issues if i.severity == Severity.WARNING]


def _char(term_category, value):
    return SimpleNamespace(
        category=SimpleNamespace(term=term_category),
        value=SimpleNamespace(term=value),
    )


def _source(chars):
    return SimpleNamespace(characteristics=chars)


def _assay(data_files=None, tech_term="nucleotide sequencing", filename="a.txt"):
    return SimpleNamespace(
        data_files=data_files or [],
        technology_type=SimpleNamespace(term=tech_term),
        filename=filename,
    )


def _study(**overrides):
    study = SimpleNamespace(
        title="T",
        description="D",
        assays=[_assay(data_files=[SimpleNamespace(filename="f.fq")])],
        sources=[_source([_char("Organism", "L. donovani")])],
        samples=[],
    )
    for k, v in overrides.items():
        setattr(study, k, v)
    return study


def _patch_load(monkeypatch, studies):
    import isatools.isatab

    monkeypatch.setattr(
        isatools.isatab, "load", lambda p: SimpleNamespace(studies=studies)
    )


@pytest.fixture
def inv_file(tmp_path):
    (tmp_path / "i_investigation.txt").write_text("placeholder")
    return tmp_path


class TestFileChecks:
    def test_missing_investigation_file(self, tmp_path):
        result = validate_isatab(tmp_path)
        assert not result.is_valid
        assert _errors(result)[0].field == "investigation_file"

    def test_parse_failure(self, inv_file, monkeypatch):
        import isatools.isatab

        def boom(path):
            raise RuntimeError("not isa-tab")

        monkeypatch.setattr(isatools.isatab, "load", boom)
        result = validate_isatab(inv_file)
        assert _errors(result)[0].field == "parse"


class TestStudyChecks:
    def test_no_studies(self, inv_file, monkeypatch):
        _patch_load(monkeypatch, [])
        result = validate_isatab(inv_file)
        assert any(i.field == "studies" for i in _errors(result))

    def test_empty_title_warns(self, inv_file, monkeypatch):
        _patch_load(monkeypatch, [_study(title="")])
        result = validate_isatab(inv_file)
        assert result.is_valid
        assert any(i.field == "study.title" for i in _warnings(result))

    def test_empty_description_is_info(self, inv_file, monkeypatch):
        _patch_load(monkeypatch, [_study(description="")])
        result = validate_isatab(inv_file)
        infos = [i for i in result.issues if i.severity == Severity.INFO]
        assert any(i.field == "study.description" for i in infos)

    def test_no_assays_is_error(self, inv_file, monkeypatch):
        _patch_load(monkeypatch, [_study(assays=[])])
        result = validate_isatab(inv_file)
        assert any(i.field == "assays" for i in _errors(result))

    def test_no_data_files_is_error(self, inv_file, monkeypatch):
        _patch_load(
            monkeypatch, [_study(assays=[_assay(data_files=[])])]
        )
        result = validate_isatab(inv_file)
        assert any(i.field == "data_files" for i in _errors(result))

    def test_missing_organism_warns(self, inv_file, monkeypatch):
        _patch_load(
            monkeypatch, [_study(sources=[_source([_char("Strain", "3D7")])])]
        )
        result = validate_isatab(inv_file)
        assert any(i.field == "organism" for i in _warnings(result))

    def test_organism_case_insensitive(self, inv_file, monkeypatch):
        _patch_load(
            monkeypatch,
            [_study(sources=[_source([_char("ORGANISM", "x")])])],
        )
        result = validate_isatab(inv_file)
        assert not any(i.field == "organism" for i in _warnings(result))

    def test_missing_technology_type_warns(self, inv_file, monkeypatch):
        _patch_load(
            monkeypatch,
            [_study(assays=[_assay(
                data_files=[SimpleNamespace(filename="f.fq")],
                tech_term=None,
            )])],
        )
        result = validate_isatab(inv_file)
        assert any(
            "technology_type" in i.field for i in _warnings(result)
        )


class TestRealFixture:
    def test_valid_fixture_is_clean(self):
        result = validate_isatab(FIXTURE_DIR)
        assert result.is_valid
        assert result.issues == []

    def test_result_properties(self):
        result = validate_isatab(FIXTURE_DIR)
        assert isinstance(result, ISAValidationResult)
