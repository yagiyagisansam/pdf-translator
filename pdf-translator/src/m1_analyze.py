#!/usr/bin/env python3
# Milestone 1: Layout analysis engine
import os, re, json, statistics, unicodedata
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
    # DEDUPE double-printed chars: many PDFs draw the same glyph twice at (near)
    # identical coordinates (fill+stroke bold emulation, overlaid form fields).
    # Without this the duplicates interleave by x and every word doubles
    # ("AIM AIM" -> "AAIIMM"), which poisons segmentation AND translation.
    seen = set()
    deduped = []
    for c in chars:
        qx = int(c["x0"] * 3)          # 1/3pt buckets; check both neighbours so
        qy = int(c["top"] * 3)         # a pair straddling a bucket edge still hits
        keys = [(c["text"], qx + dx, qy + dy)
                for dx in (0, 1, -1) for dy in (0, 1, -1)]
        if any(k in seen for k in keys):
            continue
        seen.add(keys[0])
        deduped.append(c)
    chars = deduped
    # SCALED-DUPLICATE RUNS: some PDFs draw a whole text panel TWICE at
    # slightly different scales (an infographic exported once directly and
    # once inside a rescaled form). Corresponding lines then sit up to a line
    # pitch apart with DIFFERENT text at the colliding y, so char-level
    # dedupe can never catch them and the copies interleave into gibberish.
    # Detect whole baseline runs whose normalized text already appeared
    # nearby at a slightly DIFFERENT size (a real repeated line - a table's
    # recurring cell - repeats at the SAME size) and drop the copy. This runs
    # BEFORE the per-char offset dedupe below: that one keeps only tight
    # matches and would otherwise eat parts of a copy, leaving fragments no
    # run key can pair up.
    runs = []
    for c in chars:
        sz = c.get("size") or (c["bottom"] - c["top"]) or 8.0
        r = runs[-1] if runs else None
        # the split gap is TIGHT (~half a char size): two side-by-side panels'
        # headers must not concatenate into one run, or the copies' keys
        # ("CONGRESS" vs "CONGRESSFEDERAL") never match. Word spaces are well
        # under 0.5x size; a justified line that still splits is harmless -
        # both copies split at the same place and match piecewise.
        if r is not None and abs(c["top"] - r["top"]) <= 0.35 and \
                -1.0 <= c["x0"] - r["x1"] <= max(1.5, 0.5 * sz):
            r["chars"].append(c)
            r["x1"] = max(r["x1"], c["x1"])
        else:
            runs.append({"top": c["top"], "x0": c["x0"], "x1": c["x1"],
                         "chars": [c]})
    kept_runs = {}
    drop = set()
    for r in runs:
        t = unicodedata.normalize("NFKC",
                                  "".join(x["text"] for x in r["chars"]))
        key = re.sub(r"[^0-9a-z]", "", t.lower())
        if len(key) < 6:
            continue
        szs = [x.get("size") or 0 for x in r["chars"] if x.get("size")]
        rsz = statistics.median(szs) if szs else 8.0
        dup = False
        for (px0, ptop, psz) in kept_runs.get(key, ()):
            ratio = max(rsz, psz) / max(min(rsz, psz), 0.1)
            if 1.02 <= ratio <= 1.35 and abs(r["x0"] - px0) <= 8.0 and \
                    abs(r["top"] - ptop) <= 1.8 * max(rsz, psz):
                dup = True
                break
        if dup:
            drop.update(id(x) for x in r["chars"])
        else:
            kept_runs.setdefault(key, []).append((r["x0"], r["top"], rsz))
    if drop:
        chars = [c for c in chars if id(c) not in drop]
    # OFFSET doubles: faux-bold re-draws a whole run shifted by up to ~2pt
    # diagonally - too far for the exact buckets above, too close for distinct
    # runs. A real repeated letter ("ll") sits on the SAME baseline exactly
    # one advance away, so a same-glyph neighbour with a small-but-nonzero
    # vertical offset (or well under one advance horizontally) can only be a
    # double-print. The window stays TIGHT (<=1.2pt) on purpose: two unrelated
    # lines from a scaled duplicate layer can nearly coincide, and a wider
    # window eats legitimate letters out of them (the run pass above handles
    # those as whole lines).
    recent = {}                    # text -> [(x0, top, adv, size)] kept chars
    deduped = []
    for c in chars:
        adv = c["x1"] - c["x0"]
        sz = c.get("size") or (c["bottom"] - c["top"]) or 8.0
        prev = recent.get(c["text"], [])
        prev = [p for p in prev if p[1] >= c["top"] - 1.3]
        dup = any(
            (0.05 < abs(c["top"] - p[1]) <= 1.2 and abs(c["x0"] - p[0]) <= 2.0
             and max(sz, p[3]) / max(min(sz, p[3]), 0.1) <= 1.15)
            or (abs(c["top"] - p[1]) <= 0.05
                and abs(c["x0"] - p[0]) <= 0.45 * max(adv, p[2]))
            for p in prev)
        if dup:
            continue
        prev.append((c["x0"], c["top"], adv, sz))
        recent[c["text"]] = prev
        deduped.append(c)
    chars = deduped
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
            # a char far off the segment's size is a DIFFERENT element (an
            # icon's 5.4pt badge word grazing a 10pt body line) - superscripts
            # (~70% of body) stay within the ratio
            cs = c.get("size") or 0
            ss = s.get("sz") or 0
            if cs and ss and max(cs, ss) / min(cs, ss) > 1.6:
                continue
            # INTERLEAVE guard: a char STARTING left of the segment's right
            # edge would be sorted into its middle. Real text in one line only
            # appends (kerning grazes < ~0.5pt); a mid-line insert at a
            # different size is another element physically overlapping (a
            # box header over a ghost body line) - keep them separate.
            if c["x0"] < s["x1"] - 0.6 and cs and ss and \
                    max(cs, ss) / min(cs, ss) > 1.1:
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
            # the gutter with normal spacing and must stay whole. The word-space
            # yardstick is the SMALLER char width of the two sides: a large-font
            # heading on the other side of the gutter otherwise inflates it
            # until a real column gap reads as a word space (and the columns
            # merge and interleave).
            if gutter is not None and \
                    gap > 2.0 * min(s["cw"], (c["x1"] - c["x0"]) or s["cw"]) and (
                    (s["x1"] <= gutter <= c["x0"]) or (c["x1"] <= gutter <= s["x0"])):
                continue
            score = (ov / min(ch_h, s["h"]), -gap)
            if best is None or score > best_score:
                best, best_score = s, score
        if best is None:
            open_segs.append({
                "chars": [c], "x0": c["x0"], "x1": c["x1"],
                "top": c["top"], "bottom": c["bottom"],
                "h": ch_h, "cw": (c["x1"] - c["x0"]) or mw,
                "sz": c.get("size") or 0})
        else:
            s = best
            s["chars"].append(c)
            s["x0"] = min(s["x0"], c["x0"]); s["x1"] = max(s["x1"], c["x1"])
            s["top"] = min(s["top"], c["top"]); s["bottom"] = max(s["bottom"], c["bottom"])
            # running medians keep the attach thresholds stable within the line
            s["h"] = statistics.median(x["bottom"] - x["top"] for x in s["chars"]) or 1.0
            s["cw"] = statistics.median(x["x1"] - x["x0"] for x in s["chars"]) or mw
            s["sz"] = statistics.median(x.get("size") or 0 for x in s["chars"])
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
                    if s.get("sz") and o.get("sz") and \
                            max(s["sz"], o["sz"]) / min(s["sz"], o["sz"]) > 1.6:
                        continue
                    # INTERLEAVE guard (same as the char-attach one): two real
                    # same-line segments meet edge to edge; segments whose
                    # x-ranges OVERLAP at different sizes are different
                    # elements printed on top of each other - merging would
                    # shuffle their chars together. A TINY segment (<=3 chars)
                    # is a superscript/subscript that opened early and must
                    # still rejoin its line (the char-attach guard split it
                    # out precisely because its size differs).
                    x_ov = min(s["x1"], o["x1"]) - max(s["x0"], o["x0"])
                    if x_ov > 0.6 and s.get("sz") and o.get("sz") and \
                            max(s["sz"], o["sz"]) / min(s["sz"], o["sz"]) > 1.1:
                        tiny, big = (s, o) if len(s["chars"]) <= len(o["chars"]) \
                            else (o, s)
                        # only a SMALLER-size tiny segment is a super/subscript
                        # rejoining its line; a LARGER-size one is another
                        # element's lettering arriving char by char
                        if not (len(tiny["chars"]) <= 3
                                and tiny["sz"] <= big["sz"]):
                            continue
                    gap = max(o["x0"] - s["x1"], s["x0"] - o["x1"], 0.0)
                    if gap > min(max(3.2 * max(s["cw"], o["cw"]),
                                     1.6 * max(s["h"], o["h"])), 30.0):
                        continue
                    if gutter is not None and gap > 2.0 * min(s["cw"], o["cw"]) and (
                            (s["x1"] <= gutter <= o["x0"]) or (o["x1"] <= gutter <= s["x0"])):
                        continue
                    s["chars"] += o["chars"]
                    s["x0"] = min(s["x0"], o["x0"]); s["x1"] = max(s["x1"], o["x1"])
                    s["top"] = min(s["top"], o["top"]); s["bottom"] = max(s["bottom"], o["bottom"])
                    s["h"] = statistics.median(
                        x["bottom"] - x["top"] for x in s["chars"]) or 1.0
                    s["cw"] = statistics.median(
                        x["x1"] - x["x0"] for x in s["chars"]) or mw
                    s["sz"] = statistics.median(
                        x.get("size") or 0 for x in s["chars"])
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
            # a SHORT single-word head ("Other| penalties.") is usually a bold
            # run-in lead followed by an em-quad, not an island boundary -
            # demand a clearly-wider gap before splitting it off. A table
            # label cell ("EMEA | 3,240") still splits: its gap is huge.
            short_head = " " not in head and len(head) <= 12
            need = max(4.0, (2.4 if short_head else 1.2) * cw)
            if not marker_re.match(head) and at_edge(c["x0"]) \
                    and c["x0"] - cur[-1]["x1"] >= need:
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

