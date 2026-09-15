"""Build `overlay.pdf`, a 4-page fixture that exercises the three overlay
cases discussed in the README:

  Page 1 — image with TEXT DRAWN ON TOP of it (a label and a callout).
           Both the image and the text are real PDF objects, so both
           survive extraction.

  Page 2 — image with TEXT DRAWN BESIDE it (caption on the right).
           Both the image and the text are real PDF objects.

  Page 3 — image with TEXT BURNED INTO THE IMAGE PIXELS (a chart whose
           axis labels are part of the PNG itself, not separate text).
           The image survives; the text is invisible to text extraction.

  Page 4 — text-only page (control).
"""
from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
FIXTURE = HERE / "overlay.pdf"


def main() -> None:
    chart_path = HERE / "chart.png"
    if not chart_path.exists():
        # Build the chart on demand so this script works standalone.
        from make_chart import make_chart
        make_chart()

    doc = pymupdf.open()
    try:
        # ---- Page 1: image with text drawn ON TOP ----
        page = doc.new_page()
        page.insert_text(
            (72, 60), "Case 1 — text drawn ON TOP of an image", fontsize=14
        )
        page.insert_image(
            pymupdf.Rect(72, 90, 72 + 320, 90 + 200), filename=str(chart_path)
        )
        # Callout arrow pointing at the tallest bar, with a text note over it.
        page.draw_rect(
            (220, 200, 360, 220), color=(1, 1, 1), fill=(1, 1, 0), width=1
        )
        page.insert_text(
            (224, 215), "peak: 200 units", fontsize=10, color=(0, 0, 0)
        )

        # ---- Page 2: image with text drawn BESIDE it ----
        page = doc.new_page()
        page.insert_text(
            (72, 60), "Case 2 — text drawn BESIDE an image", fontsize=14
        )
        page.insert_image(
            pymupdf.Rect(72, 90, 72 + 280, 90 + 180), filename=str(chart_path)
        )
        page.insert_textbox(
            pymupdf.Rect(370, 90, 540, 290),
            (
                "Figure A: This caption is a real text object in the PDF, "
                "sitting to the right of the chart image. Both the image "
                "and this text survive PyMuPDF extraction. "
                "If you only read the text and only look at the image "
                "independently, you lose the relationship between them. "
                "The manifest's bbox coordinates are what tie them "
                "together."
            ),
            fontsize=10,
        )

        # ---- Page 3: text BURNED INTO the image ----
        page = doc.new_page()
        page.insert_text(
            (72, 60),
            "Case 3 — text burned into image pixels (invisible to text extraction)",
            fontsize=12,
        )
        page.insert_image(
            pymupdf.Rect(72, 90, 72 + 380, 90 + 240), filename=str(chart_path)
        )
        page.insert_text(
            (72, 350),
            "(See the chart above — its axis labels and value numbers are pixels, "
            "not real text, so PyMuPDF cannot extract them.)",
            fontsize=10,
        )

        # ---- Page 4: text only (control) ----
        page = doc.new_page()
        page.insert_text((72, 80), "Case 4 — text only, no images.", fontsize=14)
        page.insert_text(
            (72, 110),
            "This page is here so we can verify the extractor still produces a "
            "valid manifest with one entry per page.",
            fontsize=11,
        )

        doc.save(FIXTURE.as_posix())
    finally:
        doc.close()

    print(f"Wrote fixture: {FIXTURE}")


if __name__ == "__main__":
    main()