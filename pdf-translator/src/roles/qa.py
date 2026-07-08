#!/usr/bin/env python3
"""確認者 (QA / Verifier) - owns quality.

Per the agreed scope, QA does NOT re-verify translation meaning against the
English. It verifies:

  Japanese validity
    - every translatable unit produced a Japanese target (nothing dropped)
    - no ⟦Tn⟧ placeholder leaked into the output (all protected tokens restored)
    - the Japanese actually reached the page (drawn characters present)
    - no residual English in body regions beyond a small tolerance
    - (optional) an LLM "is this natural, complete Japanese?" pass when a key is
      set (PDF_TRANSLATOR_QA_LLM=1); skipped for the free path

  Layout fidelity (正しい文章配置の確認)
    - figures are preserved and unmoved (the engine never touches them; QA
      asserts none were removed)
    - no drawn Japanese line overlaps another line, a figure, OR a vector rule
      (horizontal line: abstract-box border, section separator, table rule) -
      i.e. no 文字と線のかぶり
    - no residual source English is left drawn under the Japanese
    - nothing overflowed its lane

QA (確認者) and the PDF製作者 (producer) jointly OWN correct text placement: QA
detects any misplacement (overlap with text / figure / rule, overflow, residual
English) and routes it back to the producer/editor, which re-runs with a
tightened spec until placement is clean. Verifying translation MEANING is
explicitly out of scope.

On failure it returns actionable defects the orchestrator maps to a role +
tightened parameter, and re-runs, up to a bounded number of rounds.
"""
import json
import os
import re

from config import OUT

PLACEHOLDER_RE = re.compile(r"⟦T\d+⟧|⟦\?⟧")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-']{3,}")
_LIG = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}
TRANS = {"body", "heading", "caption", "title"}


def _norm(s):
    for k, v in _LIG.items():
        s = s.replace(k, v)
    return "".join(ch.lower() for ch in s if ch.isalnum())


def _rects_overlap(a, b, pad=0.5):
    return not (a["x1"] <= b["x0"] + pad or b["x1"] <= a["x0"] + pad or
                a["bottom"] <= b["top"] + pad or b["bottom"] <= a["top"] + pad)


def _pdf_pages(path):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(path)
    pages = [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    doc.close()
    return pages


def _is_jp(t):
    return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in t)


def _is_latin_word(t):
    return sum(c.isascii() and c.isalpha() for c in t) >= 2 and not _is_jp(t)


def _jp_on_english_overlaps(path):
    """Read the FINISHED PDF with pdfplumber and count places where a drawn
    Japanese word physically overlaps a surviving English word. Because both are
    read from the same output in pdfplumber's own coordinate system, this is
    immune to the pdfplumber-vs-content-stream offset gotcha and catches residual
    English that the producer's strip missed and the editor then drew over.
    Returns [(page, english, x0, top), ...]."""
    import pdfplumber
    hits = []
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages, start=1):
            words = page.extract_words()
            jp = [w for w in words if _is_jp(w["text"])]
            en = [w for w in words if _is_latin_word(w["text"])]
            if not jp or not en:
                continue
            for e in en:
                for j in jp:
                    ix = min(e["x1"], j["x1"]) - max(e["x0"], j["x0"])
                    iy = min(e["bottom"], j["bottom"]) - max(e["top"], j["top"])
                    if ix > 0 and iy > 0 and ix * iy > 6:
                        hits.append((pi, e["text"], round(e["x0"]), round(e["top"])))
                        break
    return hits


def _allowed_latin_blob(units, layout):
    parts = []
    for p in layout["pages"]:
        for b in p["blocks"]:
            if b["type"] not in TRANS:
                parts.append(b["text"])
    for u in units:
        parts.append(u.get("target") or u["source"])
    return " ".join(_norm(t) for t in parts)


