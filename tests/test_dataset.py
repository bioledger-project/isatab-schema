from pathlib import Path

import pytest

from bioledger_isatab_schema.dataset import (
    DataFile,
    DataSet,
    _infer_format,
    load_dataset_from_csv,
    load_dataset_from_isatab,
    parse_csv_samplesheet,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "valid_isatab"


class TestDataFile:
    def test_is_downloaded_requires_remote_and_path(self):
        local = DataFile(location="/x/f.bam")
        assert not local.is_downloaded
        remote = DataFile(location="https://x/f.bam", is_remote=True)
        assert not remote.is_downloaded
        remote.downloaded_path = "/tmp/f.bam"
        assert remote.is_downloaded


class TestDataSet:
    def _ds(self):
        return DataSet(
            name="d",
            files=[
                DataFile(location="/local/a.bam", format="bam"),
                DataFile(location="https://x/r.fq", format="fastq", is_remote=True),
                DataFile(
                    location="https://x/d.fq",
                    format="fastq",
                    is_remote=True,
                    downloaded_path="/tmp/d.fq",
                ),
            ],
        )

    def test_remote_files_excludes_downloaded(self):
        remote = self._ds().remote_files()
        assert len(remote) == 1
        assert remote[0].location == "https://x/r.fq"

    def test_local_files_includes_downloaded(self):
        local = self._ds().local_files()
        assert {f.location for f in local} == {"/local/a.bam", "https://x/d.fq"}

    def test_files_by_format(self):
        assert len(self._ds().files_by_format("fastq")) == 2
        assert self._ds().files_by_format("vcf") == []


class TestInferFormat:
    @pytest.mark.parametrize(
        "location,expected",
        [
            ("reads.fastq", "fastq"),
            ("reads.fq", "fastq"),
            ("reads.fastq.gz", "fastq"),
            ("ref.fasta.bz2", "fasta"),
            ("ref.fa", "fasta"),
            ("aln.bam", "bam"),
            ("aln.cram.xz", "cram"),
            ("calls.vcf.gz", "vcf"),
            ("ann.gff3", "gff"),
            ("track.bw", "bigwig"),
            ("data.txt", "tsv"),
            ("meta.json.zst", "json"),
            ("unknown.xyz", "xyz"),  # unrecognized ext passes through
            ("noext", "unknown"),
            ("https://ftp.x.org/SRR1.fastq.gz", "fastq"),
        ],
    )
    def test_format_inference(self, location, expected):
        assert _infer_format(location) == expected


def _write_csv(tmp_path, text):
    p = tmp_path / "samples.csv"
    p.write_text(text)
    return p


class TestParseCSV:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_csv_samplesheet(tmp_path / "nope.csv")

    def test_empty_file_raises(self, tmp_path):
        p = _write_csv(tmp_path, "")
        with pytest.raises(ValueError, match="empty"):
            parse_csv_samplesheet(p)

    def test_headers_only_raises(self, tmp_path):
        p = _write_csv(tmp_path, "sample_id,reads\n")
        with pytest.raises(ValueError, match="no data rows"):
            parse_csv_samplesheet(p)

    @pytest.mark.parametrize(
        "col", ["sample_id", "sample_name", "sample", "name", "id"]
    )
    def test_sample_column_candidates(self, tmp_path, col):
        p = _write_csv(tmp_path, f"{col},reads\nS1,r.fq\n")
        assert parse_csv_samplesheet(p).sample_col == col

    def test_sample_column_case_insensitive(self, tmp_path):
        p = _write_csv(tmp_path, "Sample_ID,reads\nS1,r.fq\n")
        assert parse_csv_samplesheet(p).sample_col == "Sample_ID"

    def test_organism_column_detected(self, tmp_path):
        p = _write_csv(tmp_path, "id,Organism\nS1,Leishmania\n")
        assert parse_csv_samplesheet(p).organism_col == "Organism"

    def test_file_columns_detected_by_extension(self, tmp_path):
        p = _write_csv(
            tmp_path,
            "id,reads,notes,assembly\n"
            "S1,r1.fastq.gz,hello,ref.fasta.gz\n"
            "S2,r2.fastq.gz,world,ref.fasta.gz\n",
        )
        parsed = parse_csv_samplesheet(p)
        assert set(parsed.file_columns) == {"reads", "assembly"}

    def test_unrecognized_extensions_not_file_columns(self, tmp_path):
        p = _write_csv(tmp_path, "id,comment\nS1,some.thing\n")
        assert parse_csv_samplesheet(p).file_columns == []

    def test_missing_sample_and_organism_cols(self, tmp_path):
        p = _write_csv(tmp_path, "a,b\n1,2\n")
        parsed = parse_csv_samplesheet(p)
        assert parsed.sample_col is None
        assert parsed.organism_col is None


class TestLoadDatasetFromCSV:
    def test_full_load(self, tmp_path):
        p = _write_csv(
            tmp_path,
            "sample_id,organism,reads\n"
            "S1,Leishmania donovani,s1.fastq.gz\n"
            "S2,Leishmania donovani,s2.fastq.gz\n"
            "S3,Plasmodium falciparum,s3.fastq.gz\n",
        )
        ds = load_dataset_from_csv(p)
        assert ds.name == "Samples"
        assert len(ds.organisms) == 2
        assert "csv" in ds.file_formats
        assert "fastq" in ds.file_formats
        # 3 data files + 1 self-reference to the CSV
        assert len(ds.files) == 4
        assert ds.files[0].location == str(p.resolve())
        assert ds.sample_metadata["S1"]["organism"] == "Leishmania donovani"

    def test_local_files_resolved_relative_to_csv(self, tmp_path):
        data = tmp_path / "s1.fastq.gz"
        data.write_bytes(b"fq")
        p = _write_csv(tmp_path, "sample,reads\nS1,s1.fastq.gz\n")
        ds = load_dataset_from_csv(p)
        fastq_files = [f for f in ds.files if f.format == "fastq"]
        assert fastq_files[0].location == str(data)
        assert not fastq_files[0].is_remote

    def test_missing_local_file_keeps_literal_location(self, tmp_path):
        p = _write_csv(tmp_path, "sample,reads\nS1,gone.fastq.gz\n")
        ds = load_dataset_from_csv(p)
        fastq_files = [f for f in ds.files if f.format == "fastq"]
        assert fastq_files[0].location == "gone.fastq.gz"

    def test_remote_urls_flagged(self, tmp_path):
        p = _write_csv(
            tmp_path,
            "sample,reads\nS1,https://ftp.example.org/r1.fastq.gz\n",
        )
        ds = load_dataset_from_csv(p)
        remote = [f for f in ds.files if f.is_remote]
        assert len(remote) == 1
        assert remote[0].sample_name == "S1"

    def test_rows_without_sample_col_get_row_ids(self, tmp_path):
        p = _write_csv(tmp_path, "org,reads\nL.,x.fastq\n")
        ds = load_dataset_from_csv(p)
        assert "row_0" in ds.sample_metadata

    def test_dataset_name_derived_from_filename(self, tmp_path):
        p = tmp_path / "my_cohort-samples.csv"
        p.write_text("sample_id,reads\nS1,x.fq\n")
        ds = load_dataset_from_csv(p)
        assert ds.name == "My Cohort Samples"


class TestLoadDatasetFromISATab:
    def test_valid_fixture_loads(self):
        ds = load_dataset_from_isatab(FIXTURE_DIR)
        assert ds.name == "Test ISA-Tab fixture study"
        assert ds.isa_tab_dir == FIXTURE_DIR.resolve()
        assert "Leishmania donovani" in ds.organisms
        assert ds.assay_type == "nucleotide sequencing"
        # 3 structural .txt files + 2 assay data files
        assert len(ds.files) == 5
        fastqs = ds.files_by_format("fastq")
        assert len(fastqs) == 2
        assert fastqs[0].location.startswith(str(FIXTURE_DIR.resolve()))

    def test_file_formats_deduplicated(self):
        ds = load_dataset_from_isatab(FIXTURE_DIR)
        assert ds.file_formats.count("fastq") == 1
        assert "tsv" in ds.file_formats  # the .txt ISA files

    def test_sample_metadata_extracted(self):
        ds = load_dataset_from_isatab(FIXTURE_DIR)
        assert "TEST000001-sample" in ds.sample_metadata
        chars = ds.sample_metadata["TEST000001-sample"]["characteristics"]
        assert isinstance(chars, list)

    def test_validate_false_skips_validation(self, tmp_path, monkeypatch):
        # Empty dir: validation would fail — patch isatools to isolate the flag.
        import types

        import isatools.isatab

        fake_inv = types.SimpleNamespace(studies=[], title="t")
        monkeypatch.setattr(isatools.isatab, "load", lambda p: fake_inv)
        with pytest.raises(ValueError, match="No study found"):
            load_dataset_from_isatab(tmp_path, validate=False)

    def test_invalid_isatab_raises_valueerror(self, tmp_path):
        with pytest.raises(ValueError, match="validation failed"):
            load_dataset_from_isatab(tmp_path)
