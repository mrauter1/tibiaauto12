"""Statically extract the pinned Tibia 15.33 Linux bitmap font from Qt RCC bytes.

No client or Qt library is loaded. Only zstandard-compressed resource payloads
need the optional ``zstandard`` package; the reader itself uses the standard
library. This is an offline experiment tool, not a production client dependency.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import importlib
import json
from pathlib import Path
import shlex
import struct
import zlib


SOURCE_SHA256 = "02e7365f7f38ef6439873030daeafbdc8653b0fde2ae8cbae603a0128ae94102"
CLIENT_SHA256 = "84646adbacde3aac9d2e04855cff1c8bde7cd9a3b9b6c4c0c67b2f7f84cc1aa8"
ASSET_HASHES = {
    "Verdana10px.fnt": "70bf2eb531039ae180850ca80a4241365b7438596d505c90ff8f9131fc4eae38",
    "Verdana10px.png": "4a9f6c1db76b4403428a08c03ee81440f42a780e0126b3086b47890e9bc2d2d7",
}
EXPECTED_CODEPOINTS = (32, 44, 45, 46, 47, *range(48, 58), *range(65, 91), *range(97, 123))
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_RESOURCE_BYTES = 1024 * 1024


class FontExtractionError(ValueError):
    """Invalid, unsupported, or unverified source data."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise FontExtractionError(message)


@dataclass(frozen=True)
class ResourceEntry:
    node_index: int
    flags: int
    payload_offset: int
    stored_size: int


class RccArchive:
    """Bounded Qt RCC v1/v2/v3 directory and resource reader.

    Offsets are checked within their own RCC section. Traversal rejects cycles,
    multiply referenced nodes, duplicate paths, and unsafe path components. Only
    explicitly requested files are decompressed; resource paths never become
    filesystem destinations.
    """

    def __init__(self, data: bytes):
        require(20 <= len(data) <= MAX_ARCHIVE_BYTES, "Invalid RCC archive size")
        self.data = data
        magic, version, tree, payloads, names = struct.unpack_from(">4s4I", data)
        require(magic == b"qres", "Invalid RCC signature")
        require(version in (1, 2, 3), "Unsupported RCC version")
        self.version = version
        header_size = 24 if version == 3 else 20
        require(len(data) >= header_size, "Truncated RCC header")
        self.header_flags = struct.unpack_from(">I", data, 20)[0] if version == 3 else 0
        require(not self.header_flags & ~5, "Unsupported RCC header flags")
        offsets = (tree, payloads, names)
        require(len(set(offsets)) == 3 and all(header_size <= x < len(data) for x in offsets),
                "Invalid RCC section offsets")
        ordered = sorted((*offsets, len(data)))
        ends = {start: end for start, end in zip(ordered, ordered[1:])}
        self.tree, self.payloads, self.names = tree, payloads, names
        self.tree_end, self.payloads_end, self.names_end = ends[tree], ends[payloads], ends[names]
        self.stride = 14 if version == 1 else 22
        tree_size = self.tree_end - tree
        require(tree_size % self.stride == 0, "Truncated RCC tree node")
        self.node_count = tree_size // self.stride
        require(0 < self.node_count <= 1_000_000, "Invalid RCC node count")
        self.entries = self._walk()

    def _name(self, relative: int) -> str:
        start = self.names + relative
        require(self.names <= start <= self.names_end - 6, "RCC name offset out of bounds")
        length = struct.unpack_from(">H", self.data, start)[0]
        end = start + 6 + length * 2
        require(end <= self.names_end, "Truncated RCC name")
        try:
            name = self.data[start + 6:end].decode("utf-16-be")
        except UnicodeDecodeError as exc:
            raise FontExtractionError("Invalid RCC UTF-16 name") from exc
        require(bool(name) and name not in (".", "..") and not any(c in name for c in "/\\\0"),
                "Unsafe RCC path component")
        return name

    def _walk(self) -> dict[str, ResourceEntry]:
        entries, seen, paths = {}, set(), set()
        pending = [(0, ())]
        while pending:
            index, parent = pending.pop()
            require(index not in seen, "Cyclic or multiply referenced RCC node")
            require(0 <= index < self.node_count, "RCC child index out of bounds")
            require(len(parent) < 64, "RCC directory depth limit exceeded")
            seen.add(index)
            node = self.tree + index * self.stride
            name_offset, flags, first, second = struct.unpack_from(">IHII", self.data, node)
            require(not flags & ~7, "Unsupported RCC resource flags")
            require(flags not in (3, 5, 6, 7), "Conflicting RCC resource flags")
            require(index != 0 or flags == 2, "RCC root must be a directory")
            parts = parent if index == 0 else (*parent, self._name(name_offset))
            path = "/" + "/".join(parts)
            require(path not in paths, "Duplicate RCC resource path")
            paths.add(path)
            if flags == 2:
                count, first_child = first, second
                require(first_child <= self.node_count and count <= self.node_count - first_child,
                        "RCC child range out of bounds")
                pending.extend((child, parts) for child in range(first_child, first_child + count))
            else:
                start = self.payloads + second
                require(self.payloads <= start <= self.payloads_end - 4,
                        "RCC payload offset out of bounds")
                size = struct.unpack_from(">I", self.data, start)[0]
                require(size <= self.payloads_end - start - 4, "Truncated RCC payload")
                entries[path] = ResourceEntry(index, flags, start + 4, size)
        require(len(seen) == self.node_count, "Unreachable RCC tree nodes")
        return entries

    def read(self, path: str, *, max_bytes: int = MAX_RESOURCE_BYTES) -> bytes:
        require(0 < max_bytes <= MAX_RESOURCE_BYTES, "Invalid resource output limit")
        require(path in self.entries, f"RCC resource not found: {path}")
        entry = self.entries[path]
        require(entry.stored_size <= MAX_RESOURCE_BYTES, "RCC stored resource exceeds limit")
        payload = self.data[entry.payload_offset:entry.payload_offset + entry.stored_size]
        if entry.flags == 1:
            require(len(payload) >= 4, "Truncated qCompress payload")
            expected_size = struct.unpack_from(">I", payload)[0]
            require(expected_size <= max_bytes, "RCC uncompressed resource exceeds limit")
            decoder = zlib.decompressobj()
            try:
                result = decoder.decompress(payload[4:], max_bytes + 1)
            except zlib.error as exc:
                raise FontExtractionError("Invalid qCompress payload") from exc
            require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
                    "Incomplete, excessive, or trailing qCompress data")
            require(len(result) == expected_size, "qCompress size mismatch")
        elif entry.flags == 4:
            try:
                zstandard = importlib.import_module("zstandard")
            except ImportError as exc:
                raise FontExtractionError(
                    "Zstandard resource requires optional dependency: "
                    "python -m pip install zstandard==0.25.0") from exc
            try:
                size = zstandard.frame_content_size(payload)
                require(size == zstandard.CONTENTSIZE_UNKNOWN or size <= max_bytes,
                        "RCC uncompressed resource exceeds limit")
                result = zstandard.ZstdDecompressor(max_window_size=1024).decompress(
                    payload, max_output_size=max_bytes, allow_extra_data=False)
            except zstandard.ZstdError as exc:
                raise FontExtractionError("Invalid or excessive Zstandard payload") from exc
        else:
            result = payload
        require(len(result) <= max_bytes, "RCC uncompressed resource exceeds limit")
        return result