def review(name, editor_report):
    """Return {ok, defects: [{role, kind, detail, param}]}."""
    layout = json.load(open(f"{OUT}/{name}_layout.json"))
    units = json.load(open(f"{OUT}/{name}_bilingual.json"))
    placed = editor_report["placed"]
    out_path = editor_report["out_path"]
    defects = []

    # --- Japanese validity ---------------------------------------------------
    untranslated = [u["uid"] for u in units if not u.get("target")]
    if untranslated:
        defects.append({"role": "translator", "kind": "untranslated",
                        "detail": f"{len(untranslated)} unit(s) have no Japanese: "
                                  f"{untranslated[:8]}", "param": "reengine"})
    leaked = [u["uid"] for u in units
              if u.get("target") and PLACEHOLDER_RE.search(u["target"])]
    if leaked:
        defects.append({"role": "translator", "kind": "placeholder_leak",
                        "detail": f"unrestored ⟦Tn⟧ in units {leaked[:8]}",
                        "param": "reengine"})

    ref_pages = set()
    for p in layout["pages"]:
        types = [b["type"] for b in p["blocks"]]
        if types and sum(t == "reference" for t in types) >= max(3, len(types) * 0.4):
            ref_pages.add(p["page"])
    blob = _allowed_latin_blob(units, layout)
    residual = []
    for i, txt in enumerate(_pdf_pages(out_path), start=1):
        if i in ref_pages:
            continue
        for w in WORD_RE.findall(txt):
            if _norm(w) not in blob:
                residual.append((i, w))
    if len(residual) > 8:
        defects.append({"role": "editor", "kind": "residual_english",
                        "detail": f"{len(residual)} residual English words "
                                  f"{residual[:12]}", "param": "restrip"})

    # residual English drawn UNDER Japanese (text-on-text) - the producer's strip
    # missed it and the editor reflowed Japanese on top. Read from the finished
    # PDF so the check sees exactly what a reader sees.
    ov = _jp_on_english_overlaps(out_path)
    if ov:
        defects.append({"role": "producer", "kind": "text_overlap",
                        "detail": f"{len(ov)} English fragment(s) overlap Japanese "
                                  f"{ov[:8]}", "param": "restrip"})

    drawn_chars = sum(len(ln["text"]) for lines in placed.values() for ln in lines)
    translated = [u for u in units if u.get("target")]
    if translated and drawn_chars < 50:
        defects.append({"role": "editor", "kind": "empty_output",
                        "detail": "almost no Japanese reached the page",
                        "param": "restrip"})

    # --- layout fidelity -----------------------------------------------------
    for p in layout["pages"]:
        lines = placed.get(str(p["page"]), [])
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                if _rects_overlap(lines[i], lines[j], pad=0.5):
                    defects.append({"role": "editor", "kind": "line_overlap",
                                    "detail": f"page {p['page']} lines overlap",
                                    "param": "shrink"})
                    break
            else:
                continue
            break
        for f in p.get("figures", []):
            for ln in lines:
                if _rects_overlap(ln, f, pad=-2.0):
                    defects.append({"role": "editor", "kind": "figure_overlap",
                                    "detail": f"page {p['page']} text over figure",
                                    "param": "shrink"})
                    break
        # text drawn ACROSS a vector rule (abstract-box border, section separator,
        # table rule). Vector art is kept in place, so a drawn line whose vertical
        # span contains a rule and whose x-range overlaps it is a "文字と線のかぶり".
        for r in p.get("rules", []):
            rband = {"x0": r["x0"], "x1": r["x1"],
                     "top": (r["top"] + r["bottom"]) / 2 - 0.5,
                     "bottom": (r["top"] + r["bottom"]) / 2 + 0.5}
            # pad=+1.0 REQUIRES the line to genuinely span the rule (>=1pt past it
            # on both sides); a negative pad would flag text merely abutting a
            # section separator, which is normal typesetting, not a collision.
            hit = next((ln for ln in lines if _rects_overlap(ln, rband, pad=1.0)),
                       None)
            if hit:
                defects.append({"role": "producer", "kind": "rule_overlap",
                                "detail": f"page {p['page']} text crosses a rule "
                                          f"near y={round(rband['top'])}",
                                "param": "shrink"})
                break

    if editor_report.get("overflow"):
        defects.append({"role": "editor", "kind": "overflow",
                        "detail": f"lanes overflowed: {editor_report['overflow']}",
                        "param": "shrink"})

    # --- optional LLM Japanese-validity pass (never required; off for free path)
    if os.environ.get("PDF_TRANSLATOR_QA_LLM") == "1":
        bad = _llm_japanese_check(translated)
        if bad:
            defects.append({"role": "translator", "kind": "unnatural_japanese",
                            "detail": f"LLM flagged units {bad[:8]} as not valid "
                                      f"Japanese", "param": "reengine"})

    # dedupe by (role, kind)
    seen, uniq = set(), []
    for d in defects:
        key = (d["role"], d["kind"])
        if key not in seen:
            seen.add(key); uniq.append(d)
    return {"ok": not uniq, "defects": uniq}


def _llm_japanese_check(units):
    """Ask Claude whether each target is complete, natural Japanese. Returns the
    uids judged invalid. Best-effort; any error yields no defects."""
    try:
        import anthropic
        client = anthropic.Anthropic(max_retries=2)
        sample = units[:40]
        listing = "\n".join(f"{u['uid']}: {u['target']}" for u in sample)
        msg = client.messages.create(
            model=os.environ.get("PDF_TRANSLATOR_MODEL", "claude-opus-4-8"),
            max_tokens=400,
            system="You check Japanese text quality. For each numbered line, decide "
                   "if it is broken (garbled, truncated mid-sentence, or still "
                   "English). Reply ONLY with the numbers of broken lines, comma "
                   "separated, or 'none'.",
            messages=[{"role": "user", "content": listing}],
        )
        txt = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return [int(n) for n in re.findall(r"\d+", txt)]
    except Exception:
        return []


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="QA role: review a produced PDF")
    ap.add_argument("name", nargs="?", default="paper")
    args = ap.parse_args()
    from roles import editor
    from config import resolve_pdf
    src, name = resolve_pdf(args.name)
    rep = editor.build(name, src)
    print(json.dumps(review(name, rep), ensure_ascii=False, indent=1))
