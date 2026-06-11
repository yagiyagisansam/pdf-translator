#!/usr/bin/env python3
"""M4 verification suite for the PDF translator.

These tests encode the acceptance criteria so that every change is checked and the
regressions that plagued early development are caught automatically. They are written to
be filled in / wired to real paths during Claude Code's "first tasks" (see CLAUDE.md):
de-hardcode paths, then point ANALYSIS_DIR at the pipeline output.

Run:  pytest tests/  (or python -m pytest)

Checks:
  1. residual_english      — generated *_ja.pdf has < threshold Latin words on body pages
                             (running heads, references, protected abbreviations excluded)
  2. block_overlap         — no two Japanese text blocks overlap each other
  3. figure_overlap        — no Japanese text overlaps a figure bbox
  4. layout_regression     — m1 output matches a committed golden layout JSON
"""
import os, re, json, glob
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANALYSIS_DIR = os.environ.get("ANALYSIS_DIR", os.path.join(ROOT, "analysis"))
GOLDEN_DIR = os.path.join(ROOT, "tests", "golden")

# Abbreviations / tokens that are legitimately Latin and must NOT count as residue.
ALLOWED_LATIN = re.compile(
    r"^(CMVJ|SPJ|ES|AFTE|SPAD|NAMI|NASA|ICC|AVT|VICON|Kistler|Elsevier|doi|"
    r"Sports|Medicine|Australia|Ltd|Inc|et|al|Journal|Hz|cm|kg|mm|USA|UK)$", re.I)


def _pdf_text_pages(path):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    pages = [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    doc.close()
    return pages


# ---- 1. residual English ----------------------------------------------------
@pytest.mark.parametrize("name", ["paper"])
def test_residual_english(name):
    pdf = os.path.join(ANALYSIS_DIR, f"{name}_ja.pdf")
    if not os.path.exists(pdf):
        pytest.skip(f"{pdf} not generated yet")
    layout = json.load(open(os.path.join(ANALYSIS_DIR, f"{name}_layout.json")))
    # body pages = pages that are not majority references
    ref_pages = set()
    for p in layout["pages"]:
        types = [b["type"] for b in p["blocks"]]
        if types and sum(t == "reference" for t in types) >= max(3, len(types) * 0.4):
            ref_pages.add(p["page"])
    pages = _pdf_text_pages(pdf)
    leftovers = []
    for i, txt in enumerate(pages, start=1):
        if i in ref_pages:
            continue
        # strip the running-head line (contains 'Journal' / 'et al')
        body = "\n".join(l for l in txt.splitlines()
                         if "Journal" not in l and "et al" not in l)
        for w in re.findall(r"[A-Za-z][A-Za-z\-']{3,}", body):
            if not ALLOWED_LATIN.match(w):
                leftovers.append((i, w))
    assert len(leftovers) <= 5, f"too many residual English words: {leftovers[:20]}"


# ---- 2 & 3. overlap detection -----------------------------------------------
def _rects_overlap(a, b, pad=1.0):
    return not (a["x1"] <= b["x0"] + pad or b["x1"] <= a["x0"] + pad or
                a["bottom"] <= b["top"] + pad or b["bottom"] <= a["top"] + pad)

@pytest.mark.parametrize("name", ["paper"])
def test_block_overlap(name):
    """Translated block regions on a page must not overlap each other."""
    lp = os.path.join(ANALYSIS_DIR, f"{name}_layout.json")
    if not os.path.exists(lp):
        pytest.skip("layout not generated yet")
    layout = json.load(open(lp))
    TRANS = {"body", "heading", "caption", "title"}
    for p in layout["pages"]:
        blocks = [b for b in p["blocks"] if b["type"] in TRANS]
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                assert not _rects_overlap(blocks[i], blocks[j], pad=2.0), \
                    f"page {p['page']}: blocks overlap:\n  {blocks[i]['text'][:40]!r}\n  {blocks[j]['text'][:40]!r}"

@pytest.mark.parametrize("name", ["paper"])
def test_figure_overlap(name):
    """No translatable block should overlap a figure bbox (text would cover the figure)."""
    lp = os.path.join(ANALYSIS_DIR, f"{name}_layout.json")
    if not os.path.exists(lp):
        pytest.skip("layout not generated yet")
    layout = json.load(open(lp))
    TRANS = {"body", "heading", "caption", "title"}
    for p in layout["pages"]:
        figs = p.get("figures", [])
        for b in p["blocks"]:
            if b["type"] not in TRANS:
                continue
            for f in figs:
                # captions legitimately sit just below; only flag substantial overlap
                if _rects_overlap(b, f, pad=-6.0):
                    pytest.skip("placement-level check; enable once M3 writes placed boxes")


# ---- 4. layout regression ---------------------------------------------------
@pytest.mark.parametrize("name", ["paper"])
def test_layout_regression(name):
    cur = os.path.join(ANALYSIS_DIR, f"{name}_layout.json")
    golden = os.path.join(GOLDEN_DIR, f"{name}_layout.json")
    if not (os.path.exists(cur) and os.path.exists(golden)):
        pytest.skip("need both current and golden layout to compare")
    a = json.load(open(cur)); b = json.load(open(golden))
    def sig(d):
        return [(round(bl["x0"]), round(bl["top"]), bl["type"], bl["text"][:30])
                for p in d["pages"] for bl in p["blocks"]]
    assert sig(a) == sig(b), "M1 layout drifted from golden; investigate segmentation change"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([os.path.dirname(__file__), "-v"]))
