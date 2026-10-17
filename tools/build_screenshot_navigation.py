"""Build contact sheets for navigation only; never use them as reader inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "tests/fixtures/screenshots/manifest.json"


def build(manifest: Path, batch: str) -> list[Path]:
    entries = [
        entry for entry in json.loads(manifest.read_text(encoding="utf-8"))["screenshots"]
        if entry.get("collection_batch") == batch
    ]
    destination = manifest.parent / "navigation"
    destination.mkdir(exist_ok=True)
    outputs = []
    for start in range(0, len(entries), 16):
        sheet = Image.new("RGB", (1600, 1050), "#eef1f4")
        draw = ImageDraw.Draw(sheet)
        draw.text((12, 10), f"{batch} | NAVIGATION ONLY - resized previews, never benchmark inputs", fill="black")
        for offset, entry in enumerate(entries[start:start + 16]):
            x, y = (offset % 4) * 400, 40 + (offset // 4) * 250
            with Image.open(manifest.parent / entry["file"]) as source:
                preview = source.convert("RGB")
            preview.thumbnail((390, 210))
            sheet.paste(preview, (x + 5, y + 32))
            draw.text((x + 5, y), f"{start + offset + 1:02} {entry['id']}", fill="black")
            size = entry["integrity"]
            draw.text((x + 5, y + 14), f"{size['width']}x{size['height']} | {entry['split']} | {entry['coverage']}", fill="#334155")
        output = destination / f"collection-{batch}-{start // 16 + 1}.jpg"
        sheet.save(output, quality=88)
        outputs.append(output)
    return outputs


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--batch", default="2026-10-17")
    args = parser.parse_args()
    for path in build(args.manifest, args.batch):
        print(path.relative_to(ROOT))
