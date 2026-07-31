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

def _char_segments(chars, gutter=None):
    """Cluster chars into LINE SEGMENTS in 2D: a char joins an open segment only
    when its vertical span overlaps the segment's span by >=55% of the smaller
    height AND it is horizontally near the segment (gap bounded by the segment's
    own char width). This is island-safe: text sitting at the same y but far away
    on the page (a diagram callout label next to a sidebar paragraph, the two
    halves of a spread) can never join - the old row-then-split approach chained
    such islands through a shared row and interleaved their characters by x.
    If a gutter x is given, segments never grow across it (two-column rows
    separate cleanly). Returns a list of char lists."""
    chars = [c for c in chars if c.get("text", "").strip() != ""]
    if not chars:
        return []
    widths = [c["x1"] - c["x0"] for c in chars]
    mw = statistics.median(widths) or 4.0
    chars = sorted(chars, key=lambda c: (c["top"], c["x0"]))
    open_segs, done = [], []
    for c in chars:
        ch_h = (c["bottom"] - c["top"]) or 1.0
        # chars arrive in top order, so a segment ending above this char can
        # never receive another member - close it
        still = []
        for s in open_segs:
            (done if s["bottom"] <= c["top"] + 0.1 else still).append(s)
        open_segs = still
        best = best_score = None
        for s in open_segs:
            ov = min(s["bottom"], c["bottom"]) - max(s["top"], c["top"])
            if ov < 0.55 * min(ch_h, s["h"]):
                continue
            gap = max(c["x0"] - s["x1"], s["x0"] - c["x1"], 0.0)
            # word spaces are ~0.5x char width; column/label gaps are far larger.
            # The cap keeps a stretched (justified) space inside the line while a
            # table's label->value gap or a callout island still splits.
            lim = min(max(3.2 * s["cw"], 1.6 * s["h"]), 30.0)
            if gap > lim:
                continue
            # split at the column gutter, but only when the gap there is clearly
            # wider than a word space - a full-width title/abstract line crosses
            # the gutter with normal spacing and must stay whole
            if gutter is not None and gap > 2.0 * s["cw"] and (
                    (s["x1"] <= gutter <= c["x0"]) or (c["x1"] <= gutter <= s["x0"])):
                continue
            score = (ov / min(ch_h, s["h"]), -gap)
            if best is None or score > best_score:
                best, best_score = s, score
        if best is None:
            open_segs.append({
                "chars": [c], "x0": c["x0"], "x1": c["x1"],
                "top": c["top"], "bottom": c["bottom"],
                "h": ch_h, "cw": (c["x1"] - c["x0"]) or mw})
        else:
            s = best
            s["chars"].append(c)
            s["x0"] = min(s["x0"], c["x0"]); s["x1"] = max(s["x1"], c["x1"])
            s["top"] = min(s["top"], c["top"]); s["bottom"] = max(s["bottom"], c["bottom"])
            # running medians keep the attach thresholds stable within the line
            s["h"] = statistics.median(x["bottom"] - x["top"] for x in s["chars"]) or 1.0
            s["cw"] = statistics.median(x["x1"] - x["x0"] for x in s["chars"]) or mw
            # A raised superscript (or a symbol like ±) can open its own segment
            # BEFORE its line's lower chars arrive (chars stream in top order);
            # the line then grows toward it from the side. Merge open segments
            # that have become adjacent so the line ends up whole.
            merged = True
            while merged:
                merged = False
                for o in open_segs:
                    if o is s:
                        continue
                    ov = min(s["bottom"], o["bottom"]) - max(s["top"], o["top"])
                    if ov < 0.55 * min(s["h"], o["h"]):
                        continue
                    gap = max(o["x0"] - s["x1"], s["x0"] - o["x1"], 0.0)
                    if gap > min(max(3.2 * max(s["cw"], o["cw"]),
                                     1.6 * max(s["h"], o["h"])), 30.0):
                        continue
                    if gutter is not None and gap > 2.0 * max(s["cw"], o["cw"]) and (
                            (s["x1"] <= gutter <= o["x0"]) or (o["x1"] <= gutter <= s["x0"])):
                        continue
                    s["chars"] += o["chars"]
                    s["x0"] = min(s["x0"], o["x0"]); s["x1"] = max(s["x1"], o["x1"])
                    s["top"] = min(s["top"], o["top"]); s["bottom"] = max(s["bottom"], o["bottom"])
                    s["h"] = statistics.median(
                        x["bottom"] - x["top"] for x in s["chars"]) or 1.0
                    s["cw"] = statistics.median(
                        x["x1"] - x["x0"] for x in s["chars"]) or mw
                    open_segs.remove(o)
                    merged = True
                    break
    return [s["chars"] for s in done + open_segs]


