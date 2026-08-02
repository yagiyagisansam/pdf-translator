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
    # a dot leader with its trailing page/section number (". . . . . 1-1-12"):
    # masked whole so the engine translates only the row's TITLE - otherwise
    # the dots come back as 。。。 and the row number gets scattered
    ("LEADER", re.compile(r"(?:[.·⋅]\s*){4,}[\s\d\-–./]*$")),
    ("URL",   re.compile(r"https?://\S+")),
    ("DOI",   re.compile(r"\bdoi:\S+", re.I)),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # citation markers like "4–6", "1,2", "7,17–19" possibly superscripted in source
    ("CITE",  re.compile(r"(?<=[a-zA-Z\)\.])\d{1,3}(?:[–\-,]\d{1,3})*(?=[\s\.,;:\)]|$)")),
    # numeric values with optional ± and units (e.g. 2.7 ± 0.7 cm, 250 Hz, 10 kg, p < 0.01).
    # (?![A-Za-z]) stops a unit from eating the first letter of a WORD:
    # without it "71 million" masked as "71 m" + "illion" and the translation
    # of the mangled remainder was garbage.
    ("NUM",   re.compile(r"[<>≈=]?\s?\d[\d.,]*\s?(?:±\s?\d[\d.,]*)?\s?"
                         r"(?:%|cm|mm|m|kg|g|s|ms|Hz|N|m/s2?|°|yrs?|kg/m2|weeks?)?"
                         r"(?![A-Za-z0-9])")),
    # a date ("January 17, 2025" / "17 January 2025") - the free engine scatters
    # the masked day/year tokens around the sentence ("1月にアクセス17,2025");
    # protected whole, it is deterministically rendered as 2025年1月17日
    ("DATE",  re.compile(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|"
                         r"May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
                         r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+"
                         r"(\d{1,2}),?\s+(\d{4})\b")),
    ("DATE2", re.compile(r"\b(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|"
                         r"Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|"
                         r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
                         r"Dec(?:ember)?)\.?,?\s+(\d{4})\b")),
    # month + year with no day ("January 2008", "revised December 2015")
    ("DATE3", re.compile(r"\b(January|February|March|April|May|June|July|"
                         r"August|September|October|November|December)"
                         r"\.?,?\s+(\d{4})\b")),
    # bare web/domain names (leonardo.com) - a URL pattern without the scheme.
    # The optional PATH keeps multi-segment links whole (www.faa.gov/x/y/z)
    ("SITE",  re.compile(r"\b[\w-]+(?:\.[\w-]+)*"
                         r"\.(?:com|org|net|io|gov|edu|info|co\.[a-z]{2})"
                         r"(?:/[^\s)\]}>,;\"']*)?\b")),
    # universal technical/aviation abbreviations a literal MT engine mangles
    # ("HIGE" -> beard). Domain constants, not per-document tuning.
    ("ABBR",  re.compile(r"\b(?:CMVJ|SPJ|AFTE|SPAD|NAMI|NASA|ICC|ES|QA|AVT|3D|"
                         r"ISA|VFR|IFR|FAA|EASA|HIGE|HOGE|OEI|MGW|MCP|IGE|OGE|"
                         r"NVG|AFCS|HUMS|SAR|FADEC|SATCOM|MoD|"
                         # finance/IR constants a literal MT engine mangles
                         # ("ROE" -> roe -> 魚卵). Domain constants, not
                         # per-document tuning.
                         r"ROE|ROA|ROI|ROIC|EPS|EBITDA|EBIT|CAPEX|OPEX|FCF|"
                         r"YoY|QoQ|GAAP|IFRS|NTD|TWD|USD|EUR|JPY|CAGR)\b")),
    # quarter/half notation (2Q26, 1H26, 4Q25) - not a number, not a word
    ("QTR",   re.compile(r"\b[1-4][QH]\d{2}(?![0-9])")),
    # product/model designators (AW101, EH101, CT7-8E, UK24 ...): keep verbatim so
    # no engine can transliterate or split them
    ("MODEL", re.compile(r"\b[A-Z]{1,5}\d{1,4}(?:-\d+[A-Z]*|-[A-Z]+\d*)?\b")),
]

_MONTH_NUM = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _date_jp(m, month_group, day_group, year_group):
    """Render a protected English date as a Japanese date (deterministic - no
    engine involved), e.g. 'January 17, 2025' -> '2025年1月17日'."""
    mon = _MONTH_NUM.get(m.group(month_group)[:3].lower())
    if not mon:
        return m.group().strip()
    return f"{m.group(year_group)}年{mon}月{int(m.group(day_group))}日"


def protect(text):
    """Replace protected spans with placeholders ⟦Tn⟧; return (masked_text, mapping)."""
    spans = []
    for label, rx in PROTECT_PATTERNS:
        for m in rx.finditer(text):
            if label == "DATE":
                s = _date_jp(m, 1, 2, 3)
            elif label == "DATE2":
                s = _date_jp(m, 2, 1, 3)
            elif label == "DATE3":
                mon = _MONTH_NUM.get(m.group(1)[:3].lower())
                s = f"{m.group(2)}年{mon}月" if mon else m.group().strip()
            else:
                s = m.group().strip()
            if s:
                # a leading \s? in a pattern (NUM) must not let it START one
                # char earlier than a longer, better span (DATE) at the same
                # word - resolve overlaps on the text, not the stray space
                lead = len(m.group()) - len(m.group().lstrip())
                spans.append((m.start() + lead, m.end(), s))
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
TRANSLATABLE = {"body", "heading", "caption", "title", "label"}


