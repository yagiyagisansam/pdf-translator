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
     bands; size the body font to FILL the page - shrink toward `floor` if it
     doesn't fit at CAP, or GROW up to GROW_CAP if a short translation would
     otherwise leave the lower half empty; flow each band with figures, kept text
     and vector rules fixed as obstacles; justify (両端揃え) every non-final line.
  3. Overlay + merge (pypdf-6-safe) onto the stripped pages.

Falls back to the proven per-region engine (m3.generate) when reflow cannot fit
the page even at the floor font, so output is never worse than before.
"""
import json
import re
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import Color
from pypdf import PdfReader, PdfWriter
from pikepdf import Pdf

import m3_generate as m3
from config import OUT, ensure_out
from roles import producer

CAP = 10.5               # preferred body size when a page is already full
GROW_CAP = 15.0          # max body size when GROWING to fill a sparse page
FILL_TARGET = 0.82       # grow the font until the column is at least this full
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
    """(top, bottom) bands in [x0,x1] from figures + kept-language text + vector
    rules. Vector art is kept in place, so a horizontal rule (abstract-box border,
    section separator, table rule) that crosses this column becomes a small
    obstacle band the flow skips - Japanese is never drawn across a line."""
    obs = []
    for f in page.get("figures", []):
        if _overlaps(x0, x1, f["x0"], f["x1"]):
            obs.append((f["top"] - 6, f["bottom"] + 6))
    for b in page["blocks"]:
        # kept-language text, OR a translatable block whose translation is missing
        # so its ENGLISH still sits on the page: both must block the flow, else the
        # reflowed Japanese (e.g. overflow from the other column) is drawn on top of
        # surviving English -> the "overlay" seen when translation partially fails.
        if (b["type"] in KEPT or b.get("_keep_en")) and \
                _overlaps(x0, x1, b["x0"], b["x1"]):
            obs.append((b["top"] - 2, b["bottom"] + 2))
    for r in page.get("rules", []):
        if _overlaps(x0, x1, r["x0"], r["x1"]):
            obs.append((r["top"] - 3, r["bottom"] + 3))
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


def _unit_size(u, factor):
    """Font size for a unit: its SOURCE block size scaled by the page factor.
    Reflow used to draw the whole page at ONE size, which meant the tightest
    band (a 9pt footnote zone) forced the entire page to the floor size. Per-
    unit sizes keep the source hierarchy (20pt heading / 12pt body / 9pt
    footnote) and let each band fit its own slot."""
    return max(4.0, min(42.0, (u.get("_size") or 10.0) * factor))


def _unit_lines(units, factor, width, font_of):
    """Flat [(text|None, font, justify, color, size)] for a run of units, with
    paragraph/heading gaps. `justify` is True on every wrapped line EXCEPT the
    last line of a paragraph (which stays ragged, like normal typesetting) and
    headings/titles (short, left-aligned). Gap rows carry justify=False."""
    out = []
    for k, u in enumerate(units):
        if not u.get("target"):
            continue
        font = font_of(u)
        color = tuple(u.get("_color") or (0, 0, 0))
        size = _unit_size(u, factor)
        if out:
            gap = HEAD_GAP if u["type"] in ("heading", "title") else PARA_GAP
            for _ in range(max(1, round(gap))):
                out.append((None, font, False, color, size))
        wrapped = m3._wrap(u["target"], font, size, width)
        is_head = u["type"] in ("heading", "title")
        for j, ln in enumerate(wrapped):
            last = j == len(wrapped) - 1
            out.append((ln, font, (not is_head) and (not last), color, size))
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


def _flow_column(lines, x0, width, y, y_bottom, bands):
    """Place lines from y downward, skipping obstacle bands. Line height varies
    per row (units keep their own size). Returns (draws, y_end, remainder)."""
    draws = []
    for i, (txt, font, justify, color, size) in enumerate(lines):
        lh = size * LR
        while True:
            hit = next(((t, b) for (t, b) in bands if y < b and y + lh > t), None)
            if hit is None:
                break
            y = hit[1]
        if y + lh > y_bottom + 0.1:
            return draws, y, lines[i:]
        if txt is not None:
            draws.append({"x": x0, "y_top": y, "size": size, "font": font,
                          "line": txt, "width": width, "justify": justify,
                          "color": color})
        y += lh
    return draws, y, []


def _page_geom(page):
    trans = [b for b in page["blocks"] if b["type"] in TRANS]
    if not trans:
        return None
    body = trans + [b for b in page["blocks"] if b["type"] == "data"]
    # The flow band's x-range comes from the SUBSTANTIAL translatable blocks
    # only: a margin artifact (a section number "3.1" in the left margin, a
    # side caption) must not widen the main flow to the page edge. Substantial
    # = at least 45% as wide as the widest translatable block.
    wmax = max(b["x1"] - b["x0"] for b in trans)
    main = [b for b in trans if (b["x1"] - b["x0"]) >= 0.45 * wmax] or trans
    Lx0 = min(b["x0"] for b in main); Lx1 = max(b["x1"] for b in main)
    top = min(b["top"] for b in trans)
    bottom = min(max(b["bottom"] for b in body), page["height"] * 0.94)
    # a translatable block anchored BELOW the clamped bottom must still have
    # room to draw, or it becomes a permanent 1-line overflow on every page
    bottom = max(bottom, max(b["bottom"] for b in trans) + 2)
    bottom = min(bottom, page["height"] * 0.985)
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


def _flow_in_own_box(u, factor, page, taken, font_of):
    """Place a MARGIN unit (side caption, note outside the main column) inside
    its own source bbox: same x/width/top as the source, flowing down and
    skipping figures/kept text - never through the main flow band. `taken`
    accumulates y-bands already used by earlier margin units in the same lane
    so two captions can never overlap. Returns draws."""
    x0, x1 = u["_x0"], u["_x1"]
    size = _unit_size(u, factor)
    lh = size * LR
    font = font_of(u)
    color = tuple(u.get("_color") or (0, 0, 0))
    bands = list(_obstacles_for(page, x0, x1))
    for (tx0, tx1, tt, tb) in taken:
        if not (tx1 <= x0 or tx0 >= x1):
            bands.append((tt, tb))
    # a THIN band (a table row rule at the cell's edge) must not push the
    # cell's first line out of its row - only substantial obstacles move text
    bands = [(t, bo) for (t, bo) in bands
             if not (bo - t <= 12.0 and t < u["_top"] + lh and bo > u["_top"] - 2)]
    bands = _merged_bands(bands)
    width = max(12.0, x1 - x0)
    wrapped = m3._wrap(u["target"], font, size, width)
    draws = []
    y = u["_top"]
    bottom = page["height"] * 0.96
    for ln in wrapped:
        while True:
            hit = next(((t, b) for (t, b) in bands if y < b and y + lh > t), None)
            if hit is None:
                break
            y = hit[1]
        if y + lh > bottom:
            break
        draws.append({"x": x0, "y_top": y, "size": size, "font": font,
                      "line": ln, "width": width, "justify": False,
                      "color": color})
        y += lh
    if draws:
        taken.append((x0, x1, u["_top"] - 1, y + 1))
    return draws


def _layout_page(page, page_units, factor, font_of):
    """Place all page units at scale `factor` (1.0 = source font sizes).
    Returns (draws, overflow_lines)."""
    g = _page_geom(page)
    if g is None:
        return [], 0
    y = g["top"]
    draws = []
    overflow = 0
    # margin units (side captions) and RECORD-ROW units (a TOC row, a table
    # cell/row - anything whose meaning depends on row alignment with kept
    # numbers) keep their own box; the rest flows in bands
    flow_units, margin_units = [], []
    for u in page_units:
        ov = min(u["_x1"], g["Lx1"]) - max(u["_x0"], g["Lx0"])
        if u.get("_record") or ov < 0.3 * max(u["_x1"] - u["_x0"], 1.0):
            margin_units.append(u)
        else:
            flow_units.append(u)
    taken = []
    for u in sorted(margin_units, key=lambda u: u["_top"]):
        draws += _flow_in_own_box(u, factor, page, taken, font_of)
    full_obs = _obstacles_for(page, g["Lx0"], g["Lx1"])
    lh_page = max(6.0, (page.get("body_size") or 10) * factor) * LR
    for kind, payload in _bands_for_page(flow_units):
        # VERTICAL ANCHOR (layout fidelity): a band never starts ABOVE its
        # source position. Japanese that runs shorter than the English would
        # otherwise pull everything below it upward - a bottom-anchored
        # footnote or a lower section migrating to the top of the page.
        units_in_band = payload if kind == "full" else \
            sorted(payload[1] + payload[2], key=lambda u: u["uid"])
        if not units_in_band:
            continue
        band_top = min((u.get("_top", y) for u in units_in_band), default=y)
        y = max(y, band_top)
        if kind == "full":
            lines = _unit_lines(payload, factor, g["Lx1"] - g["Lx0"], font_of)
            d, y, rem = _flow_column(lines, g["Lx0"], g["Lx1"] - g["Lx0"], y,
                                     g["bottom"], full_obs)
            draws += d; overflow += len(rem); y += lh_page * PARA_GAP
        else:
            # Newspaper-style balanced two-column flow: combine BOTH lanes in
            # reading order into one stream, fill the LEFT column top-to-bottom
            # (skipping obstacles), then continue the overflow at the top of the
            # RIGHT column. Without this, each lane flowed independently and the
            # (longer) Japanese overfilled the left column while the right sat
            # nearly empty.
            L, R = g["left"], g["right"]
            units = sorted(payload[1] + payload[2], key=lambda u: u["uid"])
            if not (L and R):
                # single usable column: flow everything down it
                col = L or R
                if col is None:
                    overflow += len(units); continue
                w = col["x1"] - col["x0"]
                lines = _unit_lines(units, factor, w, font_of)
                bands = _obstacles_for(page, col["x0"], col["x1"])
                d, y, rem = _flow_column(lines, col["x0"], w, y, g["bottom"], bands)
                draws += d; overflow += len(rem); y = g["bottom"] + lh_page * PARA_GAP
                continue
            wL, wR = L["x1"] - L["x0"], R["x1"] - R["x0"]
            if min(wL, wR) / max(wL, wR) < 0.7:
                # very unequal lanes are INDEPENDENT columns (a sidebar next to
                # the main content), not a newspaper flow - balancing them would
                # wrap the wide lane's text at the narrow lane's width and spill
                # units into the wrong lane. Flow each lane's own units in place.
                for lane, col in ((1, L), (2, R)):
                    lus = sorted(payload[lane], key=lambda u: u["uid"])
                    if not lus:
                        continue
                    wl = col["x1"] - col["x0"]
                    bands = _obstacles_for(page, col["x0"], col["x1"])
                    ly = g["top"]
                    for u in lus:
                        ly = max(ly, u.get("_top", ly))   # per-unit anchor
                        lines = _unit_lines([u], factor, wl, font_of)
                        d, ly, rem = _flow_column(lines, col["x0"], wl, ly,
                                                  g["bottom"], bands)
                        draws += d; overflow += len(rem)
                        ly += _unit_size(u, factor) * LR * PARA_GAP
                    y = max(y, ly)
                y += lh_page * PARA_GAP
                continue
            w = min(wL, wR)                # wrap to the narrower
            lines = _unit_lines(units, factor, w, font_of)
            Lbands = _obstacles_for(page, L["x0"], L["x1"])
            Rbands = _obstacles_for(page, R["x0"], R["x1"])
            Lcap = _capacity(y, g["bottom"], Lbands, lh_page)
            Rcap = _capacity(y, g["bottom"], Rbands, lh_page)
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
                                    g["bottom"], Lbands)
            draws += d
            d, yr, rem = _flow_column(lines[target_left:], R["x0"], w, y,
                                      g["bottom"], Rbands)
            draws += d; overflow += len(rem)
            y = max(yl, yr) + lh_page * PARA_GAP
    return draws, overflow


def _reflow(layout, units, floor, skip_pages=frozenset()):
    """Whole-document reflow. Returns (per_page_draws, total_overflow).

    Every unit is drawn at its SOURCE font size scaled by a per-page factor
    (1.0 = exact source sizes, the fidelity target). If a page's Japanese does
    not fit, the factor shrinks in small steps toward a floor. Sizing is
    per-unit, so a tight 9pt-footnote zone shrinks only itself - it no longer
    drags a whole page of 12pt body text down to the floor size."""
    by_page = {}
    for u in units:
        if not u.get("target"):
            continue
        spi, sbi = map(int, u["spans"][0].split(":"))
        blk = layout["pages"][spi]["blocks"][sbi]
        u["_lane"] = blk.get("col", 0)
        u["_top"] = blk.get("top", 0.0)
        u["_color"] = blk.get("color")
        u["_record"] = bool(blk.get("record"))
        import statistics as _st
        bs = []
        for s in u["spans"]:
            p2, b2 = map(int, s.split(":"))
            bb = layout["pages"][p2]["blocks"][b2]
            if bb.get("size"):
                bs.append(bb["size"])
        u["_size"] = _st.median(bs) if bs else (layout["pages"][spi].get("body_size") or 10)
        fb = layout["pages"][spi]["blocks"][sbi]
        u["_x0"] = fb.get("x0", 0.0)
        u["_x1"] = fb.get("x1", 0.0)
        by_page.setdefault(spi, []).append(u)
    per_page = {pi: [] for pi in range(len(layout["pages"]))}
    total_overflow = 0
    for pi, page in enumerate(layout["pages"]):
        if pi in skip_pages:
            continue                       # mostly-untranslated: leave English
        pu = by_page.get(pi, [])
        if not pu:
            continue
        bs = page.get("body_size") or 10
        factor_min = max(0.45, floor / bs)
        best = None
        factor = 1.0
        while factor >= factor_min:
            draws, ov = _layout_page(page, pu, factor, _font_of)
            best = draws
            if ov == 0:
                break
            factor -= 0.07
        else:
            draws, ov = _layout_page(page, pu, factor_min, _font_of)
            best = draws
            total_overflow += ov
        per_page[pi] = best
    return per_page, total_overflow


def _font_of(u):
    return "NotoJP-Bold" if u["type"] in ("heading", "title") else "NotoJP"


def _is_flowing_doc(layout):
    """True when the document's translatable text is dominated by flowing
    multi-line paragraphs (a paper/report) - the only shape column reflow is
    valid for. Scattered short blocks (brochures, posters, forms) must keep
    per-block positions instead - and so must PHOTO-DOMINATED documents
    (brochures whose pages are mostly images): their text is design elements
    anchored to the artwork, not a flowing column."""
    import statistics
    covs = []
    for p in layout["pages"]:
        area = (p["width"] or 1) * (p["height"] or 1)
        fa = sum(max(0.0, f["x1"] - f["x0"]) * max(0.0, f["bottom"] - f["top"])
                 for f in p.get("figures", []))
        covs.append(min(1.0, fa / area))
    if covs and statistics.median(covs) >= 0.30:
        return False
    tot = flowing = 0
    for p in layout["pages"]:
        for b in p["blocks"]:
            if b["type"] in TRANS:
                n = len(b.get("text", ""))
                tot += n
                if b.get("nlines", 1) >= 3:
                    flowing += n
    return tot > 0 and flowing / tot >= 0.5


def build(name, src_path, floor=6.0):
    ensure_out()
    m3._register_fonts()
    layout = json.load(open(f"{OUT}/{name}_layout.json"))
    units = json.load(open(f"{OUT}/{name}_bilingual.json"))

    # Slide decks (any landscape page) and poster/brochure-style documents don't
    # fit the paper reflow band model - their text boxes are scattered islands
    # and each one must stay at its own position. Place each unit across its own
    # block regions with the per-region engine, which keeps text at its original
    # position (and, per unit, at its original font size). Reflow is only for
    # documents whose translatable text is dominated by flowing multi-line
    # paragraphs (papers, reports).
    if any(p["width"] > p["height"] for p in layout["pages"]) \
            or not _is_flowing_doc(layout):
        out_path, stripped = m3.generate(name, src_path)
        placed = json.load(open(f"{OUT}/{name}_placed.json"))
        return {"out_path": out_path, "stripped": stripped, "mode": "region",
                "pages": len(layout["pages"]), "placed": placed, "overflow": []}

    # 1) strip original translatable text (font-decoded, content-based)
    unit_for_block = {}
    for u in units:
        if u.get("target"):
            for sid in u["spans"]:
                unit_for_block[sid] = u
    # Per-page translation coverage (by translatable character count). If a page
    # is MOSTLY untranslated - e.g. the free engine broke the citation tokens on
    # most of its paragraphs - reflowing the few translated bits into the gaps
    # around a wall of surviving English produces an unreadable pile. Leaving that
    # page entirely in its original English is far better, so such pages are
    # skipped: not stripped, not reflowed.
    MIN_PAGE_COV = 0.5
    skip_pages = set()
    for pi, p in enumerate(layout["pages"]):
        tot = sum(len(b["text"]) for bi, b in enumerate(p["blocks"])
                  if b["type"] in TRANS)
        cov = sum(len(b["text"]) for bi, b in enumerate(p["blocks"])
                  if b["type"] in TRANS and f"{pi}:{bi}" in unit_for_block)
        if tot and cov / tot < MIN_PAGE_COV:
            skip_pages.add(pi)

    kill, kill_drop = {}, {}
    for pi, p in enumerate(layout["pages"]):
        kill[pi] = kill_drop[pi] = ""
        if pi in skip_pages:
            continue                       # leave this page's English untouched
        for bi, b in enumerate(p["blocks"]):
            if b["type"] in TRANS and f"{pi}:{bi}" in unit_for_block:
                kill[pi] += m3._norm_txt(b["text"])
                kill_drop[pi] += m3._norm_txt_drop(b["text"])
            elif b["type"] in TRANS and re.search(r"[A-Za-z]", b["text"]):
                # translatable but NOT covered by a translated unit: its English
                # stays on the page, so mark it so the reflow treats it as an
                # obstacle and never draws Japanese over it. (No-letter blocks -
                # bare bullet markers, symbols - are NOT obstacles: a "•" must
                # not cut a whole flow lane in half at its y.)
                b["_keep_en"] = True
    pdf = Pdf.open(src_path)
    for pi, page in enumerate(pdf.pages):
        if kill.get(pi):
            m3.remove_text_by_content(
                page, pdf, kill[pi], kill_blob_drop=kill_drop[pi],
                keep_tokens=m3.keep_tokens_for(layout["pages"][pi], pi,
                                               unit_for_block))
    stripped = f"{OUT}/{name}_stripped.pdf"
    pdf.save(stripped); pdf.close()

    # 2) reflow (skip the mostly-untranslated pages so they stay original English)
    per_page, overflow = _reflow(layout, units, floor, skip_pages)

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
        for d in per_page.get(pi, []):
            c.setFillColor(Color(*(d.get("color") or (0, 0, 0))))
            cs = _justify_amount(d)
            # pdfplumber x is ALREADY absolute page space (it includes the media
            # box left offset), and the overlay merges into that same space, so we
            # must NOT subtract xo here - doing so shoved every line left by the
            # mediabox origin (e.g. 42pt) on PDFs whose mediabox is not at x=0,
            # which read as the whole body being "左寄り". y already omits yo for
            # the same reason, so x now matches y.
            t = c.beginText(d["x"], ph - d["y_top"] - d["size"])
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
    return {"out_path": out_path, "stripped": stripped, "mode": "reflow",
            "pages": len(layout["pages"]), "placed": placed,
            "skip_pages": sorted(pi + 1 for pi in skip_pages),
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
