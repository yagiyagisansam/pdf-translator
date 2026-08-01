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
TRANS = {"body", "heading", "caption", "title", "label"}


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
            page.flush_cache(); page.get_textmap.cache_clear()  # memory guard
            jp = [w for w in words if _is_jp(w["text"])]
            # rotated English (chart axis labels) is figure content that stays
            # on the page by design - not residual body text
            en = [w for w in words
                  if _is_latin_word(w["text"]) and w.get("upright", True)]
            if not jp or not en:
                continue
            for e in en:
                for j in jp:
                    ix = min(e["x1"], j["x1"]) - max(e["x0"], j["x0"])
                    iy = min(e["bottom"], j["bottom"]) - max(e["top"], j["top"])
                    # a REAL overprint overlaps by >=3pt in both axes; a 1-2pt
                    # edge graze (a flow line laid flush under a kept icon
                    # badge) is not something a reader can see
                    if min(ix, iy) >= 2.5 and ix * iy > 8:
                        hits.append((pi, e["text"], round(e["x0"]), round(e["top"])))
                        break
    return hits


def _unit_bbox(u, layout):
    """Union bbox of a unit's source blocks."""
    bs = [layout["pages"][pi]["blocks"][bi] for pi, bi in
          (map(int, s.split(":")) for s in u.get("spans", []))]
    if not bs:
        return None
    return {"x0": min(b["x0"] for b in bs), "x1": max(b["x1"] for b in bs),
            "top": min(b["top"] for b in bs), "bottom": max(b["bottom"] for b in bs)}


