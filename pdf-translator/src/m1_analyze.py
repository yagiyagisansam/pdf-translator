#!/usr/bin/env python3
# Milestone 1: Layout analysis engine
import os, re, json, statistics
import pdfplumber
from PIL import Image, ImageDraw, ImageFont

from config import OUT, ensure_out, resolve_pdf

DPI = 150
SCALE = DPI / 72.0

TYPE_COLORS = {
    "title":       (220, 30, 30),
    "heading":     (240, 140, 0),
    "body":        (30, 90, 220),
    "caption":     (20, 160, 60),
    "figure":      (200, 30, 200),
    "running_head":(130, 130, 130),
    "pagenum":     (130, 130, 130),
    "reference":   (0, 150, 160),
    "data":        (150, 90, 30),
}

def load_label_font(sz):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]:
        if os.path.exists(p):
            return ImageFont.truetype(p, sz)
    return ImageFont.load_default()

def _cluster_rows(chars):
    """Group y-sorted chars into visual rows. A char joins the current row when its
    vertical CENTER is within 0.6x the (smaller) char height of the row's median
    center. The reference is the row's MEDIAN center - stable within a line - not
    an accumulated bounding box, so tight line spacing can't chain successive
    lines into one giant row (which would interleave their characters by x when
    sorted). Different-size adjacent lines still separate (their centers differ by
    more than the tolerance), and raised superscripts still join their base line
    (their center stays within tolerance)."""
    import statistics as _st
    rows = []
    cur = [chars[0]]
    cy = (chars[0]["top"] + chars[0]["bottom"]) / 2
    ch_med = chars[0]["bottom"] - chars[0]["top"]
    for c in chars[1:]:
        c_cy = (c["top"] + c["bottom"]) / 2
        h = min(ch_med, c["bottom"] - c["top"]) or ch_med
        if abs(c_cy - cy) <= 0.6 * h:
            cur.append(c)
            cy = _st.median((x["top"] + x["bottom"]) / 2 for x in cur)
            ch_med = _st.median(x["bottom"] - x["top"] for x in cur)
        else:
            rows.append(cur)
            cur = [c]; cy = c_cy; ch_med = c["bottom"] - c["top"]
    rows.append(cur)
    return rows

def cluster_lines(chars, gutter=None):
    """Cluster chars into rows by y, then split rows into line-segments by big x-gaps (columns).
    If a gutter x is given, force a split there so two-column rows separate cleanly."""
    chars = [c for c in chars if c.get("text", "").strip() != ""]
    if not chars:
        return []
    widths = [c["x1"] - c["x0"] for c in chars]
    mw = statistics.median(widths) or 4.0
    chars.sort(key=lambda c: (round(c["top"], 1), c["x0"]))
    rows = _cluster_rows(chars)

    lines = []
    gap_thresh = mw * 3.0
    for row in rows:
        row.sort(key=lambda c: c["x0"])
        seg = [row[0]]
        for c in row[1:]:
            crosses_gutter = (gutter is not None and seg[-1]["x1"] <= gutter <= c["x0"])
            if crosses_gutter or (c["x0"] - seg[-1]["x1"] > gap_thresh):
                lines.append(_mkline(seg, mw)); seg = [c]
            else:
                seg.append(c)
        lines.append(_mkline(seg, mw))
    return lines

