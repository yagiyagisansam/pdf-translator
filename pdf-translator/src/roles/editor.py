#!/usr/bin/env python3
"""編集者 (Editor) - full column reflow.

Figures/tables keep their ORIGINAL absolute positions; the Japanese body is
REFLOWED by reading order into the page's structure (full-width bands for
title/authors/abstract, two-column bands for the body), flowing top-to-bottom
and skipping obstacle y-bands (figures and kept-language text). This is the
reference-goal reconstruction, not one-box-per-English-line placement.

Steps:
  1. Strip the original translatable text in place (content-based, font-decoded
     - reuses m3_generate; kept text like tables/refs/headers survives).
  2. Per page: group the page's units (reading order) into full-width / two-column
     bands; pick the largest page font (cap..floor) that fits; flow each band,
     figures fixed as obstacles.
  3. Overlay + merge (pypdf-6-safe) onto the stripped pages.

Falls back to the proven per-region engine (m3.generate) when reflow cannot fit
the page even at the floor font, so output is never worse than before.
"""
import json
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import Color
from pypdf import PdfReader, PdfWriter
from pikepdf import Pdf

import m3_generate as m3
from config import OUT, ensure_out
from roles import producer

CAP = 10.5
LR = 1.16                # line-height ratio
PARA_GAP = 0.5           # blank line-heights between units
HEAD_GAP = 0.9           # extra before a heading
KEPT = {"data", "reference", "running_head", "pagenum"}
TRANS = producer.TRANSLATABLE


def _overlaps(a0, a1, b0, b1):
    return not (a1 <= b0 + 1 or b1 <= a0 + 1)


def _merged_bands(obstacles):
    bands = sorted((t, b) for (t, b) in obstacles)
    out = []
    for t, b in bands:
        if out and t <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((t, b))
    return out


def _obstacles_for(page, x0, x1):
    """(top, bottom) bands in [x0,x1] from figures + kept-language text."""
    obs = []
    for f in page.get("figures", []):
        if _overlaps(x0, x1, f["x0"], f["x1"]):
            obs.append((f["top"] - 6, f["bottom"] + 6))
    for b in page["blocks"]:
        if b["type"] in KEPT and _overlaps(x0, x1, b["x0"], b["x1"]):
            obs.append((b["top"] - 2, b["bottom"] + 2))
    return _merged_bands(obs)


def _justify_amount(d):
    """Char spacing (両端揃え) that stretches a justified line's right edge to the
    column edge, or None if the line should stay ragged. Only full-ish lines are
    justified, and the stretch is capped so a nearly-empty line is never blown
    apart into gappy characters."""
    if not d.get("justify") or d.get("width") is None:
        return None
    n = len(d["line"])
    if n < 2:
        return None
    nat = stringWidth(d["line"], d["font"], d["size"])
    slack = d["width"] - nat
    if slack <= 0 or nat < 0.6 * d["width"]:
        return None
    cs = slack / (n - 1)
    if cs > d["size"] * 0.6:            # too sparse -> leave ragged
        return None
    return cs


def _unit_lines(units, size, width, font_of):
    """Flat [(text|None, font, justify)] for a run of units, with paragraph/
    heading gaps. `justify` is True on every wrapped line EXCEPT the last line of
    a paragraph (which stays ragged, like normal typesetting) and headings/titles
    (short, left-aligned). Gap rows carry justify=False."""
    out = []
    for k, u in enumerate(units):
        if not u.get("target"):
            continue
        font = font_of(u)
        if out:
            gap = HEAD_GAP if u["type"] in ("heading", "title") else PARA_GAP
            for _ in range(max(1, round(gap))):
                out.append((None, font, False))
        wrapped = m3._wrap(u["target"], font, size, width)
        is_head = u["type"] in ("heading", "title")
        for j, ln in enumerate(wrapped):
            last = j == len(wrapped) - 1
            out.append((ln, font, (not is_head) and (not last)))
    return out


def _capacity(y, y_bottom, bands, lh):
    """How many lines of height lh fit from y to y_bottom, skipping obstacles."""
    n = 0
    while y + lh <= y_bottom + 0.1:
        hit = next(((t, b) for (t, b) in bands if y < b and y + lh > t), None)
        if hit:
            y = hit[1]; continue
        n += 1; y += lh
    return n


def _flow_column(lines, x0, width, y, y_bottom, bands, lh):
    """Place lines from y downward, skipping obstacle bands. Returns
    (draws, y_end, remainder_lines)."""
    draws = []
    for i, (txt, font, justify) in enumerate(lines):
        while True:
            hit = next(((t, b) for (t, b) in bands if y < b and y + lh > t), None)
            if hit is None:
                break
            y = hit[1]
        if y + lh > y_bottom + 0.1:
            return draws, y, lines[i:]
        if txt is not None:
            draws.append({"x": x0, "y_top": y, "size": None, "font": font,
                          "line": txt, "width": width, "justify": justify})
        y += lh
    return draws, y, []


