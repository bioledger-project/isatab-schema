import pytest
import yaml
from pydantic import ValidationError

from bioledger_isatab_schema.manifest import (
    Manifest,
    ManifestFile,
    StudyType,
    load_manifest,
    validate_manifest,
)


def _file_dict(**overrides):
    d = {
        "filename": "reads_1.fastq.gz",
        "url": "https://example.org/reads_1.fastq.gz",
        "format": "fastq",
        "md5": "d41d8cd98f00b204e9800998ecf8427e",
    }
    d.update(overrides)
    return d


def _manifest_dict(study_type="reference_genome", **overrides):
    d = {
        "study_type": study_type,
        "assembly_accession": "GCF_000001.1",
        "organism": "Leishmania donovani",
        "files": [_file_dict()],
    }
    d.update(overrides)
    return d


def _write_manifest(study_dir, doc):
    study_dir.mkdir(parents=True, exist_ok=True)
    (study_dir / "manifest.yaml").write_text(yaml.dump(doc))


class TestManifestFile:
    def test_sha256_only_accepted(self):
        f = ManifestFile(
            filename="x",
            url="https://x",
            format="fasta",
            sha256="a" * 64,
        )
        assert f.md5 is None

    def test_md5_only_accepted(self):
        f = ManifestFile(**_file_dict())
        assert f.sha256 is None

    def test_both_checksums_accepted(self):
        f = ManifestFile(**_file_dict(sha256="a" * 64))
        assert f.sha256 and f.md5

    def test_no_checksum_rejected(self):
        with pytest.raises(ValidationError, match="sha256 or md5"):
            ManifestFile(
                filename="x", url="https://x", format="fasta"
            )

    def test_sra_accession_optional(self):
        f = ManifestFile(**_file_dict(sra_accession="SRR123456"))
        assert f.sra_accession == "SRR123456"


class TestManifest:
    def test_accession_property_reference_genome(self):
        m = Manifest.model_validate(_manifest_dict())
        assert m.accession == "GCF_000001.1"

    def test_accession_property_experimental(self):
        m = Manifest.model_validate(
            _manifest_dict(
                study_type="experimental_data",
                insdc_accession="PRJNA123",
            )
        )
        assert m.accession == "PRJNA123"

    def test_study_type_coerced_from_string(self):
        m = Manifest.model_validate(_manifest_dict(study_type="experimental_data"))
        assert m.study_type is StudyType.EXPERIMENTAL_DATA

    def test_invalid_study_type_rejected(self):
        with pytest.raises(ValidationError):
            Manifest.model_validate(_manifest_dict(study_type="rna_seq"))


class TestLoadManifest:
    def test_missing_returns_none(self, tmp_path):
        assert load_manifest(tmp_path) is None

    def test_valid_manifest_loads(self, tmp_path):
        _write_manifest(tmp_path, _manifest_dict())
        m = load_manifest(tmp_path)
        assert m.study_type is StudyType.REFERENCE_GENOME
        assert m.files[0].filename == "reads_1.fastq.gz"

    def test_malformed_raises(self, tmp_path):
        _write_manifest(tmp_path, {"files": [{"filename": "x"}]})
        with pytest.raises(ValidationError):
            load_manifest(tmp_path)


class TestValidateManifest:
    def _errors(self, issues):
        return [i for i in issues if i["severity"] == "error"]

    def _warnings(self, issues):
        return [i for i in issues if i["severity"] == "warning"]

    def test_missing_manifest_is_error(self, tmp_path):
        issues = validate_manifest(tmp_path)
        assert self._errors(issues)[0]["field"] == "manifest"

    def test_unparseable_manifest_is_error(self, tmp_path):
        (tmp_path / "manifest.yaml").write_text("{bad: [yaml")
        issues = validate_manifest(tmp_path)
        assert "Failed to parse" in self._errors(issues)[0]["message"]

    def test_schema_invalid_manifest_is_error(self, tmp_path):
        _write_manifest(tmp_path, {"files": [{"filename": "x"}]})
        issues = validate_manifest(tmp_path)
        assert "Failed to parse" in self._errors(issues)[0]["message"]

    def test_valid_reference_genome(self, tmp_path):
        study_dir = tmp_path / "GCF_000001.1"
        _write_manifest(study_dir, _manifest_dict())
        assert validate_manifest(study_dir) == []

    def test_reference_genome_missing_accession(self, tmp_path):
        study_dir = tmp_path / "GCF_000001.1"
        _write_manifest(study_dir, _manifest_dict(assembly_accession=None))
        issues = validate_manifest(study_dir)
        assert any(
            i["field"] == "assembly_accession" for i in self._errors(issues)
        )

    def test_accession_must_match_dirname(self, tmp_path):
        study_dir = tmp_path / "GCF_999999.9"
        _write_manifest(study_dir, _manifest_dict())
        issues = validate_manifest(study_dir)
        assert any(
            "must match" in i["message"] for i in self._errors(issues)
        )

    def test_non_assembly_accession_prefix_warns(self, tmp_path):
        study_dir = tmp_path / "PRJNA123"
        doc = _manifest_dict(assembly_accession="PRJNA123")
        _write_manifest(study_dir, doc)
        issues = validate_manifest(study_dir)
        assert not self._errors(issues)
        assert any(
            "non-assembly accession" in i["message"]
            for i in self._warnings(issues)
        )

    def test_experimental_missing_insdc(self, tmp_path):
        study_dir = tmp_path / "PRJNA123"
        _write_manifest(
            study_dir,
            _manifest_dict(
                study_type="experimental_data", assembly_accession=None
            ),
        )
        issues = validate_manifest(study_dir)
        assert any(
            i["field"] == "insdc_accession" for i in self._errors(issues)
        )

    def test_experimental_assembly_like_accession_warns(self, tmp_path):
        study_dir = tmp_path / "GCF_000001.1"
        _write_manifest(
            study_dir,
            _manifest_dict(
                study_type="experimental_data",
                assembly_accession=None,
                insdc_accession="GCF_000001.1",
            ),
        )
        issues = validate_manifest(study_dir)
        assert not self._errors(issues)
        assert any(
            "assembly-like accession" in i["message"]
            for i in self._warnings(issues)
        )

    def test_empty_files_list_is_error(self, tmp_path):
        study_dir = tmp_path / "GCF_000001.1"
        _write_manifest(study_dir, _manifest_dict(files=[]))
        issues = validate_manifest(study_dir)
        assert any(i["field"] == "files" for i in self._errors(issues))