def find_gutter(chars, page_w, x0=0):
    """Find a vertical whitespace band (column gutter) by scanning an x-coverage histogram.
    Robust to mixed pages: full-width rows (title/abstract) are excluded so they don't
    mask the gutter. Returns gutter center x, or None if single column."""
    chars = [c for c in chars if c.get("text", "").strip()]
    if len(chars) < 40:
        return None
    xs = [c["x0"] for c in chars]; xe = [c["x1"] for c in chars]
    lo, hi = min(xs), max(xe)
    width = hi - lo
    if width < 100:
        return None
    mid_guess = (lo + hi) / 2

    # Group chars into rows; drop rows whose span crosses the center (full-width lines)
    chars.sort(key=lambda c: (round(c["top"], 1), c["x0"]))
    rows = _cluster_rows(chars)

    band = width * 0.04
    col_chars = []
    for row in rows:
        rl = min(c["x0"] for c in row); rr = max(c["x1"] for c in row)
        # a full-width row spans across the center with no internal gutter -> skip it
        if rl < mid_guess - band and rr > mid_guess + band:
            # check if the row itself has a big internal gap (then it's 2-col, keep)
            row_sorted = sorted(row, key=lambda c: c["x0"])
            biggap = max((row_sorted[i+1]["x0"] - row_sorted[i]["x1"]
                          for i in range(len(row_sorted)-1)), default=0)
            if biggap < width * 0.03:
                continue
        col_chars.extend(row)
    if len(col_chars) < 40:
        col_chars = chars  # fallback

    nb = 120
    bw = width / nb
    cov = [0] * nb
    for c in col_chars:
        a = int((c["x0"] - lo) / bw); b = int((c["x1"] - lo) / bw)
        for i in range(max(0, a), min(nb, b + 1)):
            cov[i] += 1
    center_lo, center_hi = int(nb * 0.30), int(nb * 0.70)
    best = None
    i = center_lo
    while i < center_hi:
        if cov[i] == 0:
            j = i
            while j < center_hi and cov[j] == 0:
                j += 1
            run = j - i
            if run >= 2:
                mid = lo + (i + j) / 2 * bw
                left = sum(1 for c in col_chars if c["x1"] < mid)
                right = sum(1 for c in col_chars if c["x0"] > mid)
                if left > len(col_chars) * 0.2 and right > len(col_chars) * 0.2:
                    if best is None or run > best[1]:
                        best = (mid, run)
            i = j
        else:
            i += 1
    return best[0] if best else None

def _mkline(seg, mw):
    seg.sort(key=lambda c: c["x0"])
    # Word-gap threshold scaled by THIS segment's median char width, not the
    # page-wide one: an 8pt caption line on a 10pt page otherwise loses its
    # inter-word spaces entirely.
    seg_w = statistics.median([c["x1"] - c["x0"] for c in seg]) or mw
    gap = min(mw, seg_w) * 0.4
    text = ""
    prev = None
    for c in seg:
        if prev is not None and c["x0"] - prev["x1"] > gap:
            text += " "
        text += c["text"]
        prev = c
    sizes = [c.get("size", 0) for c in seg if c.get("size")]
    fonts = [c.get("fontname", "") for c in seg]
    bold = sum(1 for f in fonts if "bold" in f.lower()) / max(1, len(fonts))
    return {
        "x0": min(c["x0"] for c in seg), "x1": max(c["x1"] for c in seg),
        "top": min(c["top"] for c in seg), "bottom": max(c["bottom"] for c in seg),
        "text": text.strip(),
        "size": round(statistics.median(sizes), 1) if sizes else 0,
        "bold": bold > 0.5,
    }

def detect_columns(lines, page_w):
    """Return mid x if 2-column else None."""
    xs0 = [l["x0"] for l in lines]; xs1 = [l["x1"] for l in lines]
    if not xs0:
        return None
    left = min(xs0); right = max(xs1)
    mid = (left + right) / 2
    band = (right - left) * 0.08
    # count lines confined to left vs right vs crossing center
    leftc = sum(1 for l in lines if l["x1"] < mid + band)
    rightc = sum(1 for l in lines if l["x0"] > mid - band)
    crossing = sum(1 for l in lines if l["x0"] < mid - band and l["x1"] > mid + band)
    total = len(lines)
    if total >= 8 and leftc >= total * 0.25 and rightc >= total * 0.25 and crossing <= total * 0.25:
        return mid
    return None

def assign_column(line, mid, left, right):
    if mid is None:
        return 0
    band = (right - left) * 0.06
    if line["x0"] < mid - band and line["x1"] > mid + band:
        return 0  # full width
    return 1 if (line["x0"] + line["x1"]) / 2 < mid else 2

HEAD_RE = re.compile(r"^\d+(\.\d+)*\.?\s+[A-Z]")
CAP_RE = re.compile(r"^(Fig\.?|Figure|Table)\b", re.I)
REF_RE = re.compile(r"^\d+\.\s+[A-Z][a-z]+")  # ref list "1. Sheppard JM..."

