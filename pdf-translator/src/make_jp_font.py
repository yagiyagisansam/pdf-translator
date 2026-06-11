#!/usr/bin/env python3
# Build small TrueType-outline JP fonts (regular+bold) subset to the characters used,
# converting Noto CJK CFF outlines -> TrueType glyf so reportlab can embed them.
import json
from fontTools.ttLib import TTCollection, TTFont, newTable
from fontTools.subset import Subsetter, Options
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.ttGlyphPen import TTGlyphPen

OUT="/home/claude/analysis"

def used_chars():
    chars=set("　、。・！？（）「」『』ー％＝±×〜0123456789.,;:%()<>=±/-")
    for name in ["paper","deck"]:
        try:
            for u in json.load(open(f"{OUT}/{name}_bilingual.json")):
                if u.get("target"): chars|=set(u["target"])
            for u in json.load(open(f"{OUT}/{name}_units.json")):
                if u.get("target"): chars|=set(u["target"])
        except FileNotFoundError: pass
    chars|=set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz ")
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

def build(src_index_path, out_path, subfont_index, chars):
    ttc=TTCollection(src_index_path)
    font=ttc.fonts[subfont_index]
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

if __name__=="__main__":
    ch=used_chars()
    print("unique chars:", len(ch))
    build("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", f"{OUT}/NotoJP-sub.ttf", 0, ch)
    build("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", f"{OUT}/NotoJP-Bold-sub.ttf", 0, ch)