def png_dimensions(data: bytes) -> tuple[int, int]:
    require(data.startswith(b"\x89PNG\r\n\x1a\n"), "Invalid PNG signature")
    offset, dimensions, has_pixels = 8, None, False
    while offset < len(data):
        require(offset + 12 <= len(data), "Truncated PNG chunk")
        size = struct.unpack_from(">I", data, offset)[0]
        end = offset + size + 12
        require(end <= len(data), "Truncated PNG payload")
        kind, payload = data[offset + 4:offset + 8], data[offset + 8:end - 4]
        require(zlib.crc32(kind + payload) == struct.unpack_from(">I", data, end - 4)[0],
                "PNG CRC mismatch")
        if dimensions is None:
            require(kind == b"IHDR" and size == 13, "PNG must start with IHDR")
            dimensions = struct.unpack_from(">II", payload)
            require(all(0 < x <= 4096 for x in dimensions), "Invalid PNG dimensions")
        elif kind == b"IHDR":
            raise FontExtractionError("Duplicate PNG IHDR")
        if kind == b"IDAT":
            has_pixels = True
        if kind == b"IEND":
            require(size == 0 and end == len(data) and has_pixels, "Invalid PNG end")
            return dimensions
        offset = end
    raise FontExtractionError("Missing PNG IEND")


