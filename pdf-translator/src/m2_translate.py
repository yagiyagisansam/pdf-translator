#!/usr/bin/env python3
# Milestone 2: Translation pipeline
# - Builds translation UNITS from M1 layout blocks
# - Merges cross-page-joined blocks into a single unit (requirement 2)
# - Protects non-translatable tokens: inline math, citation markers, numeric+unit, URLs/emails/DOI
# - Supports a glossary for consistent terminology
# - Emits units.json (to be translated) and, after translation, a bilingual map

import os, re, json

from config import OUT, ensure_out

# ---- token protection -------------------------------------------------------
# Patterns whose matched text must survive translation verbatim.
PROTECT_PATTERNS = [
    ("URL",   re.compile(r"https?://\S+")),
    ("DOI",   re.compile(r"\bdoi:\S+", re.I)),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # citation markers like "4–6", "1,2", "7,17–19" possibly superscripted in source
    ("CITE",  re.compile(r"(?<=[a-zA-Z\)\.])\d{1,3}(?:[–\-,]\d{1,3})*(?=[\s\.,;:\)]|$)")),
    # numeric values with optional ± and units (e.g. 2.7 ± 0.7 cm, 250 Hz, 10 kg, p < 0.01)
    ("NUM",   re.compile(r"[<>≈=]?\s?\d[\d.,]*\s?(?:±\s?\d[\d.,]*)?\s?"
                         r"(?:%|cm|mm|m|kg|g|s|ms|Hz|N|m/s2?|°|yrs?|kg/m2|weeks?)?")),
    ("ABBR",  re.compile(r"\b(?:CMVJ|SPJ|AFTE|SPAD|NAMI|NASA|ICC|ES|QA|AVT|3D)\b")),
]

def protect(text):
    """Replace protected spans with placeholders ⟦Tn⟧; return (masked_text, mapping)."""
    spans = []
    for label, rx in PROTECT_PATTERNS:
        for m in rx.finditer(text):
            s = m.group().strip()
            if s:
                spans.append((m.start(), m.end(), s))
    # resolve overlaps: keep earliest, longest
    spans.sort(key=lambda x: (x[0], -(x[1]-x[0])))
    chosen = []
    last = -1
    for s, e, val in spans:
        if s >= last:
            chosen.append((s, e, val)); last = e
    mapping = {}
    out = []
    cur = 0
    for i, (s, e, val) in enumerate(chosen):
        out.append(text[cur:s])
        key = f"⟦T{i}⟧"
        mapping[key] = val
        out.append(key)
        cur = e
    out.append(text[cur:])
    return "".join(out), mapping

def restore(text, mapping):
    for key, val in mapping.items():
        text = text.replace(key, val)
    return text

# ---- build translation units ------------------------------------------------
TRANSLATABLE = {"body", "heading", "caption", "title"}

SENT_END = tuple(".!?。:;")

FOOTER_RE = re.compile(r"(doi:|©|\u00a9|rights reserved|front matter|\d{4}-\d{3,4}|"
                       r"Corresponding author|E-mail address)", re.I)

def _is_footer(text):
    return bool(FOOTER_RE.search(text or ""))

def _continues(prev_text, next_text, next_type):
    """True if next_text is a continuation of the same sentence/paragraph as prev_text."""
    t = prev_text.rstrip()
    h = next_text.lstrip()
    if not t or not h:
        return False
    if next_type in ("heading", "title", "caption"):
        return False
    # footers/affiliations/DOI lines never flow into or out of body text
    if _is_footer(t) or _is_footer(h):
        return False
    # hyphenated word split across blocks
    if t.endswith("-"):
        return True
    # prev ends mid-sentence (no terminal punctuation) and next looks like a continuation
    if not t.endswith(SENT_END):
        if h[:1].islower() or h[:1] in "(\u2018\u2019\"'" or h[:1].isdigit():
            return True
        return True
    return False

