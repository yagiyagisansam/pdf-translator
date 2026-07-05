#!/usr/bin/env python3
"""PDF製作者 (PDF Producer).

Turns the source PDF into a LAYOUT SPEC the editor can reconstruct from:
per page, the column lanes (full-width / left / right) with their x-range and
vertical flow band, and the obstacle boxes (figures, and text that stays in the
original language: tables, references, running heads, page numbers) that the
Japanese must flow around. Figure/table positions are read verbatim from M1 so
they never move.

ROLE RESPONSIBILITY - correct text placement (正しい文章配置):
The producer OWNS the geometry that makes placement correct. Every element that
stays on the page verbatim must be exposed as an obstacle so the editor never
draws Japanese on top of it. That means, in every lane:
  - figures / images
  - kept-language text (tables, references, running heads, page numbers)
  - VECTOR RULES (horizontal lines: abstract-box borders, section separators,
    table rules) - kept in place, so text must flow AROUND them, never across.
It is also the producer's job to strip the source English cleanly so no residual
fragment is left behind for the editor to draw over (see m3.remove_text_by_content).
A placement defect (text-on-figure, text-on-rule, text-on-text) is the producer's
to fix by tightening this spec, together with the QA/確認者 role that detects it.

Reuses M1 (m1_analyze) for the heavy lifting (segmentation, column detection,
figure extraction, vector-rule extraction, reading order, cross-page joins) and
adds the lane geometry.
"""
import m1_analyze
from config import OUT

TRANSLATABLE = {"body", "heading", "caption", "title"}
# Text that stays as-is in the output and therefore blocks Japanese reflow.
KEPT_TEXT = {"data", "reference", "running_head", "pagenum"}


def analyze(pdf_path, name, render=False):
    """Run M1 and return the layout dict (also written to <out>/<name>_layout.json)."""
    doc, _ = m1_analyze.analyze_pdf(pdf_path, name, render=render)
    return doc


def _lane_bounds(blocks, lane, page_w):
    bs = [b for b in blocks if b.get("col", 0) == lane]
    if not bs:
        return None
    return min(b["x0"] for b in bs), max(b["x1"] for b in bs)


def lanes_for_page(page):
    """Return {lane: {x0, x1, top, bottom, obstacles}} for lanes 0/1/2 that carry
    translatable text. `top` is where this lane's Japanese starts (the top of its
    first translatable block, so the full-width abstract band stays above the
    columns); `bottom` is the page's text bottom, giving reflow room. `obstacles`
    are y-bands (figures + kept text) the flow must skip."""
    pw, ph = page["width"], page["height"]
    blocks = page["blocks"]
    figs = page.get("figures", [])
    trans = [b for b in blocks if b["type"] in TRANSLATABLE]
    if not trans:
        return {}
    # page text band: ignore margin furniture (running heads / page numbers)
    body_like = [b for b in blocks if b["type"] in TRANSLATABLE or b["type"] in ("data",)]
    text_bottom = max((b["bottom"] for b in body_like), default=ph * 0.92)
    text_bottom = min(text_bottom, ph * 0.94)

    lanes = {}
    for lane in sorted({b.get("col", 0) for b in trans}):
        lb = _lane_bounds(trans, lane, pw)
        if lb is None:
            continue
        x0, x1 = lb
        lane_units = [b for b in trans if b.get("col", 0) == lane]
        top = min(b["top"] for b in lane_units)
        # obstacles whose x-range overlaps this lane
        obstacles = []
        for f in figs:
            if not (f["x1"] <= x0 + 1 or x1 <= f["x0"] + 1):
                obstacles.append((f["top"], f["bottom"], "figure"))
        for b in blocks:
            if b["type"] not in KEPT_TEXT:
                continue
            if b["top"] > text_bottom or b["bottom"] < top:
                continue
            if not (b["x1"] <= x0 + 1 or x1 <= b["x0"] + 1):
                obstacles.append((b["top"], b["bottom"], b["type"]))
        # vector rules crossing this lane - kept in place, so text flows around
        for r in page.get("rules", []):
            if not (r["x1"] <= x0 + 1 or x1 <= r["x0"] + 1):
                obstacles.append((r["top"] - 3, r["bottom"] + 3, "rule"))
        lanes[lane] = {"x0": x0, "x1": x1, "top": top, "bottom": text_bottom,
                       "obstacles": obstacles}
    # Clamp left/right column x-ranges so they never overlap: a block that
    # spilled slightly past the gutter can otherwise widen one lane into the
    # other's territory, and reflow would collide the two columns' text.
    if 1 in lanes and 2 in lanes and lanes[1]["x1"] > lanes[2]["x0"]:
        gutter = (lanes[1]["x1"] + lanes[2]["x0"]) / 2
        lanes[1]["x1"] = min(lanes[1]["x1"], gutter - 3)
        lanes[2]["x0"] = max(lanes[2]["x0"], gutter + 3)
    return lanes


if __name__ == "__main__":
    import argparse, json
    from config import resolve_pdf, ensure_out
    ap = argparse.ArgumentParser(description="PDF Producer: PDF -> layout spec")
    ap.add_argument("inputs", nargs="*", default=["paper"])
    args = ap.parse_args()
    ensure_out()
    for inp in args.inputs:
        path, name = resolve_pdf(inp)
        doc = analyze(path, name)
        for pi, p in enumerate(doc["pages"]):
            lanes = lanes_for_page(p)
            print(f"[{name}] page {pi+1}: lanes="
                  + ", ".join(f"{k}(x{v['x0']:.0f}-{v['x1']:.0f},"
                              f"obst{len(v['obstacles'])})" for k, v in lanes.items()))
