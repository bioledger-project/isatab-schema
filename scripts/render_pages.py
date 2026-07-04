#!/usr/bin/env python3
"""Render the isatab-schema docs site (Guide + auto-generated API Reference).

Usage:
    python scripts/render_pages.py --out _site

Reads:
    docs/GUIDE.md                                 -> _site/index.html
    bioledger_isatab_schema.docgen (live models)  -> _site/reference.html
    pages/style.css                               -> _site/style.css
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import markdown

REPO_ROOT = Path(__file__).resolve().parent.parent

NAV = """
<nav class="top">
  <a href="index.html">Guide</a>
  <a href="reference.html">API Reference</a>
  <a href="https://github.com/bioledger-project/isatab-schema">GitHub</a>
  <a href="https://github.com/bioledger-project/isatab-library">Study Library</a>
</nav>
""".strip()

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div class="page">
    {nav}
    {body}
  </div>
</body>
</html>
"""

MD_EXTENSIONS = ["tables", "fenced_code", "toc"]


def render_markdown(text: str) -> str:
    return markdown.markdown(text, extensions=MD_EXTENSIONS)


def wrap_page(title: str, body_html: str) -> str:
    return PAGE_TEMPLATE.format(title=title, nav=NAV, body=body_html)


def build(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    guide_md = (REPO_ROOT / "docs" / "GUIDE.md").read_text()
    guide_html = wrap_page("BioLedger ISA-Tab — Guide", render_markdown(guide_md))
    (out_dir / "index.html").write_text(guide_html)

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from bioledger_isatab_schema.docgen import generate_reference_markdown

    reference_md = generate_reference_markdown()
    reference_html = wrap_page("BioLedger ISA-Tab — API Reference", render_markdown(reference_md))
    (out_dir / "reference.html").write_text(reference_html)

    shutil.copy(REPO_ROOT / "pages" / "style.css", out_dir / "style.css")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="_site", help="Output directory")
    args = parser.parse_args()
    build(Path(args.out))
    print(f"Wrote site to {args.out}/")


if __name__ == "__main__":
    main()
