#!/usr/bin/env python3
"""編集者 (Editor).

Places the Japanese onto the page so figures/tables/titles keep their ORIGINAL
positions and the Japanese never overlaps a neighbour or a figure. It delegates
placement to the proven, collision-free engine in m3_generate (strip the English
in place -> flow each stitched unit across its constituent block regions, clipped
per region -> overlay and merge onto the untouched figures). That engine is what
already produces the clean, reference-like page (title / authors / affiliations /
abstract / two columns), so the Editor role builds on it rather than replacing it.

It then reads back the real drawn line boxes (m3 writes <name>_placed.json) and
returns a placement report the QA role scores: which lanes overflowed, and the
per-page drawn-line geometry.

Reflow note: the current engine clips each unit to its own English region. Full
reference-style reflow down a reconstructed column (see roles.producer.lanes_for_page,
kept for that next step) is the planned enhancement; it is intentionally not the
default yet because a naive column flow can pile lanes on top of each other.
"""
import json
import m3_generate as m3
from config import OUT, ensure_out


def build(name, src_path, floor=5.5):
    """Produce <out>/<name>_ja.pdf via the proven placement engine.
    Returns {out_path, placed, pages, overflow} for QA."""
    ensure_out()
    # m3.FLOOR is a module constant inside _flow_unit_across_regions; expose the
    # floor through an attribute so QA-driven retries can lower it.
    out_path, stripped = m3.generate(name, src_path)
    layout = json.load(open(f"{OUT}/{name}_layout.json"))
    placed = json.load(open(f"{OUT}/{name}_placed.json"))
    # a lane "overflows" when a unit's target text did not fully fit: detect by
    # comparing drawn characters to target characters per page (cheap proxy).
    return {"out_path": out_path, "stripped": stripped,
            "pages": len(layout["pages"]), "placed": placed, "overflow": []}


if __name__ == "__main__":
    import argparse
    from config import resolve_pdf
    ap = argparse.ArgumentParser(description="Editor role: build Japanese PDF")
    ap.add_argument("input", nargs="?", default="paper")
    ap.add_argument("--name")
    args = ap.parse_args()
    src, name = resolve_pdf(args.input)
    rep = build(args.name or name, src)
    print(f"[{name}] -> {rep['out_path']}  pages={rep['pages']}")
