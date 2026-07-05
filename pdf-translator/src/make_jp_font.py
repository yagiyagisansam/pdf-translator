#!/usr/bin/env python3
# Build small TrueType-outline JP fonts (regular+bold) subset to the characters used,
# converting Noto CJK CFF outlines -> TrueType glyf so reportlab can embed them.
import json, os
from fontTools.ttLib import TTCollection, TTFont, newTable
from fontTools.subset import Subsetter, Options
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

from config import OUT, ensure_out

# Common install locations for Noto Sans CJK. Override with env vars
# NOTO_CJK_REGULAR / NOTO_CJK_BOLD when the fonts live elsewhere.
_FONT_CANDIDATES = {
    "regular": [
        os.environ.get("NOTO_CJK_REGULAR", ""),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        os.path.expanduser("~/Library/Fonts/NotoSansCJK-Regular.ttc"),
        "/Library/Fonts/NotoSansCJK-Regular.ttc",
    ],
    "bold": [
        os.environ.get("NOTO_CJK_BOLD", ""),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        os.path.expanduser("~/Library/Fonts/NotoSansCJK-Bold.ttc"),
        "/Library/Fonts/NotoSansCJK-Bold.ttc",
    ],
}

def find_font(weight):
    for p in _FONT_CANDIDATES[weight]:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        f"Noto Sans CJK ({weight}) not found. Install fonts-noto-cjk "
        f"(Debian/Ubuntu: sudo apt-get install fonts-noto-cjk) or set "
        f"NOTO_CJK_{weight.upper()} to the .ttc/.otf path.")

BASE_CHARS = ("　、。・！？（）「」『』ー％＝±×〜0123456789.,;:%()<>=±/-"
              "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ")

def used_chars(names):
    chars = set(BASE_CHARS)
    for name in names:
        for suffix in ("bilingual", "units"):
            try:
                for u in json.load(open(f"{OUT}/{name}_{suffix}.json")):
                    if u.get("target"):
                        chars |= set(u["target"])
            except FileNotFoundError:
                pass
    return chars

def cff_to_glyf(font):
    """Convert a CFF-outline TTFont to TrueType glyf outlines in place."""
    glyphSet=font.getGlyphSet()
    glyphOrder=font.getGlyphOrder()
    glyf=newTable("glyf"); glyf.glyphs={}; glyf.glyphOrder=glyphOrder
    for gname in glyphOrder:
        g=glyphSet[gname]
        ttpen=TTGlyphPen(glyphSet)
        cu=Cu2QuPen(ttpen, max_err=1.0, reverse_direction=True)
        try: g.draw(cu)
        except Exception: pass
        glyf.glyphs[gname]=ttpen.glyph()
    font["glyf"]=glyf
    # add empty loca; fontTools fills it when compiling glyf
    if "loca" not in font:
        font["loca"]=newTable("loca")
    # drop CFF-specific leftovers
    for t in ("VORG","CFF ","CFF2"):
        if t in font: del font[t]
    # required tables for TTF
    head=font["head"]; head.indexToLocFormat=0
    # maxp v1.0 for glyf
    maxp=font["maxp"]; maxp.tableVersion=0x00010000
    maxp.maxZones=1; maxp.maxTwilightPoints=0; maxp.maxStorage=0
    maxp.maxFunctionDefs=0; maxp.maxInstructionDefs=0; maxp.maxStackElements=0
    maxp.maxSizeOfInstructions=0; maxp.maxComponentElements=0; maxp.maxComponentDepth=0
    font.sfntVersion="\x00\x01\x00\x00"
    return font

def _load_font(src_path, subfont_index):
    if src_path.lower().endswith(".ttc"):
        return TTCollection(src_path).fonts[subfont_index]
    return TTFont(src_path)

def build(src_path, out_path, subfont_index, chars):
    font=_load_font(src_path, subfont_index)
    # subset first (on CFF) to keep it small and fast
    opt=Options(); opt.glyph_names=True; opt.notdef_outline=True
    opt.recalc_bounds=True; opt.drop_tables=[]
    ss=Subsetter(options=opt)
    ss.populate(text="".join(sorted(chars)))
    ss.subset(font)
    cff_to_glyf(font)
    font.save(out_path)
    # round-trip to force loca/glyf recompilation and clean structure
    f2 = TTFont(out_path)
    f2.save(out_path)
    print("saved", out_path, "glyphs:", len(font.getGlyphOrder()))

def make_fonts(names):
    ensure_out()
    ch = used_chars(names)
    print("unique chars:", len(ch))
    build(find_font("regular"), f"{OUT}/NotoJP-sub.ttf", 0, ch)
    build(find_font("bold"), f"{OUT}/NotoJP-Bold-sub.ttf", 0, ch)

if __name__=="__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Build subset JP fonts for the overlay")
    ap.add_argument("names", nargs="*", default=["paper", "deck"],
                    help="document names whose translations feed the subset (default: paper deck)")
    args = ap.parse_args()
    make_fonts(args.names)