def cluster_lines(chars, gutter=None):
    """Char segments (see _char_segments) rendered as line dicts, with one
    refinement: a segment is split at an ALIGNMENT EDGE - a gap that lands
    exactly on a left edge shared by several other segments (the start of a
    neighbouring text island/column). Two islands separated by only ~10pt can
    otherwise fuse through one long line; the shared left edge is the reliable
    signal that a new island starts there. False splits are harmless (the block
    builder reunites x-overlapping fragments); false MERGES garble text."""
    chars = [c for c in chars if c.get("text", "").strip() != ""]
    if not chars:
        return []
    mw = statistics.median([c["x1"] - c["x0"] for c in chars]) or 4.0
    segs = [sorted(s, key=lambda c: (c["x0"], c["top"]))
            for s in _char_segments(chars, gutter=gutter)]
    from collections import Counter
    starts = Counter(round(s[0]["x0"]) for s in segs)
    strong = [x for x, n in starts.items() if n >= 3]
    def at_edge(x):
        return any(abs(x - e) <= 1.5 for e in strong)
    out = []
    for seg in segs:
        cw = statistics.median([c["x1"] - c["x0"] for c in seg]) or mw
        cur = [seg[0]]
        marker_re = re.compile(r"^[•‣⁃▪●–—·∙\-\d.()\[\]]+$")
        for c in seg[1:]:
            # never orphan a leading list/heading marker ("•", "3.", "[12]") -
            # its text always starts at a shared edge (hanging indent)
            head = "".join(x["text"] for x in cur).strip()
            if not marker_re.match(head) and at_edge(c["x0"]) \
                    and c["x0"] - cur[-1]["x1"] >= max(4.0, 1.2 * cw):
                out.append(cur); cur = [c]
            else:
                cur.append(c)
        out.append(cur)
    return [_mkline(s, mw) for s in out]

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

    # Cluster chars into line segments; drop segments that span across the center
    # (full-width title/abstract lines would otherwise mask the gutter). Column
    # text stays on its own side, so what remains exposes the whitespace band.
    band = width * 0.04
    col_chars = []
    for seg in _char_segments(chars):
        rl = min(c["x0"] for c in seg); rr = max(c["x1"] for c in seg)
        if rl < mid_guess - band and rr > mid_guess + band:
            continue
        col_chars.extend(seg)
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
    # a measurement cell ("22.83 m", "3 x 1,884 kW") or a stack of them (a spec
    # table's value column) is table data: keep verbatim, never translate
    if _numericish(t) or (b.get("nlines", 1) >= 2
                          and b.get("num_lines", 0) / b["nlines"] >= 0.6):
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
    # a lone capitalized word on its own line is a section label
    # ("Abstract", "Introduction", "References") - a merge boundary, not prose
    if b.get("nlines", 1) == 1 and re.fullmatch(r"[A-Z][A-Za-z]{2,15}", t.strip()):
        return "heading"
    return "body"

def _typical_line_gap(lines):
    """Median vertical gap between a line and the nearest line below it that
    overlaps it horizontally (i.e. the page's typical leading), robust to pages
    that mix several independent text islands."""
    gaps = []
    ls = sorted(lines, key=lambda l: l["top"])
    for i, l in enumerate(ls):
        best = None
        for m in ls[i + 1:]:
            g = m["top"] - l["bottom"]
            if g > 40:
                break
            if g >= -1 and min(l["x1"], m["x1"]) - max(l["x0"], m["x0"]) > 0:
                if best is None or g < best:
                    best = g
        if best is not None:
            gaps.append(best)
    return statistics.median(gaps) if gaps else 4.0


