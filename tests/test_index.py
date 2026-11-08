import json

import pytest
import yaml

from bioledger_isatab_schema.index import (
    _parse_investigation_metadata,
    build_index,
    main,
    write_index,
)


def _write_study(studies_dir, accession, manifest_doc, inv_text=None,
                 isa_files=("s_study.txt", "a_assay.txt")):
    study_dir = studies_dir / accession
    study_dir.mkdir(parents=True)
    (study_dir / "manifest.yaml").write_text(yaml.dump(manifest_doc))
    for name in isa_files:
        (study_dir / name).write_text("x")
    if inv_text is not None:
        (study_dir / "i_investigation.txt").write_text(inv_text)
    return study_dir


_MANIFEST = {
    "study_type": "experimental_data",
    "insdc_accession": "PRJNA450813",
    "organism": "Leishmania donovani",
    "strain": "BPK282A1",
    "files": [
        {"filename": "r1.fastq.gz", "format": "fastq"},
        {"filename": "r2.fastq.gz", "format": "fastq"},
        {"filename": "ref.fna.gz", "format": "fasta"},
        {"filename": "nocheck.txt"},  # no format key
    ],
}

_INV = (
    "INVESTIGATION\n"
    'Study Title\t"Quoted Study Title"\n'
    "Study Description\tSome description here\n"
)


class TestBuildIndex:
    def test_entry_fields(self, tmp_path):
        _write_study(tmp_path, "PRJNA450813", _MANIFEST, inv_text=_INV)
        (e,) = build_index(tmp_path)
        assert e["accession"] == "PRJNA450813"
        assert e["study_type"] == "experimental_data"
        assert e["organism"] == "Leishmania donovani"
        assert e["strain"] == "BPK282A1"
        assert e["insdc_accession"] == "PRJNA450813"
        assert e["assembly_accession"] == ""
        assert e["formats"] == ["fasta", "fastq"]  # sorted, deduped
        assert e["file_count"] == 4
        assert e["isa_files"] == [
            "a_assay.txt",
            "i_investigation.txt",
            "s_study.txt",
        ]
        assert e["path"] == "PRJNA450813"
        assert e["title"] == "Quoted Study Title"
        assert e["description"] == "Some description here"

    def test_multiple_studies_sorted(self, tmp_path):
        _write_study(tmp_path, "PRJNA999", dict(_MANIFEST))
        _write_study(tmp_path, "GCF_000001.1", dict(_MANIFEST))
        accs = [e["accession"] for e in build_index(tmp_path)]
        assert accs == ["GCF_000001.1", "PRJNA999"]

    def test_unparseable_manifest_skipped(self, tmp_path):
        bad = tmp_path / "BADACC"
        bad.mkdir()
        (bad / "manifest.yaml").write_text("{bad: [yaml")
        _write_study(tmp_path, "GOODACC", _MANIFEST)
        entries = build_index(tmp_path)
        assert [e["accession"] for e in entries] == ["GOODACC"]

    def test_missing_investigation_leaves_blank_metadata(self, tmp_path):
        _write_study(tmp_path, "ACC", _MANIFEST, inv_text=None)
        (e,) = build_index(tmp_path)
        assert e["title"] == ""
        assert e["description"] == ""

    def test_empty_dir(self, tmp_path):
        assert build_index(tmp_path) == []


class TestParseInvestigationMetadata:
    def test_extracts_title_and_description(self, tmp_path):
        p = tmp_path / "i_investigation.txt"
        p.write_text(_INV)
        assert _parse_investigation_metadata(p) == (
            "Quoted Study Title",
            "Some description here",
        )

    def test_missing_fields_blank(self, tmp_path):
        p = tmp_path / "i_investigation.txt"
        p.write_text("ONTOLOGY SOURCE REFERENCE\nTerm Source Name\tx\n")
        assert _parse_investigation_metadata(p) == ("", "")

    def test_unreadable_file_returns_blank(self, tmp_path):
        p = tmp_path / "nonexistent" / "i_investigation.txt"
        assert _parse_investigation_metadata(p) == ("", "")


class TestWriteIndexAndMain:
    def test_write_index(self, tmp_path):
        _write_study(tmp_path / "studies", "ACC", _MANIFEST)
        out = tmp_path / "site" / "index.json"
        write_index(tmp_path / "studies", out)
        data = json.loads(out.read_text())
        assert data[0]["accession"] == "ACC"

    def test_main_no_args_exits(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["prog"])
        with pytest.raises(SystemExit):
            main()

    def test_main_bad_dir_exits(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["prog", "/no/dir"])
        with pytest.raises(SystemExit):
            main()
        assert "not a directory" in capsys.readouterr().err

    def test_main_stdout(self, tmp_path, monkeypatch, capsys):
        _write_study(tmp_path, "ACC", _MANIFEST)
        monkeypatch.setattr("sys.argv", ["prog", str(tmp_path)])
        main()
        assert json.loads(capsys.readouterr().out)[0]["accession"] == "ACC"

    def test_main_output_file(self, tmp_path, monkeypatch):
        _write_study(tmp_path, "ACC", _MANIFEST)
        out = tmp_path / "i.json"
        monkeypatch.setattr("sys.argv", ["prog", str(tmp_path), str(out)])
        main()
        assert out.exists()
