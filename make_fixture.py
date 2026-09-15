"""Build a small multi-page PDF with text + multiple images for testing
``extract_pdf.py``.

Creates ``sample.pdf`` with:
  - Page 1: title text + one big red square image.
  - Page 2: a paragraph + two images (one green square, one blue square).
  - Page 3: text only.

Run:  python make_fixture.py
"""
from __future__ import annotations

from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

FIXTURE = Path(__file__).parent / "sample.pdf"


def _make_square(name: str, color: tuple[int, int, int], size: int = 256) -> Path:
    path = Path(__file__).parent / name
    img = Image.new("RGB", (size, size), color)
    draw = ImageDraw.Draw(img)
    # White border so the image is visually distinct.
    draw.rectangle([(0, 0), (size - 1, size - 1)], outline=(255, 255, 255), width=4)
    draw.text((size // 4, size // 2 - 8), name.rsplit(".", 1)[0], fill=(255, 255, 255))
    img.save(path)
    return path


def main() -> None:
    red = _make_square("red.png", (220, 50, 50))
    green = _make_square("green.png", (40, 170, 80))
    blue = _make_square("blue.png", (40, 80, 220))

    doc = pymupdf.open()
    try:
        # ---- Page 1 --------------------------------------------------------
        page = doc.new_page()
        page.insert_text(
            (72, 90),
            "PyMuPDF Extraction Sample",
            fontsize=22,
        )
        page.insert_text(
            (72, 130),
            "Page 1 — single image on this page.",
            fontsize=12,
        )
        page.insert_image(
            fitz_rect := pymupdf.Rect(72, 160, 72 + 220, 160 + 220),
            filename=str(red),
        )

        # ---- Page 2 --------------------------------------------------------
        page = doc.new_page()
        page.insert_text(
            (72, 80),
            "Page 2 — two images plus a paragraph of text.",
            fontsize=12,
        )
        page.insert_text(
            (72, 100),
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
            "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
            fontsize=11,
        )
        page.insert_image(
            pymupdf.Rect(72, 160, 72 + 180, 160 + 180), filename=str(green)
        )
        page.insert_image(
            pymupdf.Rect(300, 160, 300 + 180, 160 + 180), filename=str(blue)
        )

        # ---- Page 3 --------------------------------------------------------
        page = doc.new_page()
        page.insert_text(
            (72, 90),
            "Page 3 — text only, no images.",
            fontsize=14,
        )
        page.insert_text(
            (72, 120),
            "The extractor should record zero images on this page.",
            fontsize=11,
        )

        doc.save(FIXTURE.as_posix())
    finally:
        doc.close()

    print(f"Wrote fixture: {FIXTURE}")


if __name__ == "__main__":
    main()