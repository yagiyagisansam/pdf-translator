#!/usr/bin/env python3
"""Layout-fidelity regression suite on a SYNTHETIC brochure-style PDF.

Locks in the general mechanism (no per-sample tuning): scattered text islands
(sidebar paragraph, diagram callout labels, a spec table's label/value columns,
a display-size cover title) must
  1. segment into separate blocks (M1) - never fused or char-interleaved,
  2. stay separate translation units (M2),
  3. be drawn back at their source position and size (M3 region mode),
  4. pass the QA layout-fidelity gate.

The synthetic file reproduces the failure shapes seen on real brochures
(spread layouts, callouts at the same y as body text, ~15pt island gaps).
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def _make_synth_pdf(path):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4, landscape

    c = canvas.Canvas(path, pagesize=A4)
    # --- portrait cover: small colored label + display title ----------------
    w, h = A4
    c.setFillColorRGB(0.8, 0.1, 0.1)   # brand-red label: color must survive
    c.setFont("Helvetica", 10)
    c.drawString(40, h - 380, "ACME ROTORCRAFT DIVISION")
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 40)
    c.drawString(40, h - 430, "AW101")
    c.showPage()

    # --- landscape spread: sidebar paragraph + callout islands + spec table -
    c.setPageSize(landscape(A4))
    lw, lh = landscape(A4)
    c.setFont("Helvetica", 9)
    para = ["The helicopter is the most advanced and capable machine",
            "available today, building on operational experience gained",
            "in the harshest maritime and littoral environments around",
            "the world for both civil and military operators."]
    for i, ln in enumerate(para):
        c.drawString(40, lh - 60 - 11 * i, ln)
    # callout label island 1 (two stacked 7pt lines) at the same y as the para
    c.setFont("Helvetica", 7)
    c.drawString(320, lh - 60, "Composite Rotor Hub")
    c.drawString(320, lh - 69, "with Low Hinge Offset")
    # island 2, same rows, ~15pt to the right of a long line of island 1's col
    c.drawString(320, lh - 95, "Robust transmission system on board")
    c.drawString(475, lh - 95, "Advanced Profile")
    c.drawString(475, lh - 104, "Composite Blades")
    # spec table: label column + two value columns
    y0 = lh - 160
    rows = [("Length Overall", "22.83 m", "74 ft 11 in"),
            ("Overall Height", "6.66 m", "21 ft 10 in"),
            ("Rotor Diameter", "18.60 m", "61 ft 0 in")]
    for i, (lab, v1, v2) in enumerate(rows):
        y = y0 - 10 * i
        c.drawString(320, y, lab)
        c.drawString(430, y, v1)
        c.drawString(490, y, v2)
    c.showPage()
    c.save()


def _run(cmd, env):
    r = subprocess.run(cmd, cwd=SRC, env=env, capture_output=True, text=True,
                       timeout=300)
    assert r.returncode == 0, f"{cmd} failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("synth")
    out = tmp / "out"
    data = tmp / "data"
    out.mkdir(); data.mkdir()
    pdf = str(tmp / "synth.pdf")
    _make_synth_pdf(pdf)
    env = dict(os.environ, PDF_TRANSLATOR_OUT=str(out),
               PDF_TRANSLATOR_DATA=str(data))
    _run([sys.executable, "m1_analyze.py", pdf, "--name", "synth",
          "--no-render"], env)
    _run([sys.executable, "m2_translate.py", "synth"], env)
    # identity mock memo: every unit "translates" to its own source, so the
    # placement geometry can be verified without a network engine
    units = json.load(open(out / "synth_units.json"))
    memo = [{"prefix": u["masked"][:48], "ja": u["source"]} for u in units]
    json.dump(memo, open(data / "mock_memo.json", "w"), ensure_ascii=False)
    stdout = _run([sys.executable, "-m", "roles.orchestrator", pdf,
                   "--name", "synth", "--engine", "mock", "--rounds", "1"], env)
    return {"out": out, "stdout": stdout,
            "layout": json.load(open(out / "synth_layout.json")),
            "units": json.load(open(out / "synth_bilingual.json")),
            "placed": json.load(open(out / "synth_placed.json"))}


def _blocks(synth, page):
    return synth["layout"]["pages"][page]["blocks"]


def test_islands_stay_separate(synth):
    """Callout labels never fuse with the sidebar paragraph or each other."""
    texts = [b["text"] for b in _blocks(synth, 1)]
    hub = next(t for t in texts if "Composite Rotor Hub" in t)
    assert "machine" not in hub and "Advanced" not in hub
    adv = next(t for t in texts if "Advanced Profile" in t)
    assert "transmission" not in adv
    # no char interleaving anywhere (the two stacked label lines stay clean)
    assert any("with Low Hinge Offset" in t for t in texts)


def test_spec_table_values_are_data(synth):
    """Value columns are 'data' (kept verbatim); the label column stays
    translatable and separate from the values."""
    by_text = {b["text"]: b for b in _blocks(synth, 1)}
    val = next(b for t, b in by_text.items() if "22.83 m" in t)
    assert val["type"] == "data"
    lab = next(b for t, b in by_text.items() if "Length Overall" in t)
    assert lab["type"] not in ("data",)
    assert "22.83" not in lab["text"]


def test_cover_title_block(synth):
    """The 40pt cover title is its own title block, not fused with the label."""
    blocks = _blocks(synth, 0)
    title = next(b for b in blocks if b["text"].strip() == "AW101")
    assert title["type"] == "title" and title["size"] >= 30
    # the small label survives as its own block, not fused into the title
    assert any("ACME" in b["text"] and b["size"] < 15 for b in blocks)


def test_units_do_not_chain_labels(synth):
    """No translation unit contains more than one island's text."""
    for u in synth["units"]:
        assert not ("Composite Rotor Hub" in u["source"]
                    and "Advanced Profile" in u["source"])
        assert not ("machine" in u["source"]
                    and "Composite Rotor Hub" in u["source"])