# a real word: Capitalized/lowercase run of 3+ letters, or a 3+ letter acronym.
# Rejects figure debris like ".t...tItI." (weird-case runs) and dot leaders,
# while keeping "NDB", "Box", "Nondirectional" - so a TOC row with a long dot
# leader still translates but tick-label junk never does.
# the extra [a-z]{3,} alternative catches CamelCase glued by tight kerning
# ("LatinAmerica" extracts with no space and has no \b-delimited word)
_WORD_OK = re.compile(r"\b(?:[A-Za-z][a-z]{2,}|[A-Z]{3,})\b|[a-z]{3,}")


def _has_words(text):
    """True when the block contains at least one real word to translate.
    Translating letter-debris (chart tick labels, leader dots) yields junk
    drawn over artwork; such blocks stay verbatim."""
    return bool(_WORD_OK.search(text or ""))

SENT_END = tuple(".!?。:;")

# leading list-item markers: bullet, dash variants, middle dot, checkbox
_BULLET_RE = re.compile(r"^\s*[•‣⁃▪●–—・·∙]\s*"
                        r"|^\s*[-–—]\s+")

FOOTER_RE = re.compile(r"(doi:|©|\u00a9|rights reserved|front matter|\d{4}-\d{3,4}|"
                       r"Corresponding author|E-mail address|"
                       r"Received \d|received in revised|accepted \d)", re.I)

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
    # a paragraph ending with a URL/email/domain is an instruction line
    # ("... go to https://x/y") - terminal, even without punctuation. Without
    # this the next paragraph chains on and gets dragged to the wrong position.
    if re.search(r"(?:https?://\S+|www\.\S+|\S+@\S+\.\w+|"
                 r"\.(?:com|gov|org|net|edu|io|mil)(?:/\S*)?)$", t):
        return False
    # a paragraph ending with a FOOTNOTE/CITATION marker ("...compact area).4",
    # "...ability.4–6") IS sentence-final - without stripping the trailing
    # digits, consecutive paragraphs chain into one giant unit
    if re.sub(r"[\s\d,–\-]+$", "", t).endswith(SENT_END):
        return False
    # prev ends mid-sentence (no terminal punctuation) and next looks like a continuation
    if not t.endswith(SENT_END):
        if h[:1].islower() or h[:1] in "(\u2018\u2019\"'" or h[:1].isdigit():
            return True
        # next starts with a CAPITAL: only flowing prose continues like this
        # (a proper noun mid-sentence). A short block is a standalone label
        # (diagram callout, list heading) - merging labels chains unrelated
        # text into one garbled unit, so require real paragraph length.
        return len(t) >= 80
    return False


def _geom_adjacent(pb, nb):
    """nb sits directly under pb with horizontal overlap - i.e. they are the
    same visual text island. Used on landscape pages (slides/spreads), where
    text is scattered islands and reading-order 'continuation' across islands
    must never merge them."""
    lh = max((nb["bottom"] - nb["top"]) / max(nb.get("nlines", 1), 1), 1.0)
    gap = nb["top"] - pb["bottom"]
    ov = min(pb["x1"], nb["x1"]) - max(pb["x0"], nb["x0"])
    wmin = min(pb["x1"] - pb["x0"], nb["x1"] - nb["x0"]) or 1.0
    return -4 <= gap <= max(2.0 * lh, 16.0) and ov >= 0.45 * wmin

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
    #    (blocks with no letters - stray bullet markers, symbols - stay verbatim
    #    on the page; there is nothing to translate)
    seq = []
    for pi, page in enumerate(pages):
        ordered = sorted(
            [(bi, b) for bi, b in enumerate(page["blocks"])
             if b["type"] in TRANSLATABLE and _has_words(b["text"])],
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
                # On a landscape page (slide/spread) text is scattered islands;
                # merge only blocks that are geometrically one island.
                if landscape and not _geom_adjacent(pb, nb):
                    break
                # a new list item (bullet / dash marker) is a hard boundary, so
                # each bullet stays its own unit and keeps its original position
                # (critical on slides; harmless on prose).
                if _BULLET_RE.match(nb["text"]):
                    break
                # PARALLEL-LIST rows ("banking hours" / "blood pressure" ...)
                # are independent items: the lowercase-continuation heuristic
                # would chain the whole column into one run-on unit
                if pb.get("list") or nb.get("list"):
                    break
                # RULED-TABLE CELLS are one-per-cell units: the lowercase-
                # continuation heuristic would chain a whole spec-table column
                # ("2x diesels, 10.5 knots" -> "1x jeep or...") into one unit
                # drawn as a crammed paragraph at the first cell
                if pb.get("cell") or nb.get("cell"):
                    break
                # a FIGURE-INTERNAL LABEL (bubble caption, legend entry) is a
                # standalone element drawn strictly in place - never chained
                # into a paragraph with its neighbours
                if pb["type"] == "label" or nb["type"] == "label":
                    break
                # CAPTIONS are self-contained: chaining one into the body
                # flow drags paragraphs into the caption column (and pulls a
                # caption's own text out of it) across pages
                if pb["type"] == "caption" or nb["type"] == "caption":
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
            lpi = int(u["spans"][-1].split(":")[0])
            # slides/spreads: each landscape page is independent - the linear
            # merge already refuses cross-page joins there, and this fix-up
            # must not reintroduce them (a 12pt footer bullet chained onto the
            # next slide's 28pt contact block and dragged it off position)
            if pages[lpi]["width"] > pages[lpi]["height"] or \
                    (lpi + 1 < len(pages) and
                     pages[lpi + 1]["width"] > pages[lpi + 1]["height"]):
                break
            nxt = cont_by_page.get(lpi + 2)  # 1-based next page
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