def classify_block(b, body_size, page_idx, page_h, is_ref_zone):
    t = b["text"]
    if CAP_RE.match(t):
        return "caption"
    # numeric/table-like rows (e.g. "Week 1 2 3 4 5", "6 5", "Total volume 25 30 35")
    toks = t.split()
    if len(toks) >= 2:
        numlike = sum(1 for tk in toks if re.fullmatch(r"[\d.,%×x±+\-/()]+", tk))
        if numlike / len(toks) > 0.5:
            return "data"
    # top running head / bottom footer
    if b["top"] < page_h * 0.07:
        if re.fullmatch(r"\d{1,4}", t.strip()):
            return "pagenum"
        if len(t) < 120 and ("/" in t or "Journal" in t or "et al" in t or b["size"] < body_size):
            return "running_head"
    if b["bottom"] > page_h * 0.90:
        low = t.lower()
        if re.fullmatch(r"\d{1,4}", t.strip()):
            return "pagenum"
        if ("doi" in low or "\u00a9" in t or "rights reserved" in low
                or "front matter" in low or re.search(r"\d{4}-\d{3,4}", t)):
            return "running_head"
    if re.fullmatch(r"\d{1,4}", t.strip()):
        return "pagenum"
    if is_ref_zone and REF_RE.match(t):
        return "reference"
    if page_idx == 0 and b["size"] >= body_size * 1.5:
        return "title"
    if (b["bold"] and HEAD_RE.match(t)) or HEAD_RE.match(t) or (b["bold"] and b["size"] >= body_size * 1.05 and len(t) < 60):
        return "heading"
    return "body"

def group_blocks(lines, mid, left, right, body_size):
    for l in lines:
        l["col"] = assign_column(l, mid, left, right)
    blocks = []
    for col in sorted(set(l["col"] for l in lines)):
        cl = sorted([l for l in lines if l["col"] == col], key=lambda l: l["top"])
        if not cl:
            continue
        gaps = [cl[i+1]["top"] - cl[i]["bottom"] for i in range(len(cl)-1)]
        med_gap = statistics.median(gaps) if gaps else 4.0
        cur = [cl[0]]
        for i in range(1, len(cl)):
            prev, ln = cl[i-1], cl[i]
            gap = ln["top"] - prev["bottom"]
            newpara = gap > max(med_gap * 1.8, (ln["bottom"]-ln["top"]) * 0.9)
            heading = bool(HEAD_RE.match(ln["text"])) or bool(CAP_RE.match(ln["text"]))
            indent = ln["x0"] - prev["x0"] > (right-left) * 0.03
            if newpara or heading or indent:
                blocks.append(_mkblock(cur, col)); cur = [ln]
            else:
                cur.append(ln)
        blocks.append(_mkblock(cur, col))
    return blocks

def _mkblock(lines, col):
    return {
        "col": col,
        "x0": min(l["x0"] for l in lines), "x1": max(l["x1"] for l in lines),
        "top": min(l["top"] for l in lines), "bottom": max(l["bottom"] for l in lines),
        "text": " ".join(l["text"] for l in lines).strip(),
        "size": statistics.median([l["size"] for l in lines if l["size"]] or [0]),
        "bold": sum(l["bold"] for l in lines) > len(lines)/2,
        "nlines": len(lines),
    }

def horizontal_rules(page, min_width=30.0, max_thick=3.0):
    """Horizontal vector rules on the page (abstract-box borders, section
    separators, table top/bottom rules) as [{x0,x1,top,bottom}]. The reflow must
    treat these as obstacles so Japanese text is never drawn across a line -
    vector art is kept in place, so text has to flow around it. Both pdfplumber
    `lines` (zero-height horizontals) and thin `rects` (rules drawn as filled
    boxes) are collected; near-duplicates are merged."""
    out = []
    for l in page.lines:
        top, bot = min(l["top"], l["bottom"]), max(l["top"], l["bottom"])
        if bot - top <= max_thick and (l["x1"] - l["x0"]) >= min_width:
            out.append({"x0": float(l["x0"]), "x1": float(l["x1"]),
                        "top": float(top), "bottom": float(bot)})
    for r in page.rects:
        if r["height"] <= max_thick and (r["x1"] - r["x0"]) >= min_width:
            out.append({"x0": float(r["x0"]), "x1": float(r["x1"]),
                        "top": float(r["top"]), "bottom": float(r["bottom"])})
    # merge rules at essentially the same y with overlapping x-extent
    out.sort(key=lambda r: (round(r["top"]), r["x0"]))
    merged = []
    for r in out:
        m = merged[-1] if merged else None
        if m and abs(r["top"] - m["top"]) <= 2 and r["x0"] <= m["x1"] + 2:
            m["x1"] = max(m["x1"], r["x1"]); m["x0"] = min(m["x0"], r["x0"])
            m["bottom"] = max(m["bottom"], r["bottom"])
        else:
            merged.append(dict(r))
    return merged