def test_text_color_preserved(synth):
    """The colored label's fill color is captured so the overlay can draw the
    Japanese in the source color (a white-on-photo label drawn black is
    invisible)."""
    blocks = _blocks(synth, 0)
    lab = next(b for b in blocks if "ACME" in b["text"])
    r, g, b = lab.get("color", [0, 0, 0])
    assert r > 0.6 and g < 0.3 and b < 0.3, lab.get("color")


def test_protect_number_word_boundary():
    """The NUM masking pattern must not eat the first letter of a word:
    '71 million' once masked as '71 m' + 'illion' and the translation of the
    mangled remainder was garbage."""
    sys.path.insert(0, SRC)
    from m2_translate import protect
    masked, mapping = protect("A new 71 million Eur (£60 million) investment")
    assert "⟧illion" not in masked, masked   # token must not swallow the 'm'
    assert " million" in masked, masked      # the word survives for translation
    assert all(not v.rstrip().endswith(" m") for v in mapping.values()), mapping


def test_placement_fidelity(synth):
    """Every drawn unit starts at its source block position; the cover title
    keeps a display-size font. The QA gate passes."""
    assert "OK" in synth["stdout"], synth["stdout"]
    units = {u["uid"]: u for u in synth["units"]}
    layout = synth["layout"]
    drawn = {}
    for lines in synth["placed"].values():
        for ln in lines:
            d = drawn.setdefault(ln["uid"], {"x0": 1e9, "top": 1e9, "size": 0})
            d["x0"] = min(d["x0"], ln["x0"])
            d["top"] = min(d["top"], ln["top"])
            d["size"] = max(d["size"], ln.get("size") or 0)
    for uid, d in drawn.items():
        u = units[uid]
        bs = [layout["pages"][pi]["blocks"][bi] for pi, bi in
              (map(int, s.split(":")) for s in u["spans"])]
        sx0 = min(b["x0"] for b in bs)
        stop = min(b["top"] for b in bs)
        assert abs(d["x0"] - sx0) <= 14, (uid, u["source"][:40])
        assert -8 <= d["top"] - stop <= 42, (uid, u["source"][:40])
        if u["source"].strip() == "AW101":
            assert d["size"] >= 0.55 * 40
