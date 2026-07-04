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

def _pdf_text_pages(path):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    pages = [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    doc.close()
    return pages


WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']{3,}")

TRANS = {"body", "heading", "caption", "title"}


def _norm(s):
    return "".join(ch.lower() for ch in s if ch.isalnum())


def _allowed_latin_blob(name):
    """Normalized concatenation of text that legitimately remains Latin in the
    Japanese PDF, derived from the pipeline's own data (general-purpose - no
    per-sample word list):
      - blocks that are BY DESIGN untranslated (data/running_head/pagenum/
        reference/figure text)
      - the produced Japanese targets (kept proper nouns, restored tokens)
      - sources of units the engine could not translate (English remains as the
        designed fallback)
    A PDF word counts as allowed when its normalized form is a substring of the
    blob, which also covers words the PDF splits across line breaks."""
    layout = json.load(open(os.path.join(ANALYSIS_DIR, f"{name}_layout.json")))
    parts = []
    for p in layout["pages"]:
        for b in p["blocks"]:
            if b["type"] not in TRANS:
                parts.append(b["text"])
    bp = os.path.join(ANALYSIS_DIR, f"{name}_bilingual.json")
    if os.path.exists(bp):
        for u in json.load(open(bp)):
            parts.append(u.get("target") or u["source"])
    return " ".join(_norm(t) for t in parts)


# ---- 1. residual English ----------------------------------------------------
@pytest.mark.parametrize("name", ["paper"])
def test_residual_english(name):
    pdf = os.path.join(ANALYSIS_DIR, f"{name}_ja.pdf")
    if not os.path.exists(pdf):
        pytest.skip(f"{pdf} not generated yet")
    layout = json.load(open(os.path.join(ANALYSIS_DIR, f"{name}_layout.json")))
    # reference pages are intentionally English - exclude them
    ref_pages = set()
    for p in layout["pages"]:
        types = [b["type"] for b in p["blocks"]]
        if types and sum(t == "reference" for t in types) >= max(3, len(types) * 0.4):
            ref_pages.add(p["page"])
    blob = _allowed_latin_blob(name)
    pages = _pdf_text_pages(pdf)
    leftovers = []
    for i, txt in enumerate(pages, start=1):
        if i in ref_pages:
            continue
        for w in WORD_RE.findall(txt):
            if _norm(w) not in blob:
                leftovers.append((i, w))
    # Known residue on the paper sample comes from one M1 artifact (the Table 1
    # caption interleaved with the table title - see docs/IMPROVEMENT_PLAN.md);
    # the threshold guards against gross removal regressions.
    assert len(leftovers) <= 8, f"too many residual English words: {leftovers[:20]}"


# ---- 2 & 3. overlap detection -----------------------------------------------
def _rects_overlap(a, b, pad=1.0):
    return not (a["x1"] <= b["x0"] + pad or b["x1"] <= a["x0"] + pad or
                a["bottom"] <= b["top"] + pad or b["bottom"] <= a["top"] + pad)

def _overlap_pairs(layout):
    pairs = set()
    for p in layout["pages"]:
        blocks = [b for b in p["blocks"] if b["type"] in TRANS]
        for i in range(len(blocks)):
            for j in range(i + 1, len(blocks)):
                if _rects_overlap(blocks[i], blocks[j], pad=2.0):
                    pairs.add((p["page"], blocks[i]["text"][:40], blocks[j]["text"][:40]))
    return pairs


@pytest.mark.parametrize("name", ["paper"])
def test_block_overlap(name):
    """Regression guard on M1 segmentation: no NEW overlapping source-block pairs
    beyond the ones recorded in the golden layout (known artifacts: table-row
    fragments, superscript-citation rows - tracked in docs/IMPROVEMENT_PLAN.md)."""
    lp = os.path.join(ANALYSIS_DIR, f"{name}_layout.json")
    if not os.path.exists(lp):
        pytest.skip("layout not generated yet")
    layout = json.load(open(lp))
    golden_path = os.path.join(GOLDEN_DIR, f"{name}_layout.json")
    known = _overlap_pairs(json.load(open(golden_path))) if os.path.exists(golden_path) else set()
    new = _overlap_pairs(layout) - known
    assert not new, f"NEW overlapping block pairs (segmentation regression): {sorted(new)[:5]}"

@pytest.mark.parametrize("name", ["paper"])
def test_figure_overlap(name):
    """No DRAWN Japanese line may overlap a figure bbox. Uses the real placement
    boxes M3 writes to <name>_placed.json, not the pre-layout block bboxes."""
    pp = os.path.join(ANALYSIS_DIR, f"{name}_placed.json")
    lp = os.path.join(ANALYSIS_DIR, f"{name}_layout.json")
    if not (os.path.exists(pp) and os.path.exists(lp)):
        pytest.skip("run the pipeline first (m3 writes *_placed.json)")
    placed = json.load(open(pp))
    layout = json.load(open(lp))
    bad = []
    for p in layout["pages"]:
        lines = placed.get(str(p["page"]), [])
        for f in p.get("figures", []):
            for ln in lines:
                if _rects_overlap(ln, f, pad=-2.0):
                    bad.append((p["page"], ln, {k: round(f[k]) for k in ("x0", "x1", "top", "bottom")}))
    assert not bad, f"Japanese text drawn over figures: {bad[:5]}"


@pytest.mark.parametrize("name", ["paper"])
def test_placed_line_overlap(name):
    """No two drawn Japanese lines on a page may overlap each other."""
    pp = os.path.join(ANALYSIS_DIR, f"{name}_placed.json")
    if not os.path.exists(pp):
        pytest.skip("run the pipeline first (m3 writes *_placed.json)")
    placed = json.load(open(pp))
    for page, lines in placed.items():
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                assert not _rects_overlap(lines[i], lines[j], pad=0.5), \
                    f"page {page}: drawn lines overlap: {lines[i]} vs {lines[j]}"


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
