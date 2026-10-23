from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import struct
import zlib

import pytest

from tools import extract_tibia_font as extraction


PROFILE = Path(__file__).resolve().parents[1] / "profiles/tibia-15.33-bitmap-font"


def synthetic_rcc(payload=b"bitmap font", *, version=3, flags=0, name="font.fnt"):
    """One root and one file, with separate payload, name and tree sections."""
    encoded = name.encode("utf-16-be")
    names = struct.pack(">HI", len(encoded) // 2, 0) + encoded
    data = struct.pack(">I", len(payload)) + payload
    header_size = 24 if version == 3 else 20
    data_offset = header_size
    name_offset = data_offset + len(data)
    tree_offset = name_offset + len(names)
    header = struct.pack(">4s4I", b"qres", version, tree_offset, data_offset, name_offset)
    if version == 3:
        header += struct.pack(">I", flags)
    tree = b""
    for fields in [(0, 2, 1, 1), (0, flags, 0, 0)]:
        tree += struct.pack(">IHII", *fields)
        if version >= 2:
            tree += struct.pack(">Q", 0)
    return bytearray(header + data + names + tree)


def offsets(data):
    _, version, tree, payloads, names = struct.unpack_from(">4s4I", data)
    return tree, payloads, names, 14 if version == 1 else 22


@pytest.mark.parametrize("version", [1, 2, 3])
def test_plain_rcc_versions(version):
    archive = extraction.RccArchive(synthetic_rcc(version=version))
    assert archive.version == version
    assert set(archive.entries) == {"/font.fnt"}
    assert archive.read("/font.fnt") == b"bitmap font"


def test_qcompress_is_bounded_and_checks_original_size():
    raw = b"a" * 100
    payload = struct.pack(">I", len(raw)) + zlib.compress(raw)
    archive = extraction.RccArchive(synthetic_rcc(payload, flags=1))
    assert archive.read("/font.fnt") == raw
    with pytest.raises(extraction.FontExtractionError, match="exceeds limit"):
        archive.read("/font.fnt", max_bytes=50)
    bad = struct.pack(">I", len(raw) - 1) + zlib.compress(raw)
    with pytest.raises(extraction.FontExtractionError, match="size mismatch"):
        extraction.RccArchive(synthetic_rcc(bad, flags=1)).read("/font.fnt")


@pytest.mark.parametrize("payload", [b"", b"\0\0\0\x04oops", struct.pack(">I", 1) + zlib.compress(b"a") + b"extra"])
def test_bad_qcompress_payloads(payload):
    with pytest.raises(extraction.FontExtractionError):
        extraction.RccArchive(synthetic_rcc(payload, flags=1)).read("/font.fnt")


def test_zstandard_is_optional_and_reports_install_command(monkeypatch):
    def missing_package(name):
        assert name == "zstandard"
        raise ImportError("absent")
    monkeypatch.setattr(extraction.importlib, "import_module", missing_package)
    with pytest.raises(extraction.FontExtractionError, match="pip install zstandard==0.25.0"):
        extraction.RccArchive(synthetic_rcc(b"compressed", flags=4)).read("/font.fnt")


@pytest.mark.skipif(importlib.util.find_spec("zstandard") is None, reason="optional extraction dependency")
def test_zstandard_compression_bounds_and_trailing_data():
    import zstandard
    raw = b"a" * 100
    compressed = zstandard.ZstdCompressor().compress(raw)
    archive = extraction.RccArchive(synthetic_rcc(compressed, flags=4))
    assert archive.read("/font.fnt") == raw
    with pytest.raises(extraction.FontExtractionError, match="exceeds limit"):
        archive.read("/font.fnt", max_bytes=50)
    for bad in (compressed[:-1], compressed + b"extra", b"garbage"):
        with pytest.raises(extraction.FontExtractionError):
            extraction.RccArchive(synthetic_rcc(bad, flags=4)).read("/font.fnt")


@pytest.mark.parametrize("name", ["..", ".", "", "../outside", "dir/file", "dir\\file", "with\0nul"])
def test_path_components_cannot_escape_output(name):
    with pytest.raises(extraction.FontExtractionError, match="Unsafe RCC path component"):
        extraction.RccArchive(synthetic_rcc(name=name))


@pytest.mark.parametrize("mutation, message", [
    ("truncated-header", "size"),
    ("magic", "signature"),
    ("version", "version"),
    ("section", "section offsets"),
    ("tree-truncated", "tree node"),
    ("root-file", "root"),
    ("child-range", "child range"),
    ("cycle", "Cyclic"),
    ("name-offset", "name offset"),
    ("name-length", "Truncated RCC name"),
    ("payload-offset", "payload offset"),
    ("payload-length", "Truncated RCC payload"),
    ("resource-flags", "Conflicting"),
    ("unreachable", "Unreachable"),
])
def test_malformed_rcc_boundaries(mutation, message):
    data = synthetic_rcc()
    tree, payloads, names, stride = offsets(data)
    if mutation == "truncated-header":
        data = data[:19]
    elif mutation == "magic":
        data[:4] = b"evil"
    elif mutation == "version":
        struct.pack_into(">I", data, 4, 99)
    elif mutation == "section":
        struct.pack_into(">I", data, 8, payloads)
    elif mutation == "tree-truncated":
        data.pop()
    elif mutation == "root-file":
        struct.pack_into(">H", data, tree + 4, 0)
    elif mutation == "child-range":
        struct.pack_into(">I", data, tree + 6, 10_000)
    elif mutation == "cycle":
        struct.pack_into(">I", data, tree + 10, 0)
    elif mutation == "name-offset":
        struct.pack_into(">I", data, tree + stride, 10_000)
    elif mutation == "name-length":
        struct.pack_into(">H", data, names, 10_000)
    elif mutation == "payload-offset":
        struct.pack_into(">I", data, tree + stride + 10, 10_000)
    elif mutation == "payload-length":
        struct.pack_into(">I", data, payloads, 10_000)
    elif mutation == "resource-flags":
        struct.pack_into(">H", data, tree + stride + 4, 5)
    elif mutation == "unreachable":
        struct.pack_into(">I", data, tree + 6, 0)
    with pytest.raises(extraction.FontExtractionError, match=message):
        extraction.RccArchive(data)


def test_duplicate_resource_paths_are_rejected():
    data = synthetic_rcc()
    tree, _, _, stride = offsets(data)
    struct.pack_into(">I", data, tree + 6, 2)
    data.extend(data[tree + stride:tree + stride * 2])
    with pytest.raises(extraction.FontExtractionError, match="Duplicate RCC resource path"):
        extraction.RccArchive(data)


def test_unverified_source_fails_without_writing(tmp_path):
    source, output = tmp_path / "source.rcc", tmp_path / "out"
    source.write_bytes(synthetic_rcc())
    with pytest.raises(extraction.FontExtractionError, match="Source RCC SHA-256 mismatch"):
        extraction.extract_font(source, output)
    assert not output.exists()


def test_packaged_bytes_and_provenance_are_bound_to_extractor():
    provenance = json.loads((PROFILE / "provenance.json").read_text())
    assert provenance["source"]["sha256"] == extraction.SOURCE_SHA256
    assert provenance["source"]["rcc_version"] == 3
    assert provenance["client_reference"]["binary_sha256"] == extraction.CLIENT_SHA256
    assert provenance["client_reference"]["binary_verified_during_extraction"] is True
    assert provenance["extractor"]["sha256"] == sha256(Path(extraction.__file__).read_bytes()).hexdigest()
    for name, expected in extraction.ASSET_HASHES.items():
        content = (PROFILE / name).read_bytes()
        assert sha256(content).hexdigest() == expected == provenance["assets"][name]["sha256"]
        assert len(content) == provenance["assets"][name]["size_bytes"]
        assert provenance["assets"][name]["resource_path"] == "/fonts/" + name
    metrics = extraction.validate_font((PROFILE / "Verdana10px.fnt").read_bytes(),
                                       (PROFILE / "Verdana10px.png").read_bytes())
    assert metrics == provenance["font"]
    assert metrics["glyph_count"] == 67
    assert (metrics["atlas_width"], metrics["atlas_height"]) == (128, 64)


@pytest.mark.parametrize("old, new, message", [
    (b"scaleW=128", b"scaleW=127", "dimensions"),
    (b"char id=32", b"char id=33", "glyph map"),
    (b"x=26", b"x=999", "rectangle"),
    (b"chars count=67", b"chars count=66", "glyph map"),
    (b"/fonts/Verdana10px.png", b"/other.png", "page"),
])
def test_descriptor_validation(old, new, message):
    fnt = (PROFILE / "Verdana10px.fnt").read_bytes().replace(old, new, 1)
    with pytest.raises(extraction.FontExtractionError, match=message):
        extraction.validate_font(fnt, (PROFILE / "Verdana10px.png").read_bytes())


def test_invalid_png_crc():
    png = bytearray((PROFILE / "Verdana10px.png").read_bytes())
    png[20] ^= 1
    with pytest.raises(extraction.FontExtractionError, match="PNG CRC mismatch"):
        extraction.png_dimensions(png)
