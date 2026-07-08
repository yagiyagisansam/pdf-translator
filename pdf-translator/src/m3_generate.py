#!/usr/bin/env python3
# M3: Japanese PDF by removing English text in-place (no white box) + Japanese overlay.
#
# Key design decisions (hard-won; see SPEC.md):
# - English removal is CONTENT-BASED (string matching), never coordinate-based:
#   pdfplumber coords and content-stream coords disagree per page in this corpus.
# - Two-pass kill: pass 1 removes ops whose text is in the translated blob; pass 2
#   removes short stray fragments (superscripts, stat labels, citation ranges)
#   sandwiched between dropped ops in stream order.
# - Japanese overlay flows each stitched unit across its constituent block regions
#   (page/column groups) in reading order, clipped per region (no overlap ever).
import os, json, re
import pikepdf
from pikepdf import Pdf, parse_content_stream, unparse_content_stream
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import Color
from pypdf import PdfReader, PdfWriter

from config import OUT, ensure_out, resolve_pdf

def _register_fonts():
    reg = {}
    rpath = f"{OUT}/NotoJP-sub.ttf"
    bpath = f"{OUT}/NotoJP-Bold-sub.ttf"
    pdfmetrics.registerFont(TTFont("NotoJP", rpath)); reg["r"] = rpath
    try:
        pdfmetrics.registerFont(TTFont("NotoJP-Bold", bpath)); reg["b"] = bpath
    except Exception:
        pdfmetrics.registerFont(TTFont("NotoJP-Bold", rpath)); reg["b"] = rpath
    return reg

# ---- text normalization for content matching --------------------------------
_LIGATURES = {
    "\u02da": "fi",   # ˚ used as fi ligature in this font
    "\u02dc": "fl",   # ˜ used as fl ligature
    "\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff",
    "\ufb03": "ffi", "\ufb04": "ffl",
}
# Glyphs this font uses as punctuation/separators (NOT letters), which isalnum()
# would otherwise keep. Map to a dash so they normalize away like real dashes.
_SEPARATORS = {
    "\u0152": "-",    # Œ used as en-dash/range in this font
    "\u2122": "'",    # ™ used as a quote
    "\u201a": "'",    # ‚ used as a quote
}

def _expand_ligatures(s):
    for k, v in _LIGATURES.items():
        if k in s:
            s = s.replace(k, v)
    for k, v in _SEPARATORS.items():
        if k in s:
            s = s.replace(k, v)
    return s

# Ligature glyphs some fonts REUSE as quotation marks (ﬁ/ﬂ around a phrase). For
# those the expand-to-"fi"/"fl" normalization corrupts the match, so we also try a
# variant that DROPS them. A word like "signiﬁcant" (a real ligature) matches on
# the expand form; "ﬁerectile dysfunctionﬂ" (quote abuse) matches on the drop form.
_AMBIG_LIG = ("ﬁ", "ﬂ", "ﬀ", "ﬃ", "ﬄ", "˚", "˜")

def _norm_txt(s):
    # Expand known ligature glyphs, then keep only ASCII alphanumerics, lowercased.
    # ASCII-only matters for fonts WITHOUT a ToUnicode map: their raw-byte
    # punctuation glyphs decode to stray high-Latin letters (Ð Ô Õ for – ' •) that
    # isalnum() would keep and break the substring match. English body text is
    # ASCII, so restricting to ASCII drops those artifacts on both sides.
    s = _expand_ligatures(s)
    return "".join(ch.lower() for ch in s if ch.isascii() and ch.isalnum())

def _norm_txt_drop(s):
    # Same, but drop the ambiguous ligature/quote glyphs instead of expanding.
    for k, v in _SEPARATORS.items():
        s = s.replace(k, v)
    for k in _AMBIG_LIG:
        s = s.replace(k, "")
    return "".join(ch.lower() for ch in s if ch.isascii() and ch.isalnum())

_DIGIT_RE = re.compile(r"\d")

