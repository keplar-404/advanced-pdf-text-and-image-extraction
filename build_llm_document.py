"""
build_llm_document.py — Assemble an LLM-friendly Markdown representation of a PDF.

Combines extracted text (with LaTeX math from extract_math.txt), inline image
links (from extract_images/manifest.json), inline Markdown tables (from tables/),
layout awareness (from layout.json), and structural anchors into a single,
clean, navigable Markdown file: `extract_llm.md`.

Usage:
    python build_llm_document.py <input.pdf> [options]

Options:
    --extracted-dir PATH  Directory containing outputs from extract_pdf.py
                          and math_ocr.py. Defaults to `<pdf_stem>_extracted`.
    --output-file PATH    Path to write the LLM Markdown document.
                          Defaults to `<extracted_dir>/extract_llm.md`.

Outputs:
    extract_llm.md        Complete LLM-friendly document
    llm_summary.json      Machine-readable document index and table of contents
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TEXT_FILENAME_MATH = "extract_math.txt"
TEXT_FILENAME_PLAIN = "extract.txt"
IMAGES_SUBDIR = "extract_images"
MANIFEST_FILENAME = "manifest.json"
TABLES_SUBDIR = "tables"
LAYOUT_FILENAME = "layout.json"
DRAWINGS_FILENAME = "drawings.json"
MATH_MANIFEST_FILENAME = "math_manifest.json"
OUTPUT_LLM_MD = "extract_llm.md"
OUTPUT_LLM_JSON = "llm_summary.json"

_PAGE_SEP_RE = re.compile(
    r"^={72}\nPAGE (\S+)\s+\(index (\d+), file page (\S+)\)\n={72}",
    re.MULTILINE,
)


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Warning: Could not parse {path}: {exc}", file=sys.stderr)
        return None


def _parse_pages_from_text(text: str) -> list[dict]:
    """Split extract_math.txt / extract.txt into per-page text blocks."""
    all_lines = text.splitlines(keepends=True)
    sep_matches = list(_PAGE_SEP_RE.finditer(text))
    if not sep_matches:
        # Single page or no separators
        return [{"page": 1, "page_label": "1", "content": text}]

    pages = []
    for i, m in enumerate(sep_matches):
        label = m.group(1)
        index = int(m.group(2))
        start = m.end()
        end = sep_matches[i + 1].start() if i + 1 < len(sep_matches) else len(text)
        page_text = text[start:end].strip()
        pages.append(
            {
                "page": index + 1,
                "page_label": label,
                "content": page_text,
            }
        )
    return pages


def build_llm_markdown(
    pdf_path: Path,
    extracted_dir: Path,
    output_md_path: Path,
    output_json_path: Path,
) -> int:
    if not extracted_dir.exists():
        print(f"ERROR: Extracted directory not found: {extracted_dir}", file=sys.stderr)
        return 1

    # Load text source: prefer extract_math.txt over extract.txt
    text_path = extracted_dir / TEXT_FILENAME_MATH
    if not text_path.exists():
        text_path = extracted_dir / TEXT_FILENAME_PLAIN
    if not text_path.exists():
        print(f"ERROR: No text file found in {extracted_dir}", file=sys.stderr)
        return 1

    text_content = text_path.read_text(encoding="utf-8")
    pages = _parse_pages_from_text(text_content)

    # Load manifests & metadata
    images_manifest = _load_json(extracted_dir / IMAGES_SUBDIR / MANIFEST_FILENAME) or []
    layout_data = _load_json(extracted_dir / LAYOUT_FILENAME) or []
    drawings_data = _load_json(extracted_dir / DRAWINGS_FILENAME) or []
    math_manifest = _load_json(extracted_dir / MATH_MANIFEST_FILENAME) or []

    # Map images by page
    images_by_page: dict[int, list[dict]] = {}
    for img in images_manifest:
        p = img.get("page", 1)
        images_by_page.setdefault(p, []).append(img)

    # Map tables by page
    tables_dir = extracted_dir / TABLES_SUBDIR
    tables_by_page: dict[int, list[dict]] = {}
    if tables_dir.exists():
        for table_file in sorted(tables_dir.glob("*.md")):
            # Filename pattern: page006_table01.md
            m = re.match(r"page(\d+)_table(\d+)\.md", table_file.name)
            if m:
                p_num = int(m.group(1))
                t_num = int(m.group(2))
                content = table_file.read_text(encoding="utf-8").strip()
                tables_by_page.setdefault(p_num, []).append(
                    {
                        "table_index": t_num,
                        "file": table_file.name,
                        "path": str(table_file.relative_to(extracted_dir)),
                        "content": content,
                    }
                )

    # Map layout by page
    layout_by_page: dict[int, dict] = {}
    for l_entry in layout_data:
        p = l_entry.get("page", 1)
        layout_by_page[p] = l_entry

    # Map math by page
    math_by_page: dict[int, list[dict]] = {}
    for m_entry in math_manifest:
        p = m_entry.get("page", 1)
        math_by_page.setdefault(p, []).append(m_entry)

    # Assemble Document
    doc_lines: list[str] = []
    doc_title = pdf_path.stem.replace("_", " ").title()

    # Document Header
    doc_lines.append(f"# {doc_title}")
    doc_lines.append("")
    doc_lines.append(
        f"> **Document Metadata**  \n"
        f"> - **File**: `{pdf_path.name}`  \n"
        f"> - **Total Pages**: {len(pages)}  \n"
        f"> - **Extracted Media**: {len(images_manifest)} Images/Figures, {sum(len(t) for t in tables_by_page.values())} Tables, {len(math_manifest)} Equations  \n"
        f"> - **Math OCR**: {'Enabled (LaTeX formatted)' if text_path.name == TEXT_FILENAME_MATH else 'Standard'}"
    )
    doc_lines.append("")

    # Document Table of Contents / Structural Overview
    doc_lines.append("## Table of Contents & Structure Overview")
    doc_lines.append("")
    for p_info in pages:
        p_num = p_info["page"]
        p_images = images_by_page.get(p_num, [])
        p_tables = tables_by_page.get(p_num, [])
        p_math = math_by_page.get(p_num, [])
        p_lay = layout_by_page.get(p_num, {})
        col_count = p_lay.get("column_count", 1)

        summary_parts = []
        if col_count > 1:
            summary_parts.append(f"{col_count} columns")
        if p_images:
            summary_parts.append(f"{len(p_images)} image(s)")
        if p_tables:
            summary_parts.append(f"{len(p_tables)} table(s)")
        if p_math:
            summary_parts.append(f"{len(p_math)} equation(s)")

        summary_str = f" ({', '.join(summary_parts)})" if summary_parts else ""
        doc_lines.append(f"- [Page {p_num}](#page-{p_num}){summary_str}")

    doc_lines.append("")
    doc_lines.append("---")
    doc_lines.append("")

    # Page-by-Page Content
    summary_toc = []

    for p_info in pages:
        p_num = p_info["page"]
        p_label = p_info["page_label"]
        p_content = p_info["content"]

        p_images = images_by_page.get(p_num, [])
        p_tables = tables_by_page.get(p_num, [])
        p_math = math_by_page.get(p_num, [])
        p_lay = layout_by_page.get(p_num, {})
        col_count = p_lay.get("column_count", 1)

        doc_lines.append(f"## Page {p_num}")
        doc_lines.append("")

        # Page metadata callout
        meta_items = []
        if col_count > 1:
            meta_items.append(f"**Layout**: {col_count}-Column Grid")
        else:
            meta_items.append("**Layout**: Single Column")
        if p_images:
            meta_items.append(f"**Figures/Images**: {len(p_images)}")
        if p_tables:
            meta_items.append(f"**Tables**: {len(p_tables)}")
        if p_math:
            meta_items.append(f"**Math Equations**: {len(p_math)}")

        doc_lines.append(f"> {' | '.join(meta_items)}")
        doc_lines.append("")

        # Insert Page Images / Figures if any exist
        if p_images:
            doc_lines.append("### 🖼️ Extracted Figures & Images")
            doc_lines.append("")
            for idx, img in enumerate(p_images, start=1):
                img_path = img.get("path", "")
                # Normalize windows slashes for markdown compatibility
                img_rel_url = img_path.replace("\\", "/") if img_path else ""
                caption_info = img.get("nearby_caption")
                caption_text = caption_info.get("snippet") if caption_info else f"Image {idx} on Page {p_num}"
                kind = img.get("kind", "raster")

                doc_lines.append(f"#### [{kind.upper()}] {caption_text}")
                doc_lines.append(f"![{caption_text}]({img_rel_url})")
                doc_lines.append(f"*File*: `{img_rel_url}` | *Dimensions*: {img.get('width', 0)}×{img.get('height', 0)}px")
                doc_lines.append("")

        # Insert Page Tables if any exist
        if p_tables:
            doc_lines.append("### 📊 Extracted Tables")
            doc_lines.append("")
            for tbl in p_tables:
                doc_lines.append(f"#### Table {tbl['table_index']} (Page {p_num})")
                doc_lines.append(tbl["content"])
                doc_lines.append("")

        # Insert Main Body Text
        doc_lines.append("### 📝 Body Text & Formulas")
        doc_lines.append("")
        doc_lines.append(p_content)
        doc_lines.append("")
        doc_lines.append("---")
        doc_lines.append("")

        summary_toc.append(
            {
                "page": p_num,
                "label": p_label,
                "columns": col_count,
                "image_count": len(p_images),
                "table_count": len(p_tables),
                "equation_count": len(p_math),
            }
        )

    # Write output Markdown
    output_md_path.write_text("\n".join(doc_lines), encoding="utf-8")

    # Write summary JSON index for downstream API / RAG indexing
    summary_json = {
        "title": doc_title,
        "pdf_file": pdf_path.name,
        "output_md": output_md_path.name,
        "total_pages": len(pages),
        "total_images": len(images_manifest),
        "total_tables": sum(len(t) for t in tables_by_page.values()),
        "total_equations": len(math_manifest),
        "pages": summary_toc,
    }
    output_json_path.write_text(json.dumps(summary_json, indent=2), encoding="utf-8")

    print(f"\n{'=' * 72}")
    print("LLM DOCUMENT EXPORT COMPLETE")
    print(f"{'=' * 72}")
    print(f"Markdown Document : {output_md_path}")
    print(f"Summary Index     : {output_json_path}")
    print(f"Total Pages       : {len(pages)}")
    print(f"Extracted Media   : {len(images_manifest)} Images, {summary_json['total_tables']} Tables, {len(math_manifest)} Equations")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble an LLM-friendly Markdown document from extracted PDF data.",
    )
    parser.add_argument("pdf", type=Path, help="Path to input PDF file.")
    parser.add_argument(
        "--extracted-dir",
        type=Path,
        default=None,
        help="Directory containing extracted outputs. Defaults to '<pdf_stem>_extracted'.",
    )
    parser.add_argument(
        "--output-file",
        type=Path,
        default=None,
        help="Target Markdown file path. Defaults to '<extracted_dir>/extract_llm.md'.",
    )

    args = parser.parse_args(argv)

    extracted_dir = args.extracted_dir or (args.pdf.parent / f"{args.pdf.stem}_extracted")
    output_md = args.output_file or (extracted_dir / OUTPUT_LLM_MD)
    output_json = extracted_dir / OUTPUT_LLM_JSON

    return build_llm_markdown(args.pdf, extracted_dir, output_md, output_json)


if __name__ == "__main__":
    raise SystemExit(main())