def _char_rgb(c):
    """Char fill color as an (r,g,b) triple in 0..1. The overlay must draw the
    Japanese in the SOURCE text's color - a brochure's white-on-photo label
    drawn in default black is invisible on the dark background."""
    col = c.get("non_stroking_color")
    if col is None:
        return (0.0, 0.0, 0.0)
    if isinstance(col, (int, float)):
        col = (col,)
    try:
        vals = [float(v) for v in col]
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    if len(vals) == 1:
        g = vals[0]
        # A single component is a GRAY level in Gray/ICC colorspaces. In a
        # Separation/DeviceN (spot color) space it is an ink TINT: 1.0 = full
        # ink (e.g. a solid red heading), which read as gray would be WHITE -
        # invisible on paper. Approximate spot ink as darkness (hue unknown).
        if str(c.get("ncs", "")) in ("Separation", "DeviceN"):
            g = 1.0 - g
        return (g, g, g)
    if len(vals) == 3:
        return tuple(min(1.0, max(0.0, v)) for v in vals)
    if len(vals) == 4:
        cy, ma, ye, k = vals
        return ((1 - cy) * (1 - k), (1 - ma) * (1 - k), (1 - ye) * (1 - k))
    return (0.0, 0.0, 0.0)


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
    # unmapped glyphs extract as "(cid:NNN)" - almost always dashes/bullets in
    # fonts without a ToUnicode map. A dash reads correctly ("00-45"); the
    # literal "(cid:239)" would pollute the translation input and output.
    if "(cid:" in text:
        text = re.sub(r"\(cid:\d+\)", "-", text)
    # Private-Use-Area chars (Wingdings checkboxes/bullets) survive translation
    # verbatim but have no glyph in the output font - they render as tofu.
    # A generic list bullet is the faithful visible substitute.
    text = re.sub("[\\ue000-\\uf8ff]", "•", text)
    sizes = [c.get("size", 0) for c in seg if c.get("size")]
    fonts = [c.get("fontname", "") for c in seg]
    bold = sum(1 for f in fonts if "bold" in f.lower()) / max(1, len(fonts))
    from collections import Counter
    color = Counter(tuple(round(v, 2) for v in _char_rgb(c))
                    for c in seg).most_common(1)[0][0]
    return {
        "x0": min(c["x0"] for c in seg), "x1": max(c["x1"] for c in seg),
        "top": min(c["top"] for c in seg), "bottom": max(c["bottom"] for c in seg),
        "text": text.strip(),
        "size": round(statistics.median(sizes), 1) if sizes else 0,
        "bold": bold > 0.5,
        "color": list(color),
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
# a number followed by a measurement unit is DATA mid-sentence ("... range of
# 108.10 to  111.95 MHz."), never a section heading - without this guard the
# decimal matches HEAD_RE and the sentence is split at the line break
_HEAD_UNIT_GUARD = re.compile(
    r"^\d+(?:\.\d+)*\.?\s+(?:MHz|GHz|kHz|Hz|NM|nm|ft|feet|km|kg|lbs?|kt|kts|"
    r"mm|cm|m|s|sec|min|hrs?|hours?|%|°[CF]?|dB|psi|mi|yd)\b")


def _headish(t):
    return bool(HEAD_RE.match(t)) and not _HEAD_UNIT_GUARD.match(t)


# contact/address lines (a phone number, "P.O. Box 25082", "Oklahoma City, OK
# 73125", a bare URL/email line). Sealed one line = one block: merging them
# into a paragraph turns the address into run-on prose and garbles it in
# translation ("P.O.ボックス25 082オクラホマシティー").
_CONTACT_RES = (
    re.compile(r"\(\d{3}\)\s?\d{3}[-–]\d{4}"),          # (405) 954-4831
    re.compile(r"\+\d[\d\s().\-]{7,}\d$"),               # +44 (0)1234 567890
    re.compile(r"\bP\.?\s?O\.?\s?Box\s+\d+", re.I),      # P.O. Box 25082
    re.compile(r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"),      # ..., OK 73125
    # NOTE: a bare-URL line is NOT sealed - a URL often continues on the next
    # line, and sealing the first half would orphan the continuation. URLs are
    # kept whole by the space-less line join + M2 token protection instead.
)


def _is_contact_line(text):
    t = (text or "").strip()
    if not t or len(t) > 64:
        return False
    for rx in _CONTACT_RES:
        m = rx.search(t)
        if m:
            rest = (t[:m.start()] + " " + t[m.end():]).strip()
            # the line must BE the contact info (few words around the match),
            # not a prose sentence that merely mentions a phone number
            if len([w for w in rest.split() if re.search(r"[A-Za-z]", w)]) <= 3:
                return True
    return False
REF_RE = re.compile(r"^\d+\.\s+[A-Z][a-z]+")  # ref list "1. Sheppard JM..."

def classify_block(b, body_size, page_idx, page_h, is_ref_zone):
    t = b["text"]
    if CAP_RE.match(t):
        return "caption"
    # numeric/table-like rows (e.g. "Week 1 2 3 4 5", "6 5", "Total volume 25 30 35").
    # Dot-leader tokens (TOC rows) are ignored so a titled TOC entry
    # ("5-1-1. Preflight Preparation . . . .") stays translatable text.
    toks = [tk for tk in t.split() if tk.strip(".·⋅⋯") != ""]
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
    if _headish(t) or (b["bold"] and b["size"] >= body_size * 1.05 and len(t) < 60):
        return "heading"
    # ICON BADGE lettering ("CAUTION" / "TIP" stamped inside a margin icon):
    # a tiny ALL-CAPS standalone word far below body size is artwork, not
    # prose - translating it draws Japanese over the icon's baked-in word
    if b.get("nlines", 1) == 1 and (b.get("size") or 9) <= 6.5 and \
            len(t.strip()) <= 8 and t.strip().isupper():
        return "data"
    # Mostly-UPPERCASE short blocks are headings even at body size and even
    # without a bold flag ("h. 7-1-1. NATIONAL WEATHER SERVICE ..." in
    # regulation manuals mark headings by CAPS alone) - typed as body they
    # chain the following paragraphs into one run-on unit
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 6 and b.get("nlines", 1) <= 3 \
            and len(t) < 70 * max(1, b.get("nlines", 1)) \
            and sum(1 for c in letters if c.isupper()) / len(letters) >= 0.7:
        return "heading"
    # a lone capitalized word on its own line is a section label
    # ("Abstract", "Introduction", "References") - a merge boundary, not prose
    if b.get("nlines", 1) == 1 and re.fullmatch(r"[A-Z][A-Za-z]{2,15}", t.strip()):
        return "heading"
    return "body"

def _caps_ratio(text):
    """Uppercase ratio, or None when there are too few letters to be a
    meaningful case profile (a trailing "SPJ)." fragment must not read as an
    all-caps heading)."""
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 6:
        return None
    return sum(1 for c in letters if c.isupper()) / len(letters)


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


def group_blocks(lines, mid, left, right, body_size, rules=()):
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

    def _row_rule_spans(l, m):
        """True when a horizontal rule spans BOTH l and its row-mate m near
        their row - the signature of a ruled table row."""
        lo = min(l["x0"], m["x0"]); hi = max(l["x1"], m["x1"])
        for r in rules:
            if r["x0"] <= lo + 4 and r["x1"] >= hi - 4:
                yr = (r["top"] + r["bottom"]) / 2
                if l["top"] - 14 <= yr <= l["bottom"] + 14:
                    return True
        return False

    def _is_record_row(l):
        """A line with a NEARBY row-mate that is NUMERIC (a TOC row next to its
        page number, a spec label next to its value) or that shares a RULED
        table row is one row of a table/leadered list. Such rows must stay
        one-block-per-row: merging them into a paragraph turns the table into
        run-on prose and breaks the row alignment. (Prose lines occasionally
        sealed this way are harmless: M2's continuation rules re-merge them
        into one translation unit.)"""
        lh = (l["bottom"] - l["top"]) or 1.0
        for m in ls:
            if m is l:
                continue
            ov = min(l["bottom"], m["bottom"]) - max(l["top"], m["top"])
            if ov < 0.5 * min(lh, (m["bottom"] - m["top"]) or 1.0):
                continue
            gap = max(m["x0"] - l["x1"], l["x0"] - m["x1"], 0.0)
            # financial tables put a wide gutter between the label column and
            # its value columns ("EMEA ......... $ 3,240"); 15% missed them and
            # the label rows chained into one run-on block. A numeric row-mate
            # anywhere in the same visual row seals the line; a false seal on
            # prose is harmless (M2 re-merges it into the paragraph unit).
            if gap > 0.45 * page_text_w:
                continue
            # a row-mate at a WILDLY different size is page decoration (the
            # 70pt chapter numeral beside a 24pt divider title), not a table
            # cell - sealing the title would break its multi-line stitch
            lsz, msz = l.get("size") or 0, m.get("size") or 0
            if lsz and msz and max(lsz, msz) / min(lsz, msz) > 2.0:
                continue
            if _numericish(m["text"]) or _row_rule_spans(l, m):
                return "cell"
            # PARALLEL WORD-LIST COLUMNS ("banking hours | eye opener |
            # real estate"): both lines are SHORT - the body lines of a real
            # two-column document fill their column width and never match.
            # Without the seal each column merges into a run-on paragraph
            # ("冷酷な脚注無分別な非法行為..." gibberish) drawn over the
            # other columns' surviving English. Tagged "list" so the stacked-
            # cell merge never chains the stack back together.
            if len(l["text"]) <= 32 and len(m["text"]) <= 32 and gap >= 12:
                return "list"
        return None

    # a DOT-LEADER line (TOC row "Title . . . . 1-1-12") is one row of a
    # leadered list even when the leader glues the title and the page number
    # into a single segment - sealing it keeps rows from chaining into one
    # run-on paragraph of dots
    _leader = re.compile(r"(?:[.·⋅]\s*){5,}")
    record_rows, list_rows = set(), set()
    for l in ls:
        why = _is_record_row(l)
        if why or _is_contact_line(l["text"]) or _leader.search(l["text"]):
            record_rows.add(id(l))
        if why == "list":
            list_rows.add(id(l))
        # ICON BADGE lettering ("CAUTION"/"TIP" stamped in a margin icon):
        # tiny ALL-CAPS word far below body size. Sealed so it can never be
        # absorbed into the neighbouring paragraph, where it would translate
        # inline and lose its data classification.
        t = l["text"].strip()
        if l.get("size") and l["size"] <= 6.5 and len(t) <= 8 and \
                t.isupper() and t.isalpha():
            record_rows.add(id(l))
    open_blocks, done = [], []
    for l in ls:
        lh = (l["bottom"] - l["top"]) or 1.0
        allowed = max(med_gap * 1.8, lh * 0.9)
        still = []
        for b in open_blocks:
            (done if b["bottom"] < l["top"] - allowed - 0.1 else still).append(b)
        open_blocks = still
        # a line starting with a bullet marker begins a NEW list item - gluing
        # it to the previous block turns the whole list into one run-on
        # paragraph (M2's bullet boundary only works if M1 kept them apart)
        starts_new = (_headish(l["text"]) or bool(CAP_RE.match(l["text"]))
                      or bool(re.match(r"[•‣⁃▪●·∙]\s", l["text"]))
                      or id(l) in record_rows)
        best = best_score = None
        if not starts_new and id(l) not in record_rows:
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
                # CASE-PROFILE boundary: an ALL-CAPS line joining a mixed-case
                # block (or vice versa) is a heading meeting body text - some
                # PDFs mark headings by CAPS alone, with no bold flag and no
                # size change (FAA manuals), and merging them chains every
                # section of the page into one run-on block
                cr_l = _caps_ratio(l["text"])
                cr_b = _caps_ratio(b["lines"][-1]["text"])
                if cr_l is not None and cr_b is not None and \
                        ((cr_l >= 0.7 and cr_b < 0.45) or (cr_l < 0.45 and cr_b >= 0.7)):
                    continue
                if l["x0"] - b["x0"] > page_text_w * 0.03:
                    # indented line = new paragraph - EXCEPT centered display
                    # text (a two-line cover title, a centered address line):
                    # centered lines legitimately shift their left edge, so
                    # accept the join when the line CENTERS align instead
                    lc = (l["x0"] + l["x1"]) / 2
                    bc = (b["x0"] + b["x1"]) / 2
                    if abs(lc - bc) > max(8.0, (l["size"] or 8.0) * 0.6):
                        continue
                score = (ov / wmin, -gap)
                if best is None or score > best_score:
                    best, best_score = b, score
        if best is None:
            blk = {"lines": [l], "x0": l["x0"], "x1": l["x1"],
                   "top": l["top"], "bottom": l["bottom"],
                   "size_med": l["size"], "record": id(l) in record_rows,
                   "list": id(l) in list_rows}
            # a record row (TOC/table row) is sealed: one line = one block,
            # nothing may attach to it
            (done if id(l) in record_rows else open_blocks).append(blk)
        else:
            b = best
            b["lines"].append(l)
            b["x0"] = min(b["x0"], l["x0"]); b["x1"] = max(b["x1"], l["x1"])
            b["top"] = min(b["top"], l["top"]); b["bottom"] = max(b["bottom"], l["bottom"])
            b["size_med"] = statistics.median(
                [x["size"] for x in b["lines"] if x["size"]] or [0])
    # STACKED CELL MERGE: a table header cell often holds 2-3 stacked short
    # lines ("Reported" / "Operating" / "Income"). Each line got sealed as its
    # own record row (the header underline rule spans the row), which made
    # three one-line cells whose translations wrap chaotically. Vertically
    # adjacent record rows that share their x-range are ONE cell.
    # list ITEMS (parallel word-list rows) are independent entries, never the
    # stacked lines of one cell - exclude them or the whole column re-chains
    recs = [b for b in done + open_blocks
            if b.get("record") and not b.get("list")]
    recs.sort(key=lambda b: (b["x0"], b["top"]))
    merged_away = set()
    for a in recs:
        if id(a) in merged_away:
            continue
        changed = True
        while changed:
            changed = False
            for c in recs:
                if c is a or id(c) in merged_away or id(a) in merged_away:
                    continue
                ov = min(a["x1"], c["x1"]) - max(a["x0"], c["x0"])
                wmin = min(a["x1"] - a["x0"], c["x1"] - c["x0"]) or 1.0
                wmax = max(a["x1"] - a["x0"], c["x1"] - c["x0"])
                lh = max((x["bottom"] - x["top"]) for x in c["lines"])
                gap = max(c["top"] - a["bottom"], a["top"] - c["bottom"])
                s1 = a["size_med"] or 1.0
                s2 = c["size_med"] or 1.0
                # ONLY tight in-cell stacks: separate table/TOC rows have a
                # visible row gap (>=~0.5 line height) and are usually wide -
                # merging them would undo the row sealing entirely
                if ov >= 0.7 * wmin and gap <= 0.4 * lh and \
                        wmax <= 0.3 * page_text_w and \
                        max(s1, s2) / max(0.1, min(s1, s2)) <= 1.25:
                    a["lines"] += c["lines"]
                    a["x0"] = min(a["x0"], c["x0"]); a["x1"] = max(a["x1"], c["x1"])
                    a["top"] = min(a["top"], c["top"])
                    a["bottom"] = max(a["bottom"], c["bottom"])
                    merged_away.add(id(c))
                    changed = True
    blocks = []
    for b in done + open_blocks:
        if id(b) in merged_away:
            continue
        blk = _mkblock(b["lines"], 0)
        blk["col"] = assign_column(blk, mid, left, right)
        if b.get("record"):
            blk["record"] = True
        if b.get("list"):
            blk["list"] = True
        blocks.append(blk)
    return blocks

# Tokens that read as measurements/table cells alongside digits. Used to spot
# spec-table value columns ("22.83 m", "74 ft 11 in", "3 x 1,884 kW") that must
# stay verbatim (type "data"), on any document - not tied to one sample.
_UNIT_TOKEN_RE = re.compile(
    r"(?i)^(?:m|cm|mm|km|kg|g|lb|lbs|ft|in|kt|kts|kph|mph|nm|shp|hp|kw|kn|"
    r"s|sec|min|hr|hrs|hours?|x|×|/|%|°[cf]?|ft/min|m/s\d?|kg/m\d?)$")


def _numericish(text):
    # dot-leader tokens (". . . . ." in TOC rows) carry no meaning and must not
    # inflate the numeric ratio - "5-1-1. Preflight Preparation . . . . 2/20/25"
    # is a TOC TITLE row whose text needs translating, not table data
    toks = [t for t in text.split() if t.strip(".·⋅⋯") != ""]
    if not toks or not _DIGIT_ANY_RE.search(text):
        return False
    hits = sum(1 for t in toks
               if re.fullmatch(r"[\d.,%×x±+\-/()<>≈=]+", t) or _UNIT_TOKEN_RE.match(t))
    return hits / len(toks) > 0.5


_DIGIT_ANY_RE = re.compile(r"\d")


def _join_line_texts(lines):
    """Join a block's line texts. A URL split across lines must NOT get a space
    injected mid-path ("faa.gov/pilots/safety/ pilotsafetybrochures" - the
    fragment after the space escapes URL protection and gets translated), and a
    word hyphenated at the line end ("informa-/tion") is rejoined so the
    translation engine sees the real word."""
    text = ""
    for l in lines:
        t = l["text"]
        if not text:
            text = t
            continue
        m = re.search(r"\S+$", text)
        last = m.group() if m else ""
        if re.match(r"(?:https?://|www\.)", last) and last[-1] in "/-_=?&.":
            text += t                       # URL continues - no space
        elif text.endswith("-") and len(last) > 2 and last[:-1].isalpha() \
                and t[:1].islower():
            text = text[:-1] + t            # de-hyphenate a split word
        else:
            text += " " + t
    return text.strip()


def _mkblock(lines, col):
    lines = sorted(lines, key=lambda l: (l["top"], l["x0"]))
    from collections import Counter
    color = Counter(tuple(l.get("color", [0, 0, 0])) for l in lines).most_common(1)[0][0]
    return {
        "col": col,
        "color": list(color),
        "x0": min(l["x0"] for l in lines), "x1": max(l["x1"] for l in lines),
        "top": min(l["top"] for l in lines), "bottom": max(l["bottom"] for l in lines),
        "text": _join_line_texts(lines),
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
        # Band edges are the full-width blocks' own centers, so a full block is
        # always the TERMINATOR of its band - any column content in the same
        # band sits physically ABOVE it and must be read (and anchored) FIRST.
        # Emitting fulls first put a page-bottom footer before the columns,
        # which anchored the flow below them and overflowed the whole page.
        band_blocks = groups.get(band, [])
        left = sorted([b for b in band_blocks if b["col"] == 1], key=lambda b: b["top"])
        right = sorted([b for b in band_blocks if b["col"] == 2], key=lambda b: b["top"])
        ordered.extend(left); ordered.extend(right)
        for b in sorted(full_by_band.get(band, []), key=lambda b: b["top"]):
            ordered.append(b)
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
    # A bibliography exists ONLY after an explicit References/Bibliography
    # heading. Numbered-line patterns alone are NOT evidence: a regulation
    # manual's numbered paragraphs ("1. Accuracy. The accuracy of ...") would
    # otherwise classify half the document as untranslatable references.
    ref_heading_seen = False
    REF_HEAD_RE = re.compile(
        r"^\s*(?:[\divxlc]+[.)]?\s*)?(references|bibliography|works cited|"
        r"literature cited|further reading|sources and bibliography)\s*$", re.I)
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
        page_rules = horizontal_rules(page)
        blocks = group_blocks(lines, mid, left, right, body_size,
                              rules=page_rules)
        # reference zone: only at/after an explicit References heading, on a
        # page with many ref-pattern lines
        ref_head_top = None
        for l in lines:
            if REF_HEAD_RE.match(l["text"].strip()):
                ref_heading_seen = True
                ref_head_top = l["top"] if ref_head_top is None else \
                    min(ref_head_top, l["top"])
        is_ref = ref_heading_seen and \
            sum(1 for b in blocks if REF_RE.match(b["text"])) >= 3
        for b in blocks:
            zone = is_ref and (ref_head_top is None or b["top"] >= ref_head_top - 2)
            b["type"] = classify_block(b, body_size, pi, ph, zone)
        # A bibliography entry spans several lines but only the FIRST (numbered)
        # line matches REF_RE; the continuation lines fell through to "body" and got
        # translated. Once the numbered list has started it runs to the document end,
        # so propagate: reference/numbered lines mark the start (sticky across column
        # AND page breaks - a ref wrapping to the next column/page top has no number),
        # and subsequent body/heading lines become reference too. Only allow this
        # in the BACK HALF of the document: bibliographies live at the end, whereas
        # a numbered METHODS/protocol list ("1. Participants were...") is early and
        # would otherwise poison every following page as "reference" (untranslated).
        if ref_heading_seen and (is_ref or ref_started) and pi >= npages * 0.5:
            NUM_RE = re.compile(r"^\[?\d+[.\]]")
            for col in sorted({b.get("col", 0) for b in blocks}):
                cb = sorted((b for b in blocks if b.get("col", 0) == col),
                            key=lambda b: b["top"])
                for b in cb:
                    if b["top"] < (ref_head_top or -1) - 2 and not ref_started:
                        continue
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
        # figures from VECTOR DIAGRAMS: a dense cluster of curves (arcs, fans,
        # flow-chart links) is a drawing even with no raster image (an ILS
        # coverage fan). Its text is part of the artwork - translating it risks
        # tight-leading interleave corruption, and stripping it deletes labels
        # from the diagram - so such blocks become `data` (kept verbatim).
        # Straight-line grids (tables) never qualify: a diagram needs >=6 real
        # curves; a decorated text box (rounded corners = 4 arcs) does not.
        curves = list(page.curves or [])[:1500]
        if len(curves) >= 6:
            boxes = [[c["x0"], c["top"], c["x1"], c["bottom"], 1] for c in curves]
            merged_any = True
            while merged_any:
                merged_any = False
                out = []
                for bx in boxes:
                    hit = None
                    for o in out:
                        if not (bx[2] < o[0] - 12 or o[2] < bx[0] - 12 or
                                bx[3] < o[1] - 12 or o[3] < bx[1] - 12):
                            hit = o
                            break
                    if hit:
                        hit[0] = min(hit[0], bx[0]); hit[1] = min(hit[1], bx[1])
                        hit[2] = max(hit[2], bx[2]); hit[3] = max(hit[3], bx[3])
                        hit[4] += bx[4]
                        merged_any = True
                    else:
                        out.append(list(bx))
                boxes = out
            for bx in boxes:
                w, h = bx[2] - bx[0], bx[3] - bx[1]
                # 200-8000pt2 with many curves = a margin ICON (the TIP/
                # CAUTION roundel): registered as a small figure so the flow
                # never draws text across the artwork
                if bx[4] < 6 or w * h < 200 or w * h > 0.8 * pw * ph:
                    continue
                # a cluster that CONTAINS flowing body text is a decorated
                # panel (background swirl, callout box), not a diagram
                decorated = False
                for b in blocks:
                    ba = max(0.0, (b["x1"] - b["x0"])) * max(0.0, (b["bottom"] - b["top"]))
                    ix = max(0.0, min(b["x1"], bx[2]) - max(b["x0"], bx[0]))
                    iy = max(0.0, min(b["bottom"], bx[3]) - max(b["top"], bx[1]))
                    if ba and ix * iy >= 0.7 * ba and \
                            b.get("nlines", 1) >= 3 and \
                            (b.get("size") or 0) >= body_size * 0.9:
                        decorated = True
                        break
                if decorated:
                    continue
                figs.append({"type": "figure", "col": 0, "vector": True,
                             "x0": bx[0], "x1": bx[2], "top": bx[1],
                             "bottom": bx[3], "text": "", "order": None})
            # small text sitting inside a vector diagram is figure content -
            # incl. chart titles and source notes (multi-line but small type):
            # translating them draws Japanese over the chart's own lettering
            for b in blocks:
                if b["type"] not in ("body", "heading", "caption") or \
                        (b.get("size") or 0) > body_size * 1.15 or \
                        len(b["text"]) > 400:
                    continue
                ba = max(0.0, (b["x1"] - b["x0"])) * max(0.0, (b["bottom"] - b["top"]))
                if not ba:
                    continue
                for f in figs:
                    if not f.get("vector"):
                        continue
                    ix = max(0.0, min(b["x1"], f["x1"]) - max(b["x0"], f["x0"]))
                    iy = max(0.0, min(b["bottom"], f["bottom"]) - max(b["top"], f["top"]))
                    if ix * iy >= 0.7 * ba:
                        b["type"] = "data"
                        break
        rules = page_rules
        # drop TEXT UNDERLINES (link underlines, struck text): a rule lying
        # inside a text block's bbox belongs to the text, not the page
        # structure - as an obstacle it would shred the reflow capacity of a
        # link-dense page (every hyperlink underline cutting the column)
        rules = [r for r in rules
                 if not any(b["type"] in ("body", "heading", "caption", "title",
                                          "reference")
                            and b["x0"] - 2 <= r["x0"] and r["x1"] <= b["x1"] + 2
                            and b["top"] - 2 <= r["top"]
                            and r["bottom"] <= b["bottom"] + 3
                            for b in blocks)]
        ordered = reading_order(blocks)
        doc["pages"].append({
            "page": pi + 1, "width": pw, "height": ph,
            "x_off": float(page.bbox[0]), "y_off": float(page.bbox[1]),
            "top_off": 0.0,
            "columns": 2 if mid else 1, "body_size": body_size,
            "blocks": ordered, "figures": figs, "rules": rules,
        })
        # MEMORY: pdfplumber caches every page's parsed objects for the life of
        # the document - on a dense 200k+ char PDF that alone exceeds a small
        # host's RAM (Render 512MB OOM). Everything needed is now in `doc`;
        # release this page's caches before touching the next page.
        del chars, prelim, body_chars, lines, blocks, ordered
        page.flush_cache()
        page.get_textmap.cache_clear()
    pdf.close()

    # mark repeated headers/footers (same normalized text in the top OR bottom
    # margin zone on >=3 pages): running furniture, kept verbatim - a footer
    # like "Research Briefing, 27 Nov" typed as body would anchor below the
    # flow band and permanently overflow
    from collections import Counter
    def norm(t): return re.sub(r"[\d\s]", "", t).lower()[:40]

    def in_margin(b, p):
        # furniture is SMALL text: a chapter divider's 24pt display title
        # repeats the same words as the chapter's running heads, but it is the
        # page's content, not furniture - never seal display-size text
        if b.get("size") and p.get("body_size") and \
                b["size"] > p["body_size"] * 1.3:
            return False
        return (b["top"] < p["height"] * 0.15
                or b["bottom"] > p["height"] * 0.88)
    topcnt = Counter()
    for p in doc["pages"]:
        for b in p["blocks"]:
            if in_margin(b, p) and len(b["text"]) > 6:
                topcnt[norm(b["text"])] += 1
    repeated = {k for k, v in topcnt.items() if v >= 3}
    for p in doc["pages"]:
        for b in p["blocks"]:
            if in_margin(b, p) and norm(b["text"]) in repeated:
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
