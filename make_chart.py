"""Build a small bar-chart PNG with axis labels burned in (the "case 3"
case from the overlay discussion)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def make_chart() -> Path:
    out = Path(__file__).parent / "chart.png"
    img = Image.new("RGB", (520, 320), (255, 255, 255))
    d = ImageDraw.Draw(img)

    # frame
    d.rectangle([20, 20, 500, 300], outline=(60, 60, 60), width=2)
    # axes
    d.line([(60, 260), (480, 260)], fill=(60, 60, 60), width=2)
    d.line([(60, 260), (60, 50)], fill=(60, 60, 60), width=2)

    # try to load a default font; fall back to the bitmap default if not.
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except OSError:
        font = ImageFont.load_default()
        small = ImageFont.load_default()

    bars = [60, 120, 90, 200, 150, 80, 110, 175]
    for i, h in enumerate(bars):
        x0 = 80 + i * 50
        x1 = x0 + 35
        y1 = 260 - h
        d.rectangle([x0, y1, x1, 260], fill=(70, 130, 200), outline=(40, 90, 160))
        # value label on top of each bar (text burned INTO the image)
        d.text((x0 + 4, y1 - 16), str(h), fill=(20, 20, 20), font=small)

    d.text((220, 270), "Month", fill=(20, 20, 20), font=font)
    d.text((10, 130), "Sales", fill=(20, 20, 20), font=font)
    d.text((220, 8), "Q3 Sales by Month", fill=(20, 20, 20), font=font)

    img.save(out)
    return out


if __name__ == "__main__":
    p = make_chart()
    print(f"Wrote {p} ({p.stat().st_size} bytes)")