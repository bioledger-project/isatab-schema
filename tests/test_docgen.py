from bioledger_isatab_schema.docgen import DOCUMENTED_MODELS, generate_reference_markdown


def test_generate_reference_markdown_includes_all_documented_models():
    text = generate_reference_markdown()
    for model in DOCUMENTED_MODELS:
        assert f"`{model.__name__}`" in text


def test_generate_reference_markdown_includes_known_fields():
    text = generate_reference_markdown()
    for field in ["investigation_id", "characteristics", "sha256", "assembly_accession"]:
        assert f"`{field}`" in text


def test_generate_reference_markdown_includes_validation_rules():
    text = generate_reference_markdown()
    assert "Validation rules" in text
    assert "validate_isatab" in text
    assert "validate_manifest" in text