def _matches_blob(op_norm, blob, blob_drop=None, op_norm_drop=None,
                  blob_nodigit=None):
    """Kill an op if its normalized text is a substring of the translated-block
    blob, under either the expand-ligatures or the drop-ligatures normalization.
    Short ops (<3 chars) are only handled by the fragment pass.

    A third, digit-stripped pass handles superscript citations that the
    extractor interleaved INTO a word: the block text becomes garbled like
    "vol-umes2o2f7ju29mping" (the "20,27-29" cite woven through the letters),
    which the clean content-stream op "umesofjumping" can't match directly.
    Dropping digits from BOTH sides collapses the woven cite and restores the
    match. References carry the digits but are excluded from the blob, so this
    cannot spuriously delete reference text."""
    if len(op_norm) >= 3 and op_norm in blob:
        return True
    if blob_drop is not None and op_norm_drop and len(op_norm_drop) >= 3:
        if op_norm_drop in blob_drop:
            return True
    # Digit-stripped match handles a citation woven INTO a BLOCK word by the
    # extractor: the block text is garbled ("umes2o2f7ju29mping") but the actual
    # content-stream op is CLEAN ("umesofjumping"), so it matches only after the
    # blob's digits are stripped. Apply it ONLY to a clean op (no digits of its
    # own): a kept op that carries digits - a table cell "Group1", "Cell2019" -
    # is a different case and must NOT be digit-stripped, or "group"/"cell" would
    # spuriously match the body and erase kept text.
    if (blob_nodigit is not None and not _DIGIT_RE.search(op_norm)
            and len(op_norm) >= 5 and op_norm in blob_nodigit):
        return True
    return False

def _op_text(op):
    o = str(op.operator); a = op.operands
    if o == "TJ":
        t = ""
        for el in a[0]:
            if isinstance(el, pikepdf.String):
                t += str(el)
        return t
    if o == "Tj":
        return str(a[0])
    if o in ("'", '"'):
        return str(a[-1]) if a else ""
    return ""

def _mm(m, n):
    a,b,c,d,e,f=m; a2,b2,c2,d2,e2,f2=n
    return (a*a2+b*c2, a*b2+b*d2, c*a2+d*c2, c*b2+d*d2, e*a2+f*c2+e2, e*b2+f*d2+f2)

# fragments that are clearly part of body text when interspersed with it:
# citation numbers, stat labels, affiliation markers, common short connectors.
_FRAG_RE = re.compile(
    r"^(?:[a-d]|p|t|n|es|no|to|of|in|the|and|or|vs|"
    r"\d{1,4}|\d{1,3}[-–,]\d{1,3}|[±×<>=]+)$", re.I)

# Longest run of consecutive fragment ops the pass-2 sweep will drop as a single
# paragraph-internal cluster. Real clusters (author markers a-e, woven citations)
# are short; a longer contiguous run of frags is structured content (a numeric
# table) that must survive even when sandwiched between translated paragraphs.
_MAX_FRAG_RUN = 6

def _parse_tounicode(data):
    """Parse a /ToUnicode CMap stream into {code:int -> unicode:str}. Handles the
    common bfchar / bfrange forms; unknown constructs are skipped."""
    text = data.decode("latin-1", "replace")
    mp = {}

    def _u(hexstr):
        b = bytes.fromhex(hexstr)
        try:
            return b.decode("utf-16-be")
        except Exception:
            return ""

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mp[int(src, 16)] = _u(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        # <lo> <hi> <dststart>
        for lo, hi, ds in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            a, b = int(lo, 16), int(hi, 16)
            base = int(ds, 16)
            for k in range(a, b + 1):
                mp[k] = chr(base + (k - a)) if base + (k - a) < 0x110000 else ""
        # <lo> <hi> [<d0> <d1> ...]
        for lo, hi, arr in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S):
            a = int(lo, 16)
            dsts = re.findall(r"<([0-9A-Fa-f]+)>", arr)
            for off, d in enumerate(dsts):
                mp[a + off] = _u(d)
    return mp