def build_units(layout):
    """Build translation units by walking the GLOBAL reading order and merging
    consecutive flowing blocks - across columns AND across pages - into one unit,
    so a sentence split by column/page breaks is translated as a whole."""
    pages = layout["pages"]
    # estimate document body font size (mode of body block sizes)
    from collections import Counter
    szc = Counter()
    for p in pages:
        for b in p["blocks"]:
            if b["type"] == "body" and b.get("size"):
                szc[round(b["size"])] += 1
    body_size = szc.most_common(1)[0][0] if szc else 10
    BIG = body_size * 1.3   # title-like font threshold

    def is_big(b):
        return b.get("size", 0) >= BIG

    def block_id(pi, bi):
        return f"{pi}:{bi}"

    # 1) flatten translatable blocks into global reading order (page, then order)
    seq = []
    for pi, page in enumerate(pages):
        ordered = sorted(
            [(bi, b) for bi, b in enumerate(page["blocks"]) if b["type"] in TRANSLATABLE],
            key=lambda x: x[1].get("order", 1e9))
        for bi, b in ordered:
            seq.append((pi, bi, b))

    # 2) merge consecutive flowing blocks
    units = []
    i = 0
    while i < len(seq):
        pi, bi, b = seq[i]
        parts = [(pi, bi, b)]
        # headings/captions/titles AND large-font (title-like) blocks are standalone
        if b["type"] in ("heading", "caption", "title") or is_big(b):
            j = i + 1
        else:
            j = i + 1
            while j < len(seq):
                ppi, pbi, pb = parts[-1]
                npi, nbi, nb = seq[j]
                # heading/caption/title or big-font block is a hard boundary
                if nb["type"] in ("heading", "caption", "title") or is_big(nb):
                    break
                # Never merge across a page break on slide decks: each slide is
                # independent, and stitching its bullets into the previous slide's
                # unit would place them all on that page and leave this one blank.
                cross_page = npi != ppi
                landscape = (pages[npi]["width"] > pages[npi]["height"] or
                             pages[ppi]["width"] > pages[ppi]["height"])
                if cross_page and landscape:
                    break
                flag_link = pb.get("continues_to_next_page") and nb.get("continues_from_prev_page")
                if flag_link or _continues(pb["text"], nb["text"], nb["type"]):
                    parts.append((npi, nbi, nb))
                    j += 1
                else:
                    break
        # join text
        text = ""
        spans_pages = set()
        for k, (qpi, qbi, qb) in enumerate(parts):
            seg = qb["text"].strip()
            spans_pages.add(qpi + 1)
            if k == 0:
                text = seg
            elif text.endswith("-"):
                text = text[:-1] + seg
            else:
                text = text + " " + seg
        units.append({
            "uid": len(units),
            "type": parts[0][2]["type"],
            "spans": [block_id(p, q) for (p, q, _) in parts],
            "page": pi + 1,
            "pages": sorted(spans_pages),
            "cross_page": len(spans_pages) > 1,
            "multi_block": len(parts) > 1,
            "source": text,
        })
        i = j

    # 3) cross-page fix-up. M1's continuation flags bind a page-tail block to its
    #    continuation on the next page, but a hard-boundary unit can sit between
    #    them in reading order (e.g. a table caption at the bottom of the column)
    #    and break the linear merge above. Merge those flagged pairs here.
    def blk(sid):
        p, b = map(int, sid.split(":"))
        return pages[p]["blocks"][b]

    cont_by_page = {}
    for u in units:
        fb = blk(u["spans"][0])
        if fb.get("continues_from_prev_page"):
            cont_by_page.setdefault(int(u["spans"][0].split(":")[0]) + 1, u)
    consumed = set()
    for u in units:
        if u["uid"] in consumed:
            continue
        while True:
            last = blk(u["spans"][-1])
            if not last.get("continues_to_next_page"):
                break
            nxt = cont_by_page.get(int(u["spans"][-1].split(":")[0]) + 2)  # 1-based next page
            if nxt is None or nxt is u or nxt["uid"] in consumed:
                break
            seg = nxt["source"].lstrip()
            u["source"] = (u["source"][:-1] + seg) if u["source"].endswith("-") \
                else (u["source"] + " " + seg)
            u["spans"] += nxt["spans"]
            u["pages"] = sorted(set(u["pages"]) | set(nxt["pages"]))
            u["cross_page"] = True
            u["multi_block"] = True
            consumed.add(nxt["uid"])
    units = [u for u in units if u["uid"] not in consumed]
    for k, u in enumerate(units):
        u["uid"] = k
    return units

# ---- glossary ---------------------------------------------------------------
DEFAULT_GLOSSARY = {
    "assisted jumping": "アシステッドジャンプ",
    "counter-movement vertical jump": "カウンタームーブメント垂直跳び",
    "spike jump": "スパイクジャンプ",
    "vertical jump": "垂直跳び",
    "effect size": "効果量",
    "wash-out": "ウォッシュアウト",
    "motion sickness": "動揺病",
    "airsickness": "航空病",
    "telemedicine": "遠隔医療",
    "desensitization": "脱感作",
    "biofeedback": "バイオフィードバック",
}

def load_glossary():
    """DEFAULT_GLOSSARY merged with an optional user glossary at <data>/glossary.json
    ({'english term': '日本語'}) so terminology can be tuned without code changes."""
    from config import DATA_DIR
    glossary = dict(DEFAULT_GLOSSARY)
    user_path = os.path.join(DATA_DIR, "glossary.json")
    if os.path.exists(user_path):
        glossary.update(json.load(open(user_path)))
    return glossary


def apply_glossary_hint(units, glossary):
    """Attach per-unit glossary hints (terms found in the unit) for the translator."""
    for u in units:
        low = u["source"].lower()
        hints = {term: jp for term, jp in glossary.items() if term in low}
        if hints:
            u["glossary"] = hints
    return units

if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="M2: layout.json -> <name>_units.json")
    ap.add_argument("names", nargs="*", default=["paper", "deck"],
                    help="document names whose <name>_layout.json exists (default: paper deck)")
    args = ap.parse_args()
    ensure_out()
    for name in args.names:
        layout_path = f"{OUT}/{name}_layout.json"
        if not os.path.exists(layout_path):
            print(f"[{name}] skipped: {layout_path} not found (run m1_analyze first)",
                  file=sys.stderr)
            continue
        layout = json.load(open(layout_path))
        units = build_units(layout)
        units = apply_glossary_hint(units, load_glossary())
        # add masked text + token map per unit
        for u in units:
            masked, mapping = protect(u["source"])
            u["masked"] = masked
            u["tokens"] = mapping
        with open(f"{OUT}/{name}_units.json", "w") as f:
            json.dump(units, f, ensure_ascii=False, indent=1)
        nx = sum(1 for u in units if u["cross_page"])
        print(f"[{name}] units={len(units)} cross_page_units={nx}")
        # show the cross-page merged ones
        for u in units:
            if u["cross_page"]:
                print(f"   UID{u['uid']} pages={u['spans'][0].split(':')[0]}->.. : "
                      f"...{u['source'][-90:]!r}")