def validate_font(fnt: bytes, png: bytes) -> dict:
    """Validate the intended glyph set, page and rectangle extents before writing."""
    dimensions = png_dimensions(png)
    records = {}
    try:
        for line in fnt.decode("utf-8").splitlines():
            fields = shlex.split(line)
            if not fields:
                continue
            values = dict(field.split("=", 1) for field in fields[1:])
            records.setdefault(fields[0], []).append(values)
        require(all(len(records.get(kind, [])) == 1 for kind in ("info", "common", "page", "chars")),
                "Missing or duplicate BMFont metadata")
        common, page = records["common"][0], records["page"][0]
        require((int(common["scaleW"]), int(common["scaleH"])) == dimensions,
                "BMFont dimensions differ from atlas")
        require(int(common["pages"]) == 1 and int(page["id"]) == 0
                and page["file"] == "/fonts/Verdana10px.png", "Unexpected BMFont page")
        glyphs = records.get("char", [])
        ids = [int(glyph["id"]) for glyph in glyphs]
        require(len(ids) == int(records["chars"][0]["count"]) == len(EXPECTED_CODEPOINTS)
                and sorted(ids) == list(EXPECTED_CODEPOINTS), "Unexpected BMFont glyph map")
        for glyph in glyphs:
            x, y, width, height = (int(glyph[key]) for key in ("x", "y", "width", "height"))
            require(x >= 0 and y >= 0 and width > 0 and height > 0
                    and x + width <= dimensions[0] and y + height <= dimensions[1],
                    "BMFont glyph rectangle exceeds atlas")
            require(int(glyph["page"]) == 0 and int(glyph["xadvance"]) > 0,
                    "Invalid BMFont glyph page or advance")
            int(glyph["xoffset"]), int(glyph["yoffset"])
        return {"atlas_width": dimensions[0], "atlas_height": dimensions[1],
                "glyph_count": len(ids), "codepoints": sorted(ids),
                "line_height": int(common["lineHeight"]), "base": int(common["base"])}
    except (UnicodeError, KeyError, ValueError) as exc:
        if isinstance(exc, FontExtractionError):
            raise
        raise FontExtractionError("Malformed BMFont descriptor") from exc


def extract_font(source: Path, output: Path, client_binary: Path | None = None) -> dict:
    require(source.stat().st_size <= MAX_ARCHIVE_BYTES, "RCC archive exceeds size limit")
    data = source.read_bytes()
    require(sha256(data).hexdigest() == SOURCE_SHA256, "Source RCC SHA-256 mismatch")
    if client_binary is not None:
        require(sha256(client_binary.read_bytes()).hexdigest() == CLIENT_SHA256,
                "Client binary SHA-256 mismatch")
    archive = RccArchive(data)
    assets = {name: archive.read("/fonts/" + name) for name in ASSET_HASHES}
    for name, content in assets.items():
        require(sha256(content).hexdigest() == ASSET_HASHES[name], f"Asset SHA-256 mismatch: {name}")
    metrics = validate_font(assets["Verdana10px.fnt"], assets["Verdana10px.png"])
    provenance = {
        "schema": "tibiaauto.extracted-bitmap-font.v1",
        "client_reference": {"product": "Tibia", "version": "15.33.c9e45e", "platform": "Linux",
                             "binary_sha256": CLIENT_SHA256,
                             "binary_verified_during_extraction": client_binary is not None},
        "source": {"filename": "graphics_resources.rcc", "sha256": SOURCE_SHA256,
                   "size_bytes": len(data), "rcc_version": archive.version,
                   "rcc_header_flags": archive.header_flags},
        "extractor": {"path": "tools/extract_tibia_font.py",
                      "sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                      "method": "Static Qt RCC traversal; no native client or Qt code execution"},
        "font": metrics,
        "assets": {},
        "scope": "Linux client resource evidence only; Windows battle-list font identity is unverified. "
                 "Experimental profile; no production reader enablement.",
    }
    for name, content in assets.items():
        entry = archive.entries["/fonts/" + name]
        provenance["assets"][name] = {
            "resource_path": "/fonts/" + name, "sha256": ASSET_HASHES[name],
            "size_bytes": len(content), "compression": {0: "none", 1: "qCompress", 4: "zstandard"}[entry.flags],
            "node_index": entry.node_index, "payload_offset": entry.payload_offset,
            "stored_size_bytes": entry.stored_size,
        }
    # All parsing, provenance checks and validation finish before writing known names.
    output.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        destination = output / name
        require(not destination.is_symlink(), "Refusing symlink asset destination")
    require(not (output / "provenance.json").is_symlink(), "Refusing symlink provenance destination")
    for name, content in assets.items():
        (output / name).write_bytes(content)
    (output / "provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    return provenance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Pinned Linux graphics_resources.rcc")
    parser.add_argument("--out", type=Path, required=True, help="Directory for the two assets and provenance")
    parser.add_argument("--client-binary", type=Path, help="Also verify the associated Linux client SHA-256")
    args = parser.parse_args()
    try:
        provenance = extract_font(args.source, args.out, args.client_binary)
    except (OSError, FontExtractionError) as exc:
        parser.exit(2, f"Font extraction failed: {exc}\n")
    print(json.dumps({"output": str(args.out), "source_sha256": SOURCE_SHA256,
                      "glyph_count": provenance["font"]["glyph_count"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