def _page_font_decoders(page):
    """Return {font_name(str) -> (bytes_per_code:int, {code->unicode})} for fonts
    on the page that carry a /ToUnicode map. Fonts without one are omitted, and
    ops in them fall back to raw-byte matching (works for standard encodings)."""
    decoders = {}
    try:
        fonts = page.Resources.Font
    except Exception:
        return decoders
    for name, font in fonts.items():
        try:
            tu = font.get("/ToUnicode")
            if tu is None:
                continue
            width = 2 if str(font.get("/Subtype", "")) == "/Type0" else 1
            decoders[str(name)] = (width, _parse_tounicode(tu.read_bytes()))
        except Exception:
            continue
    return decoders


def _decode_op(op, cur_font, decoders):
    """Unicode text of a text-show op under the current font's ToUnicode map,
    or None if that font has no decoder (caller falls back to raw bytes)."""
    dec = decoders.get(cur_font)
    if dec is None:
        return None
    width, mp = dec
    out = []
    a = op.operands
    o = str(op.operator)
    strings = []
    if o == "TJ":
        strings = [el for el in a[0] if isinstance(el, pikepdf.String)]
    elif o == "Tj":
        strings = [a[0]]
    elif o in ("'", '"'):
        strings = [a[-1]] if a else []
    for s in strings:
        raw = bytes(s)
        for i in range(0, len(raw), width):
            code = int.from_bytes(raw[i:i + width], "big")
            out.append(mp.get(code, ""))
    return "".join(out)


def remove_text_by_content(page, owner, kill_blob, **kwargs):
    """Two-pass, coordinate-free English removal.
    Pass 1: drop any text-show op whose normalized text is inside kill_blob (the
            concatenated text of translated blocks).
    Pass 2: drop SHORT stray fragments (superscript citations, stat labels p/ES,
            affiliation markers a-d, symbols +/x/*) that are sandwiched between
            dropped body ops in stream order - pieces of a translated paragraph the
            font emitted as separate tiny ops. Running heads, page numbers and table
            text are NOT surrounded by dropped body text, so they are preserved."""
    blob_drop = kwargs.get("kill_blob_drop")
    blob_nodigit = _DIGIT_RE.sub("", kill_blob)
    decoders = _page_font_decoders(page)
    ops = list(parse_content_stream(page))
    is_text=[False]*len(ops); dropped=[False]*len(ops)
    op_uni=[None]*len(ops)   # decoded Unicode text per text op (for the frag pass)
    cur_font=None
    for i,op in enumerate(ops):
        o=str(op.operator)
        if o=="Tf" and op.operands:
            cur_font=str(op.operands[0])
            continue
        if o in ("Tj","TJ","'",'"'):
            is_text[i]=True
            # decode via the font's ToUnicode (subsetted fonts); else raw bytes
            uni=_decode_op(op, cur_font, decoders)
            t=uni if uni is not None else _op_text(op)
            op_uni[i]=t
            if _matches_blob(_norm_txt(t), kill_blob,
                             blob_drop, _norm_txt_drop(t),
                             blob_nodigit=blob_nodigit):
                dropped[i]=True
    text_idx=[i for i in range(len(ops)) if is_text[i]]
    pos={idx:k for k,idx in enumerate(text_idx)}

    def _is_frag(idx):
        """A short stray fragment: <=3 normalized chars, or matches _FRAG_RE
        (citation numbers, stat labels p/ES, affiliation a-d, symbols)."""
        raw=(op_uni[idx] if op_uni[idx] is not None else _op_text(ops[idx])).strip()
        if not raw: return True   # empty/whitespace op flows with its neighbours
        raw_sep=_expand_ligatures(raw)
        norm=_norm_txt(raw)
        return len(norm)<=3 or bool(_FRAG_RE.match(raw_sep))

    for i in text_idx:
        if dropped[i]: continue
        if not _is_frag(i): continue
        k=pos[i]
        # Scan left/right SKIPPING over consecutive fragment ops to the nearest
        # NON-fragment text op. Drop this fragment only if the body text bounding
        # its run was itself dropped on BOTH sides - i.e. it is wedged inside a
        # translated paragraph. Runs of fragments (e.g. "a,b,c,d" author markers,
        # "1-3" citations) are killed together; running heads / page numbers,
        # bounded by surviving text, are preserved.
        l=k-1
        while l>=0 and _is_frag(text_idx[l]): l-=1
        prevd = l>=0 and dropped[text_idx[l]]
        r=k+1
        while r<len(text_idx) and _is_frag(text_idx[r]): r+=1
        nextd = r<len(text_idx) and dropped[text_idx[r]]
        # A paragraph-internal fragment cluster (superscript cites, author
        # markers, stat symbols) is SHORT. A long run of fragments wedged between
        # two translated paragraphs is structured content - a numeric table drawn
        # in stream order between body paragraphs, e.g. a grid of cells like
        # "12","34","5-10" that all match _FRAG_RE - and must be preserved, not
        # swept. Bound the run length so tables survive while real clusters die.
        run_len = (r - 1) - (l + 1) + 1   # count of consecutive frags l+1..r-1
        if prevd and nextd and run_len <= _MAX_FRAG_RUN:
            dropped[i]=True
    out=[op for i,op in enumerate(ops) if not dropped[i]]
    page.Contents = owner.make_stream(unparse_content_stream(out))