def _page_geom(page):
    body = [b for b in page["blocks"] if b["type"] in TRANS or b["type"] == "data"]
    if not body:
        return None
    Lx0 = min(b["x0"] for b in body); Lx1 = max(b["x1"] for b in body)
    top = min(b["top"] for b in page["blocks"] if b["type"] in TRANS)
    bottom = min(max(b["bottom"] for b in body), page["height"] * 0.94)
    lanes = producer.lanes_for_page(page)
    left = lanes.get(1); right = lanes.get(2)
    return {"Lx0": Lx0, "Lx1": Lx1, "top": top, "bottom": bottom,
            "left": left, "right": right}


def _bands_for_page(page_units):
    """Group page units (reading order) into ('full',[u]) / ('cols',{1:[],2:[]})."""
    bands = []
    i = 0
    us = sorted(page_units, key=lambda u: u["uid"])
    while i < len(us):
        if us[i]["_lane"] == 0:
            bands.append(("full", [us[i]])); i += 1
        else:
            grp = {1: [], 2: []}
            while i < len(us) and us[i]["_lane"] in (1, 2):
                grp[us[i]["_lane"]].append(us[i]); i += 1
            bands.append(("cols", grp))
    return bands


def _layout_page(page, page_units, size, font_of):
    """Try to place all page units at `size`. Returns (draws, overflow_lines)."""
    g = _page_geom(page)
    if g is None:
        return [], 0
    lh = size * LR
    y = g["top"]
    draws = []
    overflow = 0
    full_obs = _obstacles_for(page, g["Lx0"], g["Lx1"])
    for kind, payload in _bands_for_page(page_units):
        if kind == "full":
            lines = _unit_lines(payload, size, g["Lx1"] - g["Lx0"], font_of)
            d, y, rem = _flow_column(lines, g["Lx0"], g["Lx1"] - g["Lx0"], y,
                                     g["bottom"], full_obs, lh)
            draws += d; overflow += len(rem); y += lh * PARA_GAP
        else:
            # Newspaper-style balanced two-column flow: combine BOTH lanes in
            # reading order into one stream, fill the LEFT column top-to-bottom
            # (skipping obstacles), then continue the overflow at the top of the
            # RIGHT column. Without this, each lane flowed independently and the
            # (longer) Japanese overfilled the left column while the right sat
            # nearly empty.
            L, R = g["left"], g["right"]
            if not (L and R):
                # single usable column: flow everything down it
                col = L or R
                units = sorted(payload[1] + payload[2], key=lambda u: u["uid"])
                w = col["x1"] - col["x0"]
                lines = _unit_lines(units, size, w, font_of)
                bands = _obstacles_for(page, col["x0"], col["x1"])
                d, y, rem = _flow_column(lines, col["x0"], w, y, g["bottom"], bands, lh)
                draws += d; overflow += len(rem); y = g["bottom"] + lh * PARA_GAP
                continue
            units = sorted(payload[1] + payload[2], key=lambda u: u["uid"])
            w = min(L["x1"] - L["x0"], R["x1"] - R["x0"])   # wrap to the narrower
            lines = _unit_lines(units, size, w, font_of)
            Lbands = _obstacles_for(page, L["x0"], L["x1"])
            Rbands = _obstacles_for(page, R["x0"], R["x1"])
            Lcap = _capacity(y, g["bottom"], Lbands, lh)
            Rcap = _capacity(y, g["bottom"], Rbands, lh)
            # BALANCE: split by column capacity so both columns fill to roughly
            # equal height, instead of packing the left column full and leaving
            # the right empty. The right column may hold less (e.g. a table), so
            # weight the split by each column's capacity.
            total = len(lines)
            if Lcap + Rcap > 0:
                target_left = min(Lcap, round(total * Lcap / (Lcap + Rcap)))
                if total - target_left > Rcap:      # push extra left if right can't hold it
                    target_left = min(Lcap, total - Rcap)
            else:
                target_left = 0
            d, yl, _ = _flow_column(lines[:target_left], L["x0"], w, y,
                                    g["bottom"], Lbands, lh)
            draws += d
            d, yr, rem = _flow_column(lines[target_left:], R["x0"], w, y,
                                      g["bottom"], Rbands, lh)
            draws += d; overflow += len(rem)
            y = max(yl, yr) + lh * PARA_GAP
    for d in draws:
        d["size"] = size
    return draws, overflow


