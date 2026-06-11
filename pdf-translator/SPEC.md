# SPEC — Requirements, Design Decisions, Lessons

## Requirements (from the user)
- Web-app form factor (future M5). Translation via LLM API (GPT / Claude).
- No white-box masking: remove English text in place, draw Japanese at the same position.
- Figure/table coordinates must not change.
- Stitch text that flows across columns within a page AND across pages, translate as one.
- **Method B** chosen: keep block positions + auto-shrink/reflow inside each block's
  collision-free region. (Method A = full column reflow — rejected.)
- References stay in English; never translate them.
- Scattered alphabet fragments and text/line/figure overlaps are unacceptable (not "minor").
- General-purpose: **no sample-specific hardcoding.** This was raised repeatedly.

## Architecture
```
M1 m1_analyze.py     PDF -> layout.json
M2 m2_translate.py   layout -> units.json   (stitch + token protection + glossary)
   translate_units.py units -> bilingual.json via a Translator engine
   translator.py      Anthropic / OpenAI / Mock (get_translator factory)
   make_jp_font.py    subset Noto CJK CFF -> TrueType for embedding
M3 m3_generate.py    bilingual + layout + PDF -> *_ja.pdf
```

## Key technical lessons (do not relearn the hard way)
1. **pdfplumber coords ≠ content-stream coords**, and the offset is **per-page irregular**
   (observed +30pt to −58pt). Never drive English removal from coordinates.
   - English removal = **content/string matching** of each text-show op against the
     concatenated text of translated blocks (`remove_text_by_content`).
   - M1 annotation rendering uses pdfplumber's own `to_image()` + `_reproject_bbox()`.
   - pdfium renders the **cropbox**, not the mediabox — another source of misalignment.
2. **Normalization for matching** must expand font-specific ligature/separator glyphs:
   e.g. `U+02DA`→`fi`, `U+02DC`→`fl`, `U+0152`(Œ used as en-dash)→`-`, then keep
   alphanumerics only, lowercased. Otherwise "significant"/"5–7" won't match.
3. **Two-pass fragment kill.** Pass 1 removes ops whose text is in the translated blob.
   Pass 2 removes short stray ops (superscript citations, `p`, `ES`, `a`–`d`, `±`, `×`)
   that sit **between two dropped ops in content-stream order** — these are pieces of a
   translated paragraph emitted as separate tiny ops. Running heads / page numbers /
   table cells are not sandwiched between dropped body text, so they survive.
4. **Stitching (M2).** Walk the global reading order; merge consecutive flowing blocks
   when the previous block doesn't end with sentence-final punctuation and the next
   continues it. Hard boundaries: headings, captions, titles, large-font (≥1.3× body)
   blocks, and footer lines (DOI, ©, ISSN, "Corresponding author", front-matter).
5. **Column detection (M1).** Gutter found from an x-coverage histogram that **excludes
   full-width rows**, looking for a whitespace band in the central 30–70%. Pass the gutter
   into line clustering to force the split on mixed-width pages.
6. **Placement (M3, Method B).** Each stitched unit is reflowed across its constituent
   block **regions** (page/column groups) in reading order. Each region has a collision-free
   box: start pushed below any overlapping figure (captions get extra clearance because
   chart axis labels render just outside the detected image bbox); available bottom clipped
   to the nearest element below. Font size is the largest (cap 10.5, floor 5.5) that fits the
   total capacity; every region is clipped to its own line capacity, so overlap is impossible.
   Headings get their width extended to the column's right edge so short JP headings
   ("3. 結果") don't wrap and clip.
7. **Fonts.** Noto CJK ships as CFF/OpenType, which reportlab can't embed directly. Subset to
   the used characters, convert CFF→glyf (`Cu2QuPen`+`TTGlyphPen`), add an empty `loca`, drop
   `VORG`/`CFF `, set maxp v1.0, then round-trip-save so `loca`/`glyf` recompile.
8. **Token masking.** Protect numbers/units, citations, DOIs, emails, abbreviations as `⟦Tn⟧`
   before translation; restore after. The mock memo stores **final inlined Japanese** (values
   already substituted) keyed by source-prefix, to avoid token-index drift in the demo —
   a real API engine round-trips the `⟦Tn⟧` placeholders instead.

## Anti-patterns that caused regressions (avoid)
- Hardcoded per-UID translation tables → broke whenever segmentation renumbered units.
- Coordinate-based English removal → left English on some pages, removed wrong text on others.
- Placing a multi-block unit's whole text at its first block → overflow/overlap; must reflow.
- `MockTranslator` matching the **first** prefix instead of the **longest** → wrong/short hits.

## Validation
Two sample inputs in `samples/`: a 5-page 2-column paper and a 9-page slide deck. A correct
M1 run reproduces `docs`-level layout JSON (block texts + coords + types stable). M4 (to be
added) automates: residual-English count, block-overlap, figure-overlap, layout regression.