# ---- Japanese text layout ----------------------------------------------------
# Per-(font, size) character-width cache. reportlab's stringWidth is a plain sum
# of glyph advances (no kerning), so accumulating cached per-char widths is exact
# and turns _wrap from O(len^2) stringWidth work into O(len) dict lookups. _wrap
# is called ~20x per unit during the font-size search, so this dominates M3 time.
_W_CACHE = {}

def _wrap(text, font, size, max_w):
    cache = _W_CACHE.setdefault((font, size), {})
    lines=[]; cur=""; cur_w=0.0
    for ch in text:
        if ch=="\n": lines.append(cur); cur=""; cur_w=0.0; continue
        w = cache.get(ch)
        if w is None:
            w = cache[ch] = stringWidth(ch, font, size)
        if cur_w + w <= max_w:
            cur += ch; cur_w += w
        else:
            lines.append(cur); cur = ch; cur_w = w
    if cur: lines.append(cur)
    return lines

def _flow_unit_across_regions(text, btype, regs, layout, per_page_draws):
    """Flow `text` across the unit's regions (in reading order). Pick the largest
    font (capped) such that all wrapped lines fit within the total region capacity,
    then lay lines into region 1, overflow into region 2, etc. Line slots skip
    the region's obstacle bands (foreign blocks/figures inside the box), so no
    overlap with neighbours or figures is possible."""
    if not text:
        return
    font = "NotoJP-Bold" if btype in ("heading", "title") else "NotoJP"
    FLOOR = 5.5
    CAP = 10.5

    def region_slots(rb, size):
        """y positions where a line of this size can be drawn: a fixed grid from
        the region top, minus slots that intersect an obstacle band."""
        lh = size * 1.16
        obstacles = rb.get("obstacles", ())
        slots = []
        y = rb["top"]
        while y + lh <= rb["avail_bottom"] + 0.1:
            if not any(y < ob and y + lh > ot for (ot, ob) in obstacles):
                slots.append(y)
            y += lh
        return slots, lh

    def total_fits(size):
        remaining = text
        for (pi, rb) in regs:
            w = max(10, rb["x1"] - rb["x0"])
            slots, lh = region_slots(rb, size)
            lines = _wrap(remaining, font, size, w)
            if len(lines) <= len(slots):
                return True
            remaining = _remaining_after(remaining, lines[:len(slots)])
            if not remaining:
                return True
        return len(_wrap(remaining, font, size,
                         max(10, regs[-1][1]["x1"]-regs[-1][1]["x0"]))) == 0

    size = CAP
    while size >= FLOOR:
        if total_fits(size):
            break
        size -= 0.25
    size = max(FLOOR, size)

    # distribute lines into each region's free slots
    remaining = text
    for ri, (pi, rb) in enumerate(regs):
        w = max(10, rb["x1"] - rb["x0"])
        slots, lh = region_slots(rb, size)
        if not slots:
            continue
        lines = _wrap(remaining, font, size, w)
        take = lines[:len(slots)]
        for y, ln in zip(slots, take):
            per_page_draws[pi].append({"x": rb["x0"], "y_top": y, "size": size,
                                       "font": font, "line": ln})
        remaining = _remaining_after(remaining, take)
        if not remaining:
            break

