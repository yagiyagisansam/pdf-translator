#!/usr/bin/env python3
# Milestone 2: Translation pipeline
# - Builds translation UNITS from M1 layout blocks
# - Merges cross-page-joined blocks into a single unit (requirement 2)
# - Protects non-translatable tokens: inline math, citation markers, numeric+unit, URLs/emails/DOI
# - Supports a glossary for consistent terminology
# - Emits units.json (to be translated) and, after translation, a bilingual map

import os, re, json

OUT = "/home/claude/analysis"

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

def apply_glossary_hint(units, glossary):
    """Attach per-unit glossary hints (terms found in the unit) for the translator."""
    for u in units:
        low = u["source"].lower()
        hints = {term: jp for term, jp in glossary.items() if term in low}
        if hints:
            u["glossary"] = hints
    return units

if __name__ == "__main__":
    for name in ["paper", "deck"]:
        layout = json.load(open(f"{OUT}/{name}_layout.json"))
        units = build_units(layout)
        units = apply_glossary_hint(units, DEFAULT_GLOSSARY)
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