def _reflow(layout, units, floor):
    """Whole-document reflow. Returns (per_page_draws, total_overflow)."""
    # tag each unit with its lane (first block's column) and home page
    by_page = {}
    for u in units:
        if not u.get("target"):
            continue
        spi, sbi = map(int, u["spans"][0].split(":"))
        u["_lane"] = layout["pages"][spi]["blocks"][sbi].get("col", 0)
        by_page.setdefault(spi, []).append(u)
    per_page = {pi: [] for pi in range(len(layout["pages"]))}
    total_overflow = 0
    for pi, page in enumerate(layout["pages"]):
        pu = by_page.get(pi, [])
        if not pu:
            continue
        best = None
        size = CAP
        while size >= floor:
            draws, ov = _layout_page(page, pu, size, _font_of)
            if ov == 0:
                best = draws; break
            size -= 0.5
        if best is None:
            draws, ov = _layout_page(page, pu, floor, _font_of)
            best = draws; total_overflow += ov
        per_page[pi] = best
    return per_page, total_overflow


def _font_of(u):
    return "NotoJP-Bold" if u["type"] in ("heading", "title") else "NotoJP"


def build(name, src_path, floor=6.0):
    ensure_out()
    m3._register_fonts()
    layout = json.load(open(f"{OUT}/{name}_layout.json"))
    units = json.load(open(f"{OUT}/{name}_bilingual.json"))

    # Slide decks (any landscape page) don't fit the paper reflow band model -
    # their text boxes are scattered and each slide is independent. Place each
    # unit across its own block regions with the proven per-region engine, which
    # keeps text near its original position and spreads multi-page units onto the
    # right pages.
    if any(p["width"] > p["height"] for p in layout["pages"]):
        out_path, stripped = m3.generate(name, src_path)
        placed = json.load(open(f"{OUT}/{name}_placed.json"))
        return {"out_path": out_path, "stripped": stripped,
                "pages": len(layout["pages"]), "placed": placed, "overflow": []}

    # 1) strip original translatable text (font-decoded, content-based)
    unit_for_block = {}
    for u in units:
        if u.get("target"):
            for sid in u["spans"]:
                unit_for_block[sid] = u
    kill, kill_drop = {}, {}
    for pi, p in enumerate(layout["pages"]):
        kill[pi] = kill_drop[pi] = ""
        for bi, b in enumerate(p["blocks"]):
            if b["type"] in TRANS and f"{pi}:{bi}" in unit_for_block:
                kill[pi] += m3._norm_txt(b["text"])
                kill_drop[pi] += m3._norm_txt_drop(b["text"])
    pdf = Pdf.open(src_path)
    for pi, page in enumerate(pdf.pages):
        if kill.get(pi):
            m3.remove_text_by_content(page, pdf, kill[pi], kill_blob_drop=kill_drop[pi])
    stripped = f"{OUT}/{name}_stripped.pdf"
    pdf.save(stripped); pdf.close()

    # 2) reflow
    per_page, overflow = _reflow(layout, units, floor)

    placed = {}
    for pi, draws in per_page.items():
        placed[str(pi + 1)] = [
            {"x0": d["x"], "top": d["y_top"],
             "x1": d["x"] + (d["width"] if _justify_amount(d) is not None
                             else stringWidth(d["line"], d["font"], d["size"])),
             "bottom": d["y_top"] + d["size"] * LR, "text": d["line"][:40]}
            for d in draws]
    json.dump(placed, open(f"{OUT}/{name}_placed.json", "w"))

    # 3) overlay + merge
    rd = PdfReader(src_path)
    sizes = [(float(p.mediabox.width), float(p.mediabox.height),
              float(p.mediabox.left), float(p.mediabox.bottom)) for p in rd.pages]
    overlay = f"{OUT}/{name}_overlay.pdf"
    c = None
    for pi in range(len(sizes)):
        pw, ph, xo, yo = sizes[pi]
        c = canvas.Canvas(overlay, pagesize=(pw, ph)) if c is None else c
        if pi:
            c.setPageSize((pw, ph))
        c.setFillColor(Color(0, 0, 0))
        for d in per_page.get(pi, []):
            cs = _justify_amount(d)
            t = c.beginText(d["x"] - xo, ph - d["y_top"] - d["size"])
            t.setFont(d["font"], d["size"])
            t.setCharSpace(cs if cs is not None else 0)
            t.textLine(d["line"])
            c.drawText(t)
        c.showPage()
    c.save()
    over = PdfReader(overlay); w = PdfWriter(); w.append(PdfReader(stripped))
    for i, page in enumerate(w.pages):
        if i < len(over.pages):
            page.merge_page(over.pages[i])
    out_path = f"{OUT}/{name}_ja.pdf"
    with open(out_path, "wb") as f:
        w.write(f)
    return {"out_path": out_path, "stripped": stripped,
            "pages": len(layout["pages"]), "placed": placed,
            "overflow": [("total", overflow)] if overflow else []}


if __name__ == "__main__":
    import argparse
    from config import resolve_pdf
    ap = argparse.ArgumentParser(description="Editor role: reflow Japanese PDF")
    ap.add_argument("input", nargs="?", default="paper")
    ap.add_argument("--name")
    args = ap.parse_args()
    src, name = resolve_pdf(args.input)
    rep = build(args.name or name, src)
    print(f"[{name}] -> {rep['out_path']}  overflow={rep['overflow']}")