def reading_order(blocks):
    """Band-based: full-width blocks split page; within band: left col then right col."""
    full = sorted([b for b in blocks if b["col"] == 0], key=lambda b: b["top"])
    cols = [b for b in blocks if b["col"] != 0]
    # band edges from full-width block centers
    seps = [(b["top"]+b["bottom"])/2 for b in full]
    ordered = []
    bands_full = full[:]  # will interleave
    # Build sequence: iterate y; emit full-width when reached, otherwise columns within band
    edges = sorted(seps)
    def band_of(y):
        i = 0
        for e in edges:
            if y > e: i += 1
        return i
    groups = {}
    for b in cols:
        cy = (b["top"]+b["bottom"])/2
        groups.setdefault(band_of(cy), []).append(b)
    full_by_band = {}
    for b in full:
        cy = (b["top"]+b["bottom"])/2
        full_by_band.setdefault(band_of(cy), []).append(b)
    nbands = max([0] + list(groups.keys()) + list(full_by_band.keys())) + 1
    for band in range(nbands):
        for b in sorted(full_by_band.get(band, []), key=lambda b: b["top"]):
            ordered.append(b)
        band_blocks = groups.get(band, [])
        left = sorted([b for b in band_blocks if b["col"] == 1], key=lambda b: b["top"])
        right = sorted([b for b in band_blocks if b["col"] == 2], key=lambda b: b["top"])
        ordered.extend(left); ordered.extend(right)
    for i, b in enumerate(ordered):
        b["order"] = i + 1
    return ordered

