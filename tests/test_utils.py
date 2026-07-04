"""Tests for bioledger_isatab_schema.utils."""

from __future__ import annotations

import logging

from bioledger_isatab_schema.utils import warn_on_ambiguous_periods


class TestWarnOnAmbiguousPeriods:
    """warn_on_ambiguous_periods logs a WARNING when a basename contains
    periods that are not recognized compression extensions."""

    def test_warns_on_ncbi_accession_basename(self, caplog):
        """NCBI accessions like GCF_000227135.1_ASM...fna.gz trigger a warning."""
        with caplog.at_level(logging.WARNING):
            warn_on_ambiguous_periods(
                "GCF_000227135.1_ASM22713v2_genomic.fna.gz"
            )
        assert any(
            "may be misinterpreted as file extensions" in r.message
            for r in caplog.records
        )

    def test_warns_on_period_in_stem(self, caplog):
        """Any period remaining after stripping .gz triggers a warning."""
        with caplog.at_level(logging.WARNING):
            warn_on_ambiguous_periods("sample.R1.fastq.gz")
        assert any(
            "may be misinterpreted as file extensions" in r.message
            for r in caplog.records
        )

    def test_no_warning_for_clean_basename(self, caplog):
        """Basenames without extra periods do not trigger a warning."""
        with caplog.at_level(logging.WARNING):
            warn_on_ambiguous_periods("sample.fastq.gz")
            warn_on_ambiguous_periods("reference.fasta")
            warn_on_ambiguous_periods("already_clean.fna.gz")
        assert not caplog.records

    def test_no_warning_for_hidden_file(self, caplog):
        """Leading-dot hidden files (.hidden) should not warn."""
        with caplog.at_level(logging.WARNING):
            warn_on_ambiguous_periods(".hidden")
        assert not caplog.records

    def test_no_warning_for_plain_name(self, caplog):
        """Names with no extension at all should not warn."""
        with caplog.at_level(logging.WARNING):
            warn_on_ambiguous_periods("README")
        assert not caplog.records