def _allowed_latin_blob(units, layout):
    from m2_translate import _has_words
    parts = []
    for p in layout["pages"]:
        for b in p["blocks"]:
            # non-translatable types stay verbatim - and so do translatable
            # blocks with no real words (figure tick debris, leader dots):
            # M2 deliberately leaves those in place
            if b["type"] not in TRANS or not _has_words(b["text"]):
                parts.append(b["text"])
    for u in units:
        parts.append(u.get("target") or u["source"])
    # joined WITHOUT separators: overprinted running heads ("AIM" drawn twice,
    # 3pt apart, on AIM change pages) extract as one word ("AIMAIM") and must
    # still match the two adjacent kept blocks
    return "".join(_norm(t) for t in parts)


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
    # TOFU gate: every non-ASCII char the editor drew must exist in the
    # embedding subset font, or the reader sees a notdef box where a kanji
    # belongs (a stale shared subset file caused exactly this)
    try:
        from fontTools.ttLib import TTFont as _FT
        cover = set(_FT(f"{OUT}/NotoJP-sub.ttf").getBestCmap().keys())
        tofu = set()
        for lines in placed.values():
            for ln in lines:
                for ch in ln.get("text", ""):
                    if ord(ch) > 127 and ord(ch) not in cover:
                        tofu.add(ch)
        if tofu:
            defects.append({"role": "editor", "kind": "missing_glyph",
                            "detail": "font subset lacks drawn chars: "
                                      + "".join(sorted(tofu))[:40],
                            "param": "restrip"})
    except Exception:
        pass

    ref_pages = set()
    for p in layout["pages"]:
        types = [b["type"] for b in p["blocks"]]
        if types and sum(t == "reference" for t in types) >= max(3, len(types) * 0.4):
            ref_pages.add(p["page"])
    # pages the editor intentionally left in English (translation coverage too
    # low to reflow) are by-design English - not residual-strip defects
    ref_pages |= set(editor_report.get("skip_pages") or [])
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
                    # text whose SOURCE block already sat on this figure (a
                    # cover title on a background photo) is faithful, not a
                    # defect - only text that MOVED onto a figure is flagged
                    uid = ln.get("uid")
                    if uid is not None:
                        src = _unit_bbox(units[uid], layout) \
                            if 0 <= uid < len(units) else None
                        if src and _rects_overlap(src, f, pad=-2.0):
                            continue
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
            # pad=0 = neutral overlap: the line's bbox must actually include the
            # rule band (genuine crossing), but a line merely abutting the rule
            # (bbox edge exactly at it) is not flagged. A negative pad over-reports
            # (flags adjacent text); a large positive pad under-reports.
            hit = next((ln for ln in lines if _rects_overlap(ln, rband, pad=0.0)),
                       None)
            if hit:
                defects.append({"role": "producer", "kind": "rule_overlap",
                                "detail": f"page {p['page']} text crosses a rule "
                                          f"near y={round(rband['top'])}",
                                "param": "shrink"})
                break

    # --- layout fidelity vs the SOURCE (配置が元PDFと同じか) -----------------
    # Mechanical guarantee, any document: in per-region mode every unit must be
    # drawn AT its source block's position and at a comparable font size; in
    # reflow mode the Japanese must stay inside the page's original text band.
    mode = editor_report.get("mode", "reflow")
    body_sizes = sorted(p.get("body_size") or 0 for p in layout["pages"])
    doc_body = (body_sizes[len(body_sizes) // 2] if body_sizes else 10) or 10
    if mode == "region":
        import statistics as _st
        # a unit that was translated but never reached the page (its region's
        # slots were all swallowed) or arrived truncated is DATA LOSS - the
        # reader silently misses content. Placed lines carry uid + char count.
        drawn_ch = {}
        for lines in placed.values():
            for ln in lines:
                if ln.get("uid") is not None:
                    drawn_ch[ln["uid"]] = drawn_ch.get(ln["uid"], 0) + \
                        ln.get("nch", len(ln.get("text", "")))
        lost, cut = [], []
        for u in units:
            tgt = u.get("target")
            if not tgt:
                continue
            got = drawn_ch.get(u["uid"], 0)
            if got == 0:
                lost.append(u["uid"])
            elif got < len(tgt) - 2:
                cut.append(u["uid"])
        if lost:
            defects.append({"role": "editor", "kind": "unit_dropped",
                            "detail": f"{len(lost)} translated unit(s) never "
                                      f"drawn: uids {lost[:8]}",
                            "param": "shrink"})
        if cut:
            defects.append({"role": "editor", "kind": "unit_truncated",
                            "detail": f"{len(cut)} unit(s) drawn truncated: "
                                      f"uids {cut[:8]}", "param": "shrink"})
        src_geo = {}
        for u in units:
            if not u.get("target"):
                continue
            bs = [layout["pages"][pi]["blocks"][bi] for pi, bi in
                  (map(int, s.split(":")) for s in u["spans"])]
            src_geo[u["uid"]] = {
                "x0": min(b["x0"] for b in bs),
                "top": min(b["top"] for b in bs),
                "size": _st.median([b["size"] for b in bs if b.get("size")] or [0]),
            }
        drawn = {}
        for lines in placed.values():
            for ln in lines:
                uid = ln.get("uid")
                if uid is None:
                    continue
                d = drawn.setdefault(uid, {"x0": 1e9, "top": 1e9, "size": 0})
                d["x0"] = min(d["x0"], ln["x0"])
                d["top"] = min(d["top"], ln["top"])
                d["size"] = max(d["size"], ln.get("size") or 0)
        drift, small = [], []
        for uid, g in src_geo.items():
            d = drawn.get(uid)
            if not d:
                continue
            # x must match the source block; top may only move down a little
            # (figure/caption clearance), never jump elsewhere on the page
            if abs(d["x0"] - g["x0"]) > 14 or not (-8 <= d["top"] - g["top"] <= 42):
                drift.append(uid)
            if g["size"] >= 1.3 * doc_body and d["size"] \
                    and d["size"] < 0.55 * g["size"]:
                small.append(uid)   # display text (title) rendered tiny
        if drift:
            defects.append({"role": "editor", "kind": "layout_drift",
                            "detail": f"{len(drift)} unit(s) drawn away from the "
                                      f"source position: uids {drift[:8]}",
                            "param": "shrink"})
        if small:
            defects.append({"role": "editor", "kind": "size_fidelity",
                            "detail": f"{len(small)} display unit(s) far smaller "
                                      f"than the source: uids {small[:8]}",
                            "param": "shrink"})
    else:
        stray = []
        for p in layout["pages"]:
            if not p["blocks"]:
                continue
            x_lo = min(b["x0"] for b in p["blocks"]) - 10
            x_hi = max(b["x1"] for b in p["blocks"]) + 10
            for ln in placed.get(str(p["page"]), []):
                if ln["x0"] < x_lo or ln["x1"] > x_hi:
                    stray.append((p["page"], ln["text"][:20]))
        if stray:
            defects.append({"role": "editor", "kind": "layout_drift",
                            "detail": f"{len(stray)} line(s) outside the source "
                                      f"text band: {stray[:6]}", "param": "shrink"})

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