def analyze_pdf(path, name, render=True):
    ensure_out()
    doc = {"file": os.path.basename(path), "pages": []}
    pdf = pdfplumber.open(path)
    ref_started = False   # once the reference list begins it runs to the doc end,
                          # so this persists across pages (a reference whose last
                          # line wraps onto the next page's top has no number)
    npages = len(pdf.pages)
    for pi, page in enumerate(pdf.pages):
        pw, ph = page.width, page.height
        chars = page.chars
        # First pass to estimate body size, then detect the column gutter from body-region chars
        prelim = cluster_lines(chars)
        from collections import Counter
        szs = Counter(round(l["size"]) for l in prelim if l["size"])
        body_size = szs.most_common(1)[0][0] if szs else 10
        body_chars = [c for c in chars
                      if abs(round(c.get("size", 0)) - body_size) <= 1]
        gutter = find_gutter(body_chars, pw)
        # Re-cluster forcing a split at the gutter so two-column rows separate
        lines = cluster_lines(chars, gutter=gutter)
        left = min([l["x0"] for l in lines], default=0)
        right = max([l["x1"] for l in lines], default=pw)
        mid = gutter if gutter is not None else detect_columns(lines, pw)
        blocks = group_blocks(lines, mid, left, right, body_size)
        # reference zone: page where many ref-pattern lines
        is_ref = sum(1 for b in blocks if REF_RE.match(b["text"])) >= 3
        for b in blocks:
            b["type"] = classify_block(b, body_size, pi, ph, is_ref)
        # A bibliography entry spans several lines but only the FIRST (numbered)
        # line matches REF_RE; the continuation lines fell through to "body" and got
        # translated. Once the numbered list has started it runs to the document end,
        # so propagate: reference/numbered lines mark the start (sticky across column
        # AND page breaks - a ref wrapping to the next column/page top has no number),
        # and subsequent body/heading lines become reference too. Only allow this
        # in the BACK HALF of the document: bibliographies live at the end, whereas
        # a numbered METHODS/protocol list ("1. Participants were...") is early and
        # would otherwise poison every following page as "reference" (untranslated).
        if (is_ref or ref_started) and pi >= npages * 0.5:
            NUM_RE = re.compile(r"^\[?\d+[.\]]")
            for col in sorted({b.get("col", 0) for b in blocks}):
                cb = sorted((b for b in blocks if b.get("col", 0) == col),
                            key=lambda b: b["top"])
                for b in cb:
                    if b["type"] == "reference" or NUM_RE.match(b["text"].strip()):
                        b["type"] = "reference"; ref_started = True
                    elif ref_started and b["type"] in ("body", "heading"):
                        b["type"] = "reference"
        # blocks directly under a "Table N" caption are table data. Also demote
        # false "headings" there (column headers like "5 Sets×Reps" match the
        # numbered-heading regex) - but keep real dotted section headings
        # ("3. Results") which can legitimately follow a table.
        SECTION_RE = re.compile(r"^\d+(\.\d+)*\.\s")
        for tc in [b for b in blocks if b["type"] == "caption"
                   and b["text"].lower().startswith("table")]:
            for b in blocks:
                if b is tc or b["type"] not in ("body", "heading"):
                    continue
                if b["type"] == "heading" and SECTION_RE.match(b["text"]):
                    continue
                if b["col"] == tc["col"] and tc["top"] < b["top"] < tc["top"] + 0.22 * ph:
                    b["type"] = "data"
        # figures from images
        figs = []
        for im in page.images:
            figs.append({"type": "figure", "col": 0,
                         "x0": im["x0"], "x1": im["x1"],
                         "top": im["top"], "bottom": im["bottom"],
                         "text": "", "order": None})
        rules = horizontal_rules(page)
        ordered = reading_order(blocks)
        doc["pages"].append({
            "page": pi + 1, "width": pw, "height": ph,
            "x_off": float(page.bbox[0]), "y_off": float(page.bbox[1]),
            "top_off": 0.0,
            "columns": 2 if mid else 1, "body_size": body_size,
            "blocks": ordered, "figures": figs, "rules": rules,
        })
    pdf.close()

    # mark repeated headers (same normalized text in top region on >=3 pages)
    from collections import Counter
    def norm(t): return re.sub(r"[\d\s]", "", t).lower()[:40]
    topcnt = Counter()
    for p in doc["pages"]:
        for b in p["blocks"]:
            if b["top"] < p["height"] * 0.15 and len(b["text"]) > 6:
                topcnt[norm(b["text"])] += 1
    repeated = {k for k, v in topcnt.items() if v >= 3}
    for p in doc["pages"]:
        for b in p["blocks"]:
            if b["top"] < p["height"] * 0.15 and norm(b["text"]) in repeated:
                b["type"] = "running_head"

    # body-flow candidates: real body text, away from margins
    def bodyflow(p):
        return [b for b in p["blocks"] if b["type"] == "body"]

    # cross-page joins (requirement 2): stitch only true sentence continuations
    joins = []
    END_PUNC = tuple(".!?\u3002")
    for i in range(len(doc["pages"]) - 1):
        ptail = doc["pages"][i]
        datatops = [b["top"] for b in ptail["blocks"] if b["type"] == "data"]
        cut = min(datatops) if datatops else ptail["height"]
        cand_t = [b for b in bodyflow(ptail) if b["bottom"] <= cut + 2]
        cand_h = bodyflow(doc["pages"][i+1])
        if not cand_t or not cand_h:
            continue
        last = next((b for b in reversed(cand_t) if len(b["text"].strip()) >= 25), cand_t[-1])
        nxt = next((b for b in cand_h if len(b["text"].strip()) >= 25), cand_h[0])
        t = last["text"].rstrip(); h = nxt["text"].lstrip()
        if not t or not h:
            continue
        tail_open = not t.endswith(END_PUNC)
        head_cont = h[:1].islower() or h[:1] in "\u2018\u2019\"(" or t.endswith("-")
        if tail_open and head_cont and not HEAD_RE.match(h):
            last["continues_to_next_page"] = True
            nxt["continues_from_prev_page"] = True
            joins.append({"from_page": i+1, "to_page": i+2,
                          "tail": t[-70:], "head": h[:70]})
    doc["cross_page_joins"] = joins

    # render annotated pages using pdfplumber's renderer AND its internal coordinate
    # converter (_reproject_bbox), so every box maps exactly regardless of the PDF's
    # cropbox/mediabox/origin quirks. No manual scale/offset math.
    imgs = []
    if not render:
        with open(f"{OUT}/{name}_layout.json", "w") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        return doc, imgs
    plumb = pdfplumber.open(path)
    f_lab = load_label_font(18)
    for pi, pinfo in enumerate(doc["pages"]):
        page = plumb.pages[pi]
        pim = page.to_image(resolution=150)
        img = pim.original.convert("RGB")
        def to_px(b):
            return pim._reproject_bbox((b["x0"], b["top"], b["x1"], b["bottom"]))
        d = ImageDraw.Draw(img, "RGBA")
        for fg in pinfo["figures"]:
            _boxpx(d, to_px(fg), TYPE_COLORS["figure"], "FIG", f_lab)
        for b in pinfo["blocks"]:
            col = TYPE_COLORS.get(b["type"], (0, 0, 0))
            lab = str(b.get("order", "")) + ("/" + b["type"][:4] if b["type"] != "body" else "")
            px = to_px(b)
            _boxpx(d, px, col, lab, f_lab)
            if b.get("continues_to_next_page"):
                d.text((px[2]-70, px[3]-26), "cont->", fill=(200, 0, 0), font=f_lab)
            if b.get("continues_from_prev_page"):
                d.text((px[0]+2, px[1]+2), "<-cont", fill=(200, 0, 0), font=f_lab)
        # downscale for delivery
        MAXW, MAXH = 1240, 1654
        if img.width > MAXW or img.height > MAXH:
            img.thumbnail((MAXW, MAXH))
        out = f"{OUT}/{name}_p{pi+1:02d}.png"
        img.save(out)
        imgs.append(out)
    plumb.close()

    with open(f"{OUT}/{name}_layout.json", "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return doc, imgs

def _boxpx(d, xy, color, label, font):
    d.rectangle(list(xy), outline=color + (255,), width=2, fill=color + (24,))
    if label:
        tx, ty = xy[0] + 1, xy[1] + 1
        d.rectangle([tx, ty, tx + len(label) * 10 + 6, ty + 18], fill=color + (235,))
        d.text((tx + 3, ty), label, fill=(255, 255, 255), font=font)

def _box(d, b, color, label, font, sx, sy, ox=0.0, oy=0.0):
    xy = [(b["x0"]-ox)*sx, (b["top"]-oy)*sy, (b["x1"]-ox)*sx, (b["bottom"]-oy)*sy]
    d.rectangle(xy, outline=color + (255,), width=2, fill=color + (24,))
    if label:
        tx, ty = xy[0]+1, xy[1]+1
        d.rectangle([tx, ty, tx+len(label)*11+6, ty+20], fill=color+(235,))
        d.text((tx+3, ty+1), label, fill=(255,255,255), font=font)

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="M1: PDF layout analysis -> <name>_layout.json")
    ap.add_argument("inputs", nargs="*", default=["paper", "deck"],
                    help="PDF paths or sample names (default: paper deck)")
    ap.add_argument("--name", help="override output name (single input only)")
    ap.add_argument("--no-render", action="store_true",
                    help="skip annotated PNG rendering (faster)")
    args = ap.parse_args()
    if args.name and len(args.inputs) != 1:
        ap.error("--name requires exactly one input")
    ensure_out()
    for inp in args.inputs:
        path, name = resolve_pdf(inp)
        if args.name:
            name = args.name
        doc, imgs = analyze_pdf(path, name, render=not args.no_render)
        nb = sum(len(p["blocks"]) for p in doc["pages"])
        nf = sum(len(p["figures"]) for p in doc["pages"])
        print(f"[{name}] pages={len(doc['pages'])} blocks={nb} figures={nf} "
              f"cross_page_joins={len(doc['cross_page_joins'])} cols/page="
              f"{[p['columns'] for p in doc['pages']]}")