def group_blocks(lines, mid, left, right, body_size):
    """Build blocks in 2D: a line joins an open block only when it is vertically
    adjacent AND horizontally aligned with it (x-overlap or shared left edge) AND
    of a similar font size. Column membership is assigned to the finished block's
    bbox. The old per-column purely-vertical merge chained unrelated text islands
    (sidebar paragraphs, diagram labels, table columns) that happened to share
    the page's single wide column."""
    if not lines:
        return []
    med_gap = _typical_line_gap(lines)
    page_text_w = max(right - left, 1.0)
    ls = sorted(lines, key=lambda l: (l["top"], l["x0"]))
    open_blocks, done = [], []
    for l in ls:
        lh = (l["bottom"] - l["top"]) or 1.0
        allowed = max(med_gap * 1.8, lh * 0.9)
        still = []
        for b in open_blocks:
            (done if b["bottom"] < l["top"] - allowed - 0.1 else still).append(b)
        open_blocks = still
        starts_new = bool(HEAD_RE.match(l["text"])) or bool(CAP_RE.match(l["text"]))
        best = best_score = None
        if not starts_new:
            for b in open_blocks:
                gap = l["top"] - b["bottom"]
                if gap > allowed or gap < -lh * 0.6:
                    continue
                ov = min(l["x1"], b["x1"]) - max(l["x0"], b["x0"])
                wmin = min(l["x1"] - l["x0"], b["x1"] - b["x0"]) or 1.0
                aligned = abs(l["x0"] - b["x0"]) <= max(6.0, (l["size"] or 8.0))
                if not (ov >= 0.45 * wmin or (aligned and ov > 0)):
                    continue
                # tabular row guard: if ANOTHER segment sits on the same visual
                # row inside this block's x-span, the row is a table row (multiple
                # cells) - it must not be absorbed into a prose/caption block
                if any(m is not l
                       and min(l["bottom"], m["bottom"]) - max(l["top"], m["top"])
                           >= 0.5 * min(lh, (m["bottom"] - m["top"]) or 1.0)
                       and min(m["x1"], b["x1"]) - max(m["x0"], b["x0"]) > 0
                       for m in ls):
                    continue
                s1 = l["size"] or body_size or 10
                s2 = b["size_med"] or body_size or 10
                if max(s1, s2) / max(0.1, min(s1, s2)) > 1.25:
                    continue    # different font sizes = different roles (title vs
                                # author line, label vs body) - keep them apart
                if l["x0"] - b["x0"] > page_text_w * 0.03:
                    continue    # indented line = new paragraph
                score = (ov / wmin, -gap)
                if best is None or score > best_score:
                    best, best_score = b, score
        if best is None:
            open_blocks.append({
                "lines": [l], "x0": l["x0"], "x1": l["x1"],
                "top": l["top"], "bottom": l["bottom"],
                "size_med": l["size"]})
        else:
            b = best
            b["lines"].append(l)
            b["x0"] = min(b["x0"], l["x0"]); b["x1"] = max(b["x1"], l["x1"])
            b["top"] = min(b["top"], l["top"]); b["bottom"] = max(b["bottom"], l["bottom"])
            b["size_med"] = statistics.median(
                [x["size"] for x in b["lines"] if x["size"]] or [0])
    blocks = []
    for b in done + open_blocks:
        blk = _mkblock(b["lines"], 0)
        blk["col"] = assign_column(blk, mid, left, right)
        blocks.append(blk)
    return blocks

# Tokens that read as measurements/table cells alongside digits. Used to spot
# spec-table value columns ("22.83 m", "74 ft 11 in", "3 x 1,884 kW") that must
# stay verbatim (type "data"), on any document - not tied to one sample.
_UNIT_TOKEN_RE = re.compile(
    r"(?i)^(?:m|cm|mm|km|kg|g|lb|lbs|ft|in|kt|kts|kph|mph|nm|shp|hp|kw|kn|"
    r"s|sec|min|hr|hrs|hours?|x|×|/|%|°[cf]?|ft/min|m/s\d?|kg/m\d?)$")


def _numericish(text):
    toks = text.split()
    if not toks or not _DIGIT_ANY_RE.search(text):
        return False
    hits = sum(1 for t in toks
               if re.fullmatch(r"[\d.,%×x±+\-/()<>≈=]+", t) or _UNIT_TOKEN_RE.match(t))
    return hits / len(toks) > 0.5


_DIGIT_ANY_RE = re.compile(r"\d")


def _mkblock(lines, col):
    lines = sorted(lines, key=lambda l: (l["top"], l["x0"]))
    return {
        "col": col,
        "x0": min(l["x0"] for l in lines), "x1": max(l["x1"] for l in lines),
        "top": min(l["top"] for l in lines), "bottom": max(l["bottom"] for l in lines),
        "text": " ".join(l["text"] for l in lines).strip(),
        "size": statistics.median([l["size"] for l in lines if l["size"]] or [0]),
        "bold": sum(l["bold"] for l in lines) > len(lines)/2,
        "nlines": len(lines),
        "num_lines": sum(1 for l in lines if _numericish(l["text"])),
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
        # rotated (non-upright) text is figure content - chart axis labels,
        # decorative verticals. It extracts garbled (reversed), cannot be
        # stripped reliably, and must simply stay untouched on the page.
        chars = [c for c in page.chars if c.get("upright", True)]
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
