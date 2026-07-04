"""Generate a Markdown API reference from the live ISA-Tab pydantic models.

Usage (CLI):
    python -m bioledger_isatab_schema.docgen > reference.md

This introspects the model classes via ``model_json_schema()`` rather than
hand-maintaining field tables, so the reference can never drift from
``models.py``/``manifest.py`` — regenerate any time (e.g. in CI) and it
reflects the current field set, types, requirements, defaults, and
descriptions.

The "Validation rules" section is likewise pulled straight from the
docstrings of ``validate_isatab`` and ``validate_manifest`` rather than
hand-duplicated, so it stays in sync with the actual checks.
"""

from __future__ import annotations

import inspect
from typing import Any

from .manifest import Manifest, ManifestFile, validate_manifest
from .models import CharacteristicSpec, ISAStudySpec, OntologySourceSpec, SampleSpec, SourceSpec
from .validate import validate_isatab

# Order matters: this is the order sections appear in the generated doc.
DOCUMENTED_MODELS = [
    ISAStudySpec,
    OntologySourceSpec,
    SourceSpec,
    SampleSpec,
    CharacteristicSpec,
    Manifest,
    ManifestFile,
]

_TYPE_NAMES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "null": "None",
}


def _resolve_ref(ref: str, defs: dict[str, Any]) -> tuple[dict[str, Any], str]:
    name = ref.rsplit("/", 1)[-1]
    return defs.get(name, {}), name


def _resolve_type(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    """Best-effort human-readable type string for a JSON-schema property."""
    if "$ref" in prop:
        resolved, name = _resolve_ref(prop["$ref"], defs)
        if "enum" in resolved:
            return " | ".join(f'"{v}"' for v in resolved["enum"])
        return name

    if "allOf" in prop and prop["allOf"]:
        return _resolve_type(prop["allOf"][0], defs)

    if "anyOf" in prop:
        options = prop["anyOf"]
        parts = [_resolve_type(o, defs) for o in options if o.get("type") != "null"]
        parts = [p for p in dict.fromkeys(parts) if p]  # dedupe, keep order
        if any(o.get("type") == "null" for o in options):
            parts.append("None")
        return " | ".join(parts) if parts else "any"

    t = prop.get("type")
    if t == "array":
        return f"list[{_resolve_type(prop.get('items', {}), defs)}]"
    if t == "object":
        additional = prop.get("additionalProperties")
        if isinstance(additional, dict):
            return f"dict[str, {_resolve_type(additional, defs)}]"
        return "dict"
    return _TYPE_NAMES.get(t, t) if t else "any"


def _format_default(prop: dict[str, Any]) -> str:
    if "default" not in prop:
        return "\u2014"
    default = prop["default"]
    if default in ("", None, [], {}):
        return "\u2014"
    return f"`{default!r}`" if isinstance(default, str) else f"`{default}`"


def _model_table(model: type) -> str:
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    props = schema.get("properties", {})

    lines = [f"## `{model.__name__}`", ""]
    doc = (model.__doc__ or "").strip()
    if doc:
        lines.append(doc)
        lines.append("")
    lines.append("| Field | Type | Required | Default | Description |")
    lines.append("|-------|------|----------|---------|-------------|")
    for name, prop in props.items():
        type_str = _resolve_type(prop, defs)
        is_required = "**yes**" if name in required else "no"
        default_str = _format_default(prop)
        description = (prop.get("description") or "").replace("\n", " ")
        lines.append(
            f"| `{name}` | `{type_str}` | {is_required} | {default_str} | {description} |"
        )
    lines.append("")
    return "\n".join(lines)


def _validation_rules_section() -> str:
    """Render the 'Validation rules' section straight from function docstrings."""
    lines = ["## Validation rules", ""]
    for fn in (validate_isatab, validate_manifest):
        doc = inspect.getdoc(fn) or ""
        lines.append(f"### `{fn.__name__}()`")
        lines.append("")
        lines.append(doc)
        lines.append("")
    return "\n".join(lines)


def generate_reference_markdown() -> str:
    """Render the full auto-generated API reference for all documented models."""
    parts = [
        "# BioLedger ISA-Tab \u2014 API Reference",
        "",
        "_Auto-generated from `bioledger_isatab_schema.models`/`manifest`/`validate` "
        "at build time \u2014 do not hand-edit._",
        "",
    ]
    parts.extend(_model_table(model) for model in DOCUMENTED_MODELS)
    parts.append(_validation_rules_section())
    return "\n".join(parts)


def main() -> None:
    print(generate_reference_markdown())


if __name__ == "__main__":
    main()