def _remaining_after(text, taken_lines):
    """Return the suffix of text after removing the characters in taken_lines.
    _wrap inserts breaks but never adds/removes chars, so char counting is exact."""
    n = sum(len(l) for l in taken_lines)
    return text[n:]

# ---- main pipeline -------------------------------------------------------------
def generate(name, src_path):
    ensure_out()
    _register_fonts()
    units=json.load(open(f"{OUT}/{name}_bilingual.json"))
    layout=json.load(open(f"{OUT}/{name}_layout.json"))
    unit_for_block={}
    for u in units:
        if not u.get("target"): continue
        for sid in u["spans"]: unit_for_block[sid]=u
    TRANS={"body","heading","caption","title"}

    def overlaps_x(a0, a1, b0, b1):
        return not (a1 <= b0 + 1 or b1 <= a0 + 1)

    # A flow-region is the bbox of one or more contiguous blocks of a unit in the
    # same column on one page. The unit's text is flowed across its regions.
    def region_box(pi, blocks_in_region, blocks_all, figs, btype):
        x0 = min(b["x0"] for b in blocks_in_region)
        x1 = max(b["x1"] for b in blocks_in_region)
        top = min(b["top"] for b in blocks_in_region)
        bottom = max(b["bottom"] for b in blocks_in_region)
        own = set(id(b) for b in blocks_in_region)
        # Headings/titles have tight English boxes; a Japanese heading can be wider.
        # Extend the usable width rightward to the right edge of body text in the
        # same column so the heading renders on one line instead of wrapping & clipping.
        if btype in ("heading", "title"):
            col_right = x1
            for ob in blocks_all:
                if ob["type"] in ("body", "heading", "caption") and \
                   ob["x0"] >= x0 - 4 and abs(ob["top"] - top) < 400 and ob["x1"] > col_right:
                    if ob["x0"] < x0 + 40:
                        col_right = max(col_right, ob["x1"])
            x1 = max(x1, col_right)
        FIG_MARGIN = 8.0
        CAP_CLEAR = 26.0
        # push start below an overlapping figure (captions need more clearance:
        # chart axis labels render slightly OUTSIDE the detected image bbox)
        for f in figs:
            if not overlaps_x(x0, x1, f["x0"], f["x1"]):
                continue
            clear = CAP_CLEAR if btype == "caption" else FIG_MARGIN
            fb = f["bottom"] + clear
            if top < fb and bottom > f["top"] - 2 and fb < bottom + 80:
                top = max(top, fb)
        # nearest element below that overlaps horizontally => available bottom.
        # Blocks that merely ABUT the unit's bbox (gap < 0.5pt, e.g. the keywords
        # line right under an abstract) are obstacles too - skipping them let
        # longer Japanese flow into the neighbour's space. Only elements clearly
        # above the unit's bottom are ignored; the max(bottom, ...) floor below
        # keeps slight source overlaps from shrinking the region.
        h = layout["pages"][pi]["height"]
        avail_bottom = h
        for ob in blocks_all:
            if id(ob) in own or ob["top"] <= bottom - 2.0:
                continue
            if overlaps_x(x0, x1, ob["x0"], ob["x1"]):
                avail_bottom = min(avail_bottom, ob["top"])
        for f in figs:
            if f["top"] <= bottom - 2.0:
                continue
            if overlaps_x(x0, x1, f["x0"], f["x1"]):
                avail_bottom = min(avail_bottom, f["top"] - FIG_MARGIN)
        avail_bottom = max(bottom, avail_bottom - 2.0)
        return {"x0": x0, "x1": x1, "top": top, "avail_bottom": avail_bottom,
                "own": own, "page": pi, "obstacles": []}

    # Per page, the concatenated normalized text of all TRANSLATED blocks.
    # The content-based kill removes any text-show op whose text is part of this blob.
    kill_blob = {pi: "" for pi in range(len(layout["pages"]))}
    kill_blob_drop = {pi: "" for pi in range(len(layout["pages"]))}
    unit_regions = {}   # uid -> (unit, regions)
    for pi, p in enumerate(layout["pages"]):
        for bi, b in enumerate(p["blocks"]):
            if b["type"] not in TRANS:
                continue
            sid = f"{pi}:{bi}"
            u = unit_for_block.get(sid)
            if not u:
                continue
            kill_blob[pi] += _norm_txt(b["text"])
            kill_blob_drop[pi] += _norm_txt_drop(b["text"])

    for u in units:
        if not u.get("target"):
            continue
        # gather this unit's blocks in span (reading) order, grouped by (page, column)
        groups = []
        for s in u["spans"]:
            spi, sbi = map(int, s.split(":"))
            blk = layout["pages"][spi]["blocks"][sbi]
            col = blk.get("col", 0)
            if groups and groups[-1][0] == spi and groups[-1][2] == col:
                groups[-1][1].append(blk)
            else:
                groups.append((spi, [blk], col))
        regs = []
        for (spi, blks, col) in groups:
            pg = layout["pages"][spi]
            rb = region_box(spi, blks, pg["blocks"], pg.get("figures", []), u["type"])
            regs.append((spi, rb))
        unit_regions[u["uid"]] = (u, regs)

    # Second pass: obstacle bands per region. A region's line slots must skip
    # (a) every OTHER region's box (that's where another unit's Japanese will be
    # drawn - the title page can float a small block like "Abstract" inside a
    # larger stitched unit's box), (b) blocks whose English stays visible
    # (data/running heads/untranslated units), and (c) figures.
    regs_by_page = {}
    for _, regs in unit_regions.values():
        for pi, rb in regs:
            regs_by_page.setdefault(pi, []).append(rb)
    translated_blocks = set()
    for _, regs in unit_regions.values():
        for pi, rb in regs:
            translated_blocks |= rb["own"]
    for pi, p in enumerate(layout["pages"]):
        page_regs = regs_by_page.get(pi, [])
        visible = [b for b in p["blocks"] if id(b) not in translated_blocks]
        for rb in page_regs:
            bands = rb["obstacles"]
            for rb2 in page_regs:
                if rb2 is rb:
                    continue
                if overlaps_x(rb["x0"], rb["x1"], rb2["x0"], rb2["x1"]):
                    bands.append((rb2["top"] - 1.0, rb2["avail_bottom"] + 1.0))
            for b in visible:
                if overlaps_x(rb["x0"], rb["x1"], b["x0"], b["x1"]):
                    bands.append((b["top"] - 1.0, b["bottom"] + 1.0))
            for f in p.get("figures", []):
                if overlaps_x(rb["x0"], rb["x1"], f["x0"], f["x1"]):
                    bands.append((f["top"] - 8.0, f["bottom"] + 8.0))
            # keep only bands that actually cut into this region's slot range,
            # and never let a band that covers the region's own start erase the
            # whole region (mutually-overlapping source boxes would deadlock)
            rb["obstacles"] = [(t, b) for (t, b) in bands
                               if b > rb["top"] + 1.0 and t < rb["avail_bottom"] - 1.0
                               and t > rb["top"] + 1.0]

    # 1) strip English in-place (content-based, coordinate-free)
    pdf=Pdf.open(src_path)
    for pi,page in enumerate(pdf.pages):
        if kill_blob.get(pi):
            remove_text_by_content(page, pdf, kill_blob[pi],
                                   kill_blob_drop=kill_blob_drop[pi])
    stripped=f"{OUT}/{name}_stripped.pdf"; pdf.save(stripped); pdf.close()

    # 2) overlay - flow each unit across its regions
    overlay=f"{OUT}/{name}_overlay.pdf"
    rd=PdfReader(src_path)
    npages=len(rd.pages)
    page_sizes=[]
    for page in rd.pages:
        mb=page.mediabox
        page_sizes.append((float(mb.width),float(mb.height),float(mb.left),float(mb.bottom)))
    per_page_draws={pi:[] for pi in range(npages)}
    for uid,(u,regs) in unit_regions.items():
        _flow_unit_across_regions(u["target"], u["type"], regs, layout, per_page_draws)

    # Persist the actually-drawn line boxes so M4 can verify text/figure overlap
    # on real placement data instead of the pre-layout block bboxes.
    placed = {}
    for pi, draws in per_page_draws.items():
        placed[str(pi + 1)] = [
            {"x0": d["x"], "top": d["y_top"],
             "x1": d["x"] + stringWidth(d["line"], d["font"], d["size"]),
             "bottom": d["y_top"] + d["size"] * 1.16,
             "text": d["line"][:40]}
            for d in draws]
    with open(f"{OUT}/{name}_placed.json", "w") as f:
        json.dump(placed, f)
    c=None
    for pi in range(npages):
        pw,ph,xo,yo=page_sizes[pi]
        if c is None: c=canvas.Canvas(overlay,pagesize=(pw,ph))
        else: c.setPageSize((pw,ph))
        # The overlay merges into the page's ABSOLUTE space, and pdfplumber x/y are
        # already absolute (they include the mediabox origin), so we must NOT
        # subtract the mediabox offset: x = d["x"], baseline y = ph - y_top - size.
        # Subtracting xo shoved every line left by the mediabox origin (e.g. 42pt)
        # on PDFs whose mediabox is not at x=0 -> the body looked "左寄り".
        for d in per_page_draws[pi]:
            c.setFillColor(Color(0,0,0)); c.setFont(d["font"], d["size"])
            c.drawString(d["x"], ph - d["y_top"] - d["size"], d["line"])
        c.showPage()
    c.save()

    # 3) merge overlay onto stripped pages.
    # Append the base to the writer FIRST, then merge onto the writer's pages.
    # Merging onto reader pages and add_page()-ing them afterwards silently drops
    # the overlay on every page after the first with pypdf 6.x (shared overlay
    # font resources don't survive the reader-page -> writer copy).
    over=PdfReader(overlay); w=PdfWriter()
    w.append(PdfReader(stripped))
    for i,page in enumerate(w.pages):
        if i<len(over.pages): page.merge_page(over.pages[i])
    out_path=f"{OUT}/{name}_ja.pdf"
    with open(out_path,"wb") as f: w.write(f)
    return out_path, stripped

if __name__=="__main__":
    import argparse
    ap = argparse.ArgumentParser(description="M3: bilingual + layout + source PDF -> <name>_ja.pdf")
    ap.add_argument("inputs", nargs="*", default=["paper"],
                    help="PDF paths or sample names (default: paper)")
    ap.add_argument("--name", help="override document name (single input only)")
    args = ap.parse_args()
    if args.name and len(args.inputs) != 1:
        ap.error("--name requires exactly one input")
    for inp in args.inputs:
        src, name = resolve_pdf(inp)
        if args.name:
            name = args.name
        out,stripped=generate(name, src)
        a=os.path.getsize(src); b=os.path.getsize(out)
        print(f"[{name}] source={a/1024:.0f}KB -> japanese={b/1024:.0f}KB ({out})")
