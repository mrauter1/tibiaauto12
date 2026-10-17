"""Regenerate integrity metadata for the manually annotated screenshot corpus."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).parent


def resources(values, boxes):
    return {
        name: {
            "value": value,
            "visible": True,
            "box": boxes[name],
            "verification": "manual_verified",
        }
        for name, value in values.items()
    }


def entry(identifier, filename, *, split, classification, client_class,
          expected_discovery, coverage, fidelity, source, client, panel_box, values,
          boxes, notes):
    path = ROOT / "originals" / filename
    with Image.open(path) as image:
        width, height = image.size
        image_format = image.format
    return {
        "id": identifier,
        "file": f"originals/{filename}",
        "split": split,
        "classification": classification,
        "client_class": client_class,
        "expected_discovery": expected_discovery,
        "coverage": coverage,
        "data_origin": "real",
        "asset_fidelity": fidelity,
        "source": source,
        "integrity": {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "width": width,
            "height": height,
            "format": image_format,
        },
        "client": client,
        "skills_panel": {"visible": True, "box": panel_box},
        "labels": resources(values, boxes),
        "notes": notes,
    }


PORTAL_PAGE = "https://forum.portaltibia.com.br/topic/32425-chars-skills/"
PORTAL_CLIENT = {
    "family": "CipSoft legacy native client",
    "version": None,
    "confidence": "high_family_only",
    "evidence": (
        "The source page identifies these as Tibia character screenshots and was posted "
        "2016-01-11; its UI predates the Qt client released later in 2016. The post date "
        "does not prove an exact installed version."
    ),
}


entries = [
    entry(
        "user-paulistinha-20261013",
        "user-paulistinha-20261013.png",
        split="train",
        classification="confirmed",
        client_class="target_official_style",
        expected_discovery=True,
        coverage="full_screenshot",
        fidelity="source_original",
        source={
            "kind": "user_upload",
            "page_url": None,
            "image_url": None,
            "retrieved_at": "2026-10-13",
            "posted_at": None,
            "character": "Matusalah",
        },
        client={
            "family": "Paulistinha OT client with an official-style Qt-era layout",
            "version": None,
            "confidence": "high_family_only",
            "evidence": (
                "The visible title is 'Paulistinha Ot - Matusalah' and the frame uses the "
                "official-style dockable Tibia interface. Appearance alone does not prove "
                "the implementation or build, and no version string is visible."
            ),
        },
        panel_box=[1481, 29, 1697, 313],
        values={"resource_health": 710, "resource_mana": 1085,
                "resource_soul": 4, "resource_capacity": 192},
        boxes={"resource_health": [1644, 61, 1673, 72],
               "resource_mana": [1631, 78, 1673, 90],
               "resource_soul": [1664, 96, 1673, 107],
               "resource_capacity": [1645, 113, 1673, 125]},
        notes="Only confirmed target-family full desktop screenshot currently available.",
    ),
]

portal = [
    ("01-0i483jb", "portal-tibia-2016-01-0i483jb.webp", "train", [223, 1, 394, 350],
     (185, 90, 100, 78), ([351,75,369,83],[356,89,369,97],[351,103,369,111],[356,117,369,125])),
    ("02-VtukRkf", "portal-tibia-2016-02-VtukRkf.webp", "train", [225, 35, 399, 384],
     (185, 90, 100, 394), ([343,101,361,109],[348,115,361,123],[343,129,361,137],[341,143,361,151])),
    ("03-dUqF9aj", "portal-tibia-2016-03-dUqF9aj.webp", "train", [284, 26, 446, 382],
     (185, 90, 100, 157), ([404,98,422,106],[409,112,422,120],[404,126,422,134],[404,140,422,148])),
    ("04-P9brcLm", "portal-tibia-2016-04-P9brcLm.webp", "heldout", [245, 25, 442, 378],
     (180, 85, 100, 451), ([364,95,382,103],[369,109,382,117],[364,123,382,131],[364,137,382,145])),
    ("08-L1QY5z1", "portal-tibia-2016-08-L1QY5z1.webp", "train", [230, 58, 394, 393],
     (185, 90, 100, 141), ([349,116,367,124],[354,130,367,138],[349,144,367,152],[349,158,367,166])),
    ("09-BnumJDC", "portal-tibia-2016-09-BnumJDC.webp", "heldout", [228, 4, 389, 368],
     (260, 71, 100, 106), ([355,79,375,87],[362,93,375,101],[355,107,375,115],[355,121,375,129])),
    ("10-Ttu3X5q", "portal-tibia-2016-10-Ttu3X5q.webp", "heldout", [143, 0, 301, 345],
     (6080, 2055, 200, 1335), ([251,72,281,82],[251,86,281,96],[251,100,281,110],[253,114,281,124])),
]
for suffix, filename, split, panel, nums, boxes in portal:
    image_id = suffix.split("-", 1)[1]
    values = dict(zip(("resource_health", "resource_mana", "resource_soul", "resource_capacity"), nums))
    box_map = dict(zip(values, boxes))
    entries.append(entry(
        f"portal-tibia-2016-{suffix}", filename, split=split,
        classification="similar", client_class="legacy_transfer",
        expected_discovery=True, coverage="supplemental_crop",
        fidelity="source_transcode",
        source={
            "kind": "web",
            "page_url": PORTAL_PAGE,
            "image_url": f"https://i.imgur.com/{image_id}.png",
            "retrieved_at": "2026-10-13",
            "posted_at": "2016-01-10/11",
            "character": None,
        },
        client=PORTAL_CLIENT, panel_box=panel, values=values, boxes=box_map,
        notes=(
            "Real Skills-panel crop served by Imgur as a WebP transcode. Retained as an "
            "explicit legacy transfer case; it is neither a full screenshot nor "
            "evidence for the target Qt client."
        ),
    ))

full_path = ROOT / "originals" / "tibiadecorations-2024-full-window.png"
with Image.open(full_path) as image:
    full_width, full_height = image.size
    full_format = image.format
entries.append({
    "id": "tibiadecorations-2024-full-window",
    "file": "originals/tibiadecorations-2024-full-window.png",
    "split": "heldout",
    "classification": "negative",
    "client_class": "official_style_no_skills_panel",
    "expected_discovery": False,
    "coverage": "full_screenshot",
    "data_origin": "real",
    "asset_fidelity": "source_original",
    "source": {
        "kind": "web",
        "page_url": "https://tibiadecorations.com/quality-screenshots/",
        "image_url": "https://tibiadecorations.com/wp-content/uploads/2024/01/full-window.png",
        "retrieved_at": "2026-10-13",
        "posted_at": "2024-01",
        "character": "Makadamia",
    },
    "integrity": {
        "sha256": hashlib.sha256(full_path.read_bytes()).hexdigest(),
        "width": full_width,
        "height": full_height,
        "format": full_format,
    },
    "client": {
        "family": "official Tibia client",
        "version": None,
        "confidence": "high_family_only",
        "evidence": (
            "The supported Tibia fansite's screenshot guide presents this as a Tibia full-window "
            "capture. The article does not state a client build, so the exact version is unknown."
        ),
    },
    "skills_panel": {"visible": False, "box": None},
    "labels": {
        name: {"value": None, "visible": False, "box": None,
               "verification": "not_visible"}
        for name in ("resource_health", "resource_mana", "resource_soul", "resource_capacity")
    },
    "notes": (
        "Real 1920x1017 full-window official-client example with the Skills panel closed. "
        "The alternate top/equipment widgets are deliberately not annotated as Skills rows."
    ),
})

entries.append(entry(
    "dfg-kalibra-ek362",
    "dfg-kalibra-ek362.webp",
    split="train",
    classification="similar",
    client_class="official_style_transfer",
    expected_discovery=True,
    coverage="supplemental_crop",
    fidelity="source_original",
    source={
        "kind": "web",
        "page_url": "https://www.dfg.com.br/pt/tibia/contas/ek-362-kalibra-2-dummy-de-treino-917867645",
        "image_url": "https://cdn.dfg.com.br/itemimages/917867645-ek-362-kalibra-2-dummy-de-treino-TDR2.webp",
        "retrieved_at": "2026-10-13",
        "posted_at": None,
        "character": None,
    },
    client={
        "family": "official-style Qt-era Tibia layout",
        "version": None,
        "confidence": "medium_family_only",
        "evidence": (
            "The DFG listing identifies the game as Tibia and the screenshot shows the same "
            "dockable Qt-era UI family. The marketplace page does not state or prove a client build."
        ),
    },
    panel_box=[0, 0, 169, 508],
    values={"resource_health": 5480, "resource_mana": 1855,
            "resource_soul": 200, "resource_capacity": 2476},
    boxes={"resource_health": [116, 94, 150, 104],
           "resource_mana": [117, 108, 150, 118],
           "resource_soul": [128, 122, 150, 130],
           "resource_capacity": [116, 136, 150, 146]},
    notes=(
        "Real 1016x706 full client image with a left-docked Skills panel and all four "
        "resource rows visible. Retained as a same-family transfer case; exact version unknown. "
        "This was moved from heldout to train after its first failed read was inspected during "
        "development of the generic fallback."
    ),
))

entries.append(entry(
    "dfg-retro-pvp-rp535",
    "dfg-list-978242455-conta-50-loyalty-rp-535-com-itens-retro-pvp-B03E-1924049.webp",
    split="train",
    classification="similar",
    client_class="official_style_transfer",
    expected_discovery=True,
    coverage="full_screenshot",
    fidelity="source_original",
    source={
        "kind": "web",
        "page_url": (
            "https://www.dfg.com.br/tibia/contas/"
            "50-loyalty-rp-535-com-itens-retro-pvp-978242455"
        ),
        "image_url": "https://cdn.dfg.com.br/itemimages/978242455-conta-50-loyalty-rp-535-com-itens-retro-pvp-B03E-1924049.webp",
        "retrieved_at": "2026-10-13",
        "posted_at": None,
        "character": None,
    },
    client={
        "family": "official-style Qt-era Tibia layout",
        "version": None,
        "confidence": "medium_family_only",
        "evidence": (
            "The Tibia account listing title and visible dockable interface support the client "
            "family classification. No authoritative build string is present."
        ),
    },
    panel_box=[0, 0, 172, 437],
    values={"resource_health": 5455, "resource_mana": 7995,
            "resource_soul": 100, "resource_capacity": 4250},
    boxes={"resource_health": [120, 92, 154, 102],
           "resource_mana": [120, 106, 154, 116],
           "resource_soul": [133, 120, 154, 130],
           "resource_capacity": [120, 134, 154, 144]},
    notes=(
        "Real 1920x1080 full-client image with a left-docked Skills panel, providing a "
        "layout and resolution transfer case. Exact version is unknown."
    ),
))

entries.append(entry(
    "dfg-pvp-ek133",
    "dfg-related-998237216-vendo-ek-133-mundo-pvp-disponivel-para-transferencia-4DOS.webp",
    split="train",
    classification="similar",
    client_class="official_style_transfer",
    expected_discovery=True,
    coverage="supplemental_crop",
    fidelity="source_original",
    source={
        "kind": "web",
        "page_url": (
            "https://www.dfg.com.br/en/tibia/"
            "vendo-ek-133-mundo-pvp-disponivel-para-transferencia-998237216"
        ),
        "image_url": "https://cdn.dfg.com.br/itemimages/998237216-vendo-ek-133-mundo-pvp-disponivel-para-transferencia-4DOS.webp",
        "retrieved_at": "2026-10-13",
        "posted_at": None,
        "character": None,
    },
    client={
        "family": "official-style Qt-era Tibia layout",
        "version": None,
        "confidence": "medium_family_only",
        "evidence": (
            "The Tibia account listing and visible dockable interface support the family "
            "classification. The screenshot itself does not expose a client build."
        ),
    },
    panel_box=[638, 21, 814, 417],
    values={"resource_health": 2060, "resource_mana": 560,
            "resource_soul": 100, "resource_capacity": 2207},
    boxes={"resource_health": [758, 118, 792, 126],
           "resource_mana": [770, 132, 792, 140],
           "resource_soul": [770, 146, 792, 154],
           "resource_capacity": [758, 161, 792, 169]},
    notes=(
        "Source-cropped 970x637 client image with a right-side Skills panel and overlapping Quest "
        "Log window. On its initial unseen evaluation, one layout was found and health, mana, "
        "and capacity were exact at the annotated locations; the Soul region was omitted/UNKNOWN "
        "with zero incorrect-known values. It became train data once that failure was inspected."
    ),
))

entries.append(entry(
    "dfg-ustebra-ek177",
    "dfg-917013286-ustebra-ek177-DWTG.webp",
    split="heldout",
    classification="similar",
    client_class="official_style_transfer",
    expected_discovery=True,
    coverage="full_screenshot",
    fidelity="source_original",
    source={
        "kind": "web",
        "page_url": (
            "https://www.dfg.com.br/tibia/contas/"
            "com-ek-em-ustebra-177-skill-113-106-axe-917013286"
        ),
        "image_url": (
            "https://cdn.dfg.com.br/itemimages/"
            "917013286-conta-tibia-com-ek-em-ustebra-177-skill-113-106-axe-DWTG.webp"
        ),
        "retrieved_at": "2026-10-13",
        "posted_at": None,
        "character": "Tobhi",
    },
    client={
        "family": "official-style Qt-era Tibia layout",
        "version": None,
        "confidence": "medium_family_only",
        "evidence": (
            "The DFG page identifies the listing as Tibia, and the preserved full-window "
            "image shows the dockable official-style client with a Skills panel. Neither "
            "the page nor the image exposes an authoritative client build."
        ),
    },
    panel_box=[1743, 463, 1917, 895],
    values={"resource_health": 2720, "resource_mana": 541,
            "resource_soul": 100, "resource_capacity": 1451},
    boxes={"resource_health": [1864, 559, 1898, 569],
           "resource_mana": [1876, 573, 1897, 581],
           "resource_soul": [1877, 587, 1898, 595],
           "resource_capacity": [1865, 601, 1897, 611]},
    notes=(
        "Real 1920x1009 full-client image with a narrow right-docked Skills panel. "
        "The untouched heldout image was exact for all four resources on its first "
        "evaluation and was not used to tune the reader. Exact version is unknown."
    ),
))

entries.append(entry(
    "dfg-yonabra-ek195",
    "dfg-974104750-yonabra-ek195-FQ2C.webp",
    split="train",
    classification="similar",
    client_class="official_style_transfer",
    expected_discovery=True,
    coverage="full_screenshot",
    fidelity="source_original",
    source={
        "kind": "web",
        "page_url": (
            "https://www.dfg.com.br/tibia/contas/"
            "yonabra-vendo-ek-195-swd-95-95-3kk-212-dias-p-a-974104750"
        ),
        "image_url": (
            "https://cdn.dfg.com.br/itemimages/"
            "974104750-tibia-yonabra-vendo-ek-195-swd-95-95-3kk-212-dias-p-a-FQ2C.webp"
        ),
        "retrieved_at": "2026-10-13",
        "posted_at": None,
        "character": "Heinschalle",
    },
    client={
        "family": "official-style Qt-era Tibia layout",
        "version": None,
        "confidence": "medium_family_only",
        "evidence": (
            "The DFG page identifies the listing as Tibia, and the full-window image title "
            "and dockable interface support the official-style family classification. A "
            "date visible in game content is not a client build identifier."
        ),
    },
    panel_box=[1143, 20, 1271, 383],
    values={"resource_health": 2969, "resource_mana": 1025,
            "resource_soul": 200, "resource_capacity": 1676},
    boxes={"resource_health": [1232, 90, 1256, 97],
           "resource_mana": [1232, 101, 1256, 108],
           "resource_soul": [1240, 111, 1256, 118],
           "resource_capacity": [1232, 122, 1256, 130]},
    notes=(
        "Real 1400x788 full-client image with a compact right-docked Skills panel and "
        "macOS window chrome. Initial unseen evaluation returned no layout and zero "
        "candidates. It became train data once that failure was inspected and reported "
        "for reader development. Exact version is unknown."
    ),
))

# Additional manually reviewed real captures preserve their received source bytes.
for specimen in json.loads((ROOT / "additional-sources.json").read_text(encoding="utf-8")):
    path = ROOT / specimen["file"]
    with Image.open(path) as image:
        width, height = image.size
        image_format = image.format
    specimen["integrity"] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "width": width, "height": height, "format": image_format,
    }
    entries.append(specimen)

# Human review is kept separate from detector output and never changes pixels.
visual_review = json.loads((ROOT / "visual-review.json").read_text(encoding="utf-8"))
for item in entries:
    item["render_equivalence"] = visual_review["sources"][item["id"]]

manifest = {
    "schema": "tibiaauto.screenshot-corpus",
    "version": 1,
    "box_format": "xyxy-half-open",
    "generated_at": "2026-10-17",
    "notes": [
        "Boxes are absolute full-image [left, top, right, bottom] coordinates; right and bottom are excluded.",
        "Resource boxes tightly enclose numeric glyphs (including displayed thousands separators), not row labels.",
        "Confirmed means the target Paulistinha Qt-style client family, not a confirmed exact build.",
        "Legacy similar fixtures must not be counted as same-version or full-screenshot coverage.",
        "No synthetic image is included. Exact versions remain unknown when no build string or authoritative source states one.",
        "The supplied executable was independently identified as a modified CipSoft 15.30 Qt6 client, but that binary context does not prove which build produced any screenshot.",
        "The 1016x706 Kalibra, 970x637 cluttered, and 1400x788 Yonabra images are development/train data because their initial failures were inspected; their notes preserve the unseen outcomes.",
    ],
    "screenshots": entries,
}
(ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
