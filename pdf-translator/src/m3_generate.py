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
import os, json, glob, re
import pikepdf
from pikepdf import Pdf, parse_content_stream, unparse_content_stream
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.colors import Color
from pypdf import PdfReader, PdfWriter

OUT = "/home/claude/analysis"

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

def _norm_txt(s):
    # Expand known ligature glyphs, then keep only alphanumerics, lowercased.
    s = _expand_ligatures(s)
    return "".join(ch.lower() for ch in s if ch.isalnum())

def _matches_blob(op_norm, blob):
    """Kill an op if its normalized text is a substring of the translated-block blob.
    Ligatures are already expanded in _norm_txt, so plain containment is reliable.
    Short ops (<3 chars) are only handled by the fragment pass."""
    if len(op_norm) < 3:
        return False
    return op_norm in blob

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

def remove_text_by_content(page, owner, kill_blob):
    """Two-pass, coordinate-free English removal.
    Pass 1: drop any text-show op whose normalized text is inside kill_blob (the
            concatenated text of translated blocks).
    Pass 2: drop SHORT stray fragments (superscript citations, stat labels p/ES,
            affiliation markers a-d, symbols +/x/*) that are sandwiched between
            dropped body ops in stream order - pieces of a translated paragraph the
            font emitted as separate tiny ops. Running heads, page numbers and table
            text are NOT surrounded by dropped body text, so they are preserved."""
    ops = list(parse_content_stream(page))
    is_text=[False]*len(ops); dropped=[False]*len(ops)
    for i,op in enumerate(ops):
        if str(op.operator) in ("Tj","TJ","'",'"'):
            is_text[i]=True
            if _matches_blob(_norm_txt(_op_text(op)), kill_blob):
                dropped[i]=True
    text_idx=[i for i in range(len(ops)) if is_text[i]]
    pos={idx:k for k,idx in enumerate(text_idx)}
    for i in text_idx:
        if dropped[i]: continue
        raw=_op_text(ops[i]).strip()
        raw_sep=_expand_ligatures(raw)   # turns the Œ-dash into '-', etc.
        norm=_norm_txt(raw)
        if len(norm)>3 and not _FRAG_RE.match(raw_sep): continue
        k=pos[i]
        prevd = k-1>=0 and dropped[text_idx[k-1]]
        nextd = k+1<len(text_idx) and dropped[text_idx[k+1]]
        if prevd and nextd:
            dropped[i]=True
    out=[op for i,op in enumerate(ops) if not dropped[i]]
    page.Contents = owner.make_stream(unparse_content_stream(out))

# ---- Japanese text layout ----------------------------------------------------
def _wrap(text, font, size, max_w):
    lines=[]; cur=""
    for ch in text:
        if ch=="\n": lines.append(cur); cur=""; continue
        if stringWidth(cur+ch,font,size)<=max_w: cur+=ch
        else: lines.append(cur); cur=ch
    if cur: lines.append(cur)
    return lines

def _flow_unit_across_regions(text, btype, regs, layout, per_page_draws):
    """Flow `text` across the unit's regions (in reading order). Pick the largest
    font (capped) such that all wrapped lines fit within the total region capacity,
    then lay lines into region 1, overflow into region 2, etc. Every region is
    clipped to its own collision-free box, so no overlap with neighbours/figures."""
    if not text:
        return
    font = "NotoJP-Bold" if btype in ("heading", "title") else "NotoJP"
    FLOOR = 5.5
    CAP = 10.5

    def region_capacity(rb, size):
        lh = size * 1.16
        h = max(0, rb["avail_bottom"] - rb["top"])
        return max(0, int(h // lh)), lh

    def total_fits(size):
        remaining = text
        for (pi, rb) in regs:
            w = max(10, rb["x1"] - rb["x0"])
            cap, lh = region_capacity(rb, size)
            lines = _wrap(remaining, font, size, w)
            if len(lines) <= cap:
                return True
            remaining = _remaining_after(remaining, lines[:cap])
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

    # distribute lines, clipping every region to its capacity
    remaining = text
    for ri, (pi, rb) in enumerate(regs):
        w = max(10, rb["x1"] - rb["x0"])
        cap, lh = region_capacity(rb, size)
        if cap <= 0:
            continue
        lines = _wrap(remaining, font, size, w)
        take = lines[:cap]
        y = rb["top"]
        for ln in take:
            per_page_draws[pi].append({"x": rb["x0"], "y_top": y, "size": size,
                                       "font": font, "line": ln})
            y += lh
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
        # nearest element below that overlaps horizontally => available bottom
        h = layout["pages"][pi]["height"]
        avail_bottom = h
        for ob in blocks_all:
            if id(ob) in own or ob["top"] <= bottom + 0.5:
                continue
            if overlaps_x(x0, x1, ob["x0"], ob["x1"]):
                avail_bottom = min(avail_bottom, ob["top"])
        for f in figs:
            if f["top"] <= bottom + 0.5:
                continue
            if overlaps_x(x0, x1, f["x0"], f["x1"]):
                avail_bottom = min(avail_bottom, f["top"] - FIG_MARGIN)
        avail_bottom = max(bottom, avail_bottom - 2.0)
        return {"x0": x0, "x1": x1, "top": top, "avail_bottom": avail_bottom}

    # Per page, the concatenated normalized text of all TRANSLATED blocks.
    # The content-based kill removes any text-show op whose text is part of this blob.
    kill_blob = {pi: "" for pi in range(len(layout["pages"]))}
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

    # 1) strip English in-place (content-based, coordinate-free)
    pdf=Pdf.open(src_path)
    for pi,page in enumerate(pdf.pages):
        if kill_blob.get(pi):
            remove_text_by_content(page, pdf, kill_blob[pi])
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
    c=None
    for pi in range(npages):
        pw,ph,xo,yo=page_sizes[pi]
        if c is None: c=canvas.Canvas(overlay,pagesize=(pw,ph))
        else: c.setPageSize((pw,ph))
        # reportlab canvas origin is mediabox bottom-left; canvas spans mediabox.
        # content y for a pdfplumber 'top' is (mb_top - top); reportlab y is that
        # minus mb_bottom => mediabox_height - top. So baseline = ph - y_top - size.
        for d in per_page_draws[pi]:
            c.setFillColor(Color(0,0,0)); c.setFont(d["font"], d["size"])
            c.drawString(d["x"] - xo, ph - d["y_top"] - d["size"], d["line"])
        c.showPage()
    c.save()

    # 3) merge overlay onto stripped pages
    base=PdfReader(stripped); over=PdfReader(overlay); w=PdfWriter()
    for i,page in enumerate(base.pages):
        if i<len(over.pages): page.merge_page(over.pages[i])
        w.add_page(page)
    out_path=f"{OUT}/{name}_ja.pdf"
    with open(out_path,"wb") as f: w.write(f)
    return out_path, stripped

if __name__=="__main__":
    import sys
    names = sys.argv[1:] or ["paper"]
    SRC = {
        "paper": "/mnt/user-data/uploads/The_effect_of_assisted_jumping_on_vertical_jump_height_in_high-performance_volleyball_players.pdf",
        "deck": "/mnt/user-data/uploads/NASA-Navy_telemedicine__Autogenic_feedback_training_exercises_for_motion_sickness.pdf",
    }
    for name in names:
        out,stripped=generate(name, SRC[name])
        a=os.path.getsize(SRC[name]); b=os.path.getsize(out)
        print(f"[{name}] source={a/1024:.0f}KB -> japanese={b/1024:.0f}KB ({out})")
