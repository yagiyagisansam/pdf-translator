# CLAUDE.md — Context for Claude Code

This file orients an AI coding assistant working on this repository. Read it first.

## What this project is
A **general-purpose** tool that converts English PDFs (academic papers, slide decks)
into Japanese PDFs **while preserving the exact layout** of figures, tables, and other
non-text elements. The emphasis is on generality: **no per-file tuning**. Any logic that
only works for the two sample files is a bug, not a feature.

Work is milestone-based. Historically the user reviews and approves each milestone before
the next begins. Communicate in Japanese with the user when summarizing progress.

## Absolute requirements (do not regress these)
1. **No white-box masking.** English text is removed in place by editing the PDF content
   stream; Japanese is drawn at the same location. Figures/tables/vector art are never touched.
2. **Figure/table coordinates never change.**
3. **Cross-column AND cross-page sentences are translated as one unit** (reading-order stitch).
4. **References must stay in English — never translate them.** (M1 types them `reference`,
   which is excluded from `TRANSLATABLE`, so they are skipped end to end.)
5. **No scattered alphabet fragments, no text-on-text / text-on-figure overlap.** These are
   not "minor"; they block acceptance.
6. **Two placement modes, chosen automatically (editor.build):**
   - **region** (slides, brochures, posters - any landscape page OR portrait docs whose
     translatable text is NOT dominated by multi-line paragraphs): every unit is drawn at
     its SOURCE block's position, at a font size tracking the SOURCE size (a 39pt cover
     title stays display-size; a 7pt callout label stays small). QA enforces this
     mechanically (layout_drift / size_fidelity defects).
   - **reflow** (papers/reports): figures fixed, Japanese reflowed by reading order into
     the page's column structure. Reflow is PER-UNIT SIZED (each unit at its source
     block size x a page shrink factor - never one uniform page size), VERTICALLY
     ANCHORED (a band never starts above its source y), margin units (side captions)
     keep their own bbox, and very-unequal column lanes (<0.7 width ratio) flow
     independently instead of newspaper-balancing.

## The single biggest gotcha: coordinates
`pdfplumber` page coordinates and the PDF **content-stream** coordinates do **not** line up
by any simple formula in this corpus — the offset varies per page (e.g. +30pt on one page,
−58pt on another). Two consequences, already designed around:
- **English removal is content-based** (string match of each text-show op against the
  translated-block text), never coordinate-based. See `m3_generate.remove_text_by_content`.
- **M1 rendering uses pdfplumber's own `to_image()` / `_reproject_bbox()`** rather than manual
  coordinate math. pdfium renders the **cropbox**, not the mediabox, which also shifts coords.

## Pipeline (run in this order)
```
src/m1_analyze.py        # PDF -> <name>_layout.json  (blocks, columns, reading order, figures)
src/m2_translate.py      # layout -> <name>_units.json (stitched units, masked tokens, glossary)
src/translate_units.py   # units  -> <name>_bilingual.json  (calls a Translator engine)
src/make_jp_font.py      # build subset Noto CJK TrueType fonts the overlay can embed
src/m3_generate.py       # bilingual + layout + source PDF -> <name>_ja.pdf
```
Translator engines live in `src/translator.py` (`AnthropicTranslator`, `OpenAITranslator`,
`MockTranslator`). Production uses an API engine; the offline demo uses `mock` backed by
`data/mock_memo.json`. **Do not reintroduce hardcoded per-UID translation tables.**

## Setup status (2026-07)
The original "first tasks" are DONE: paths are de-hardcoded (`src/config.py` + argparse
CLIs + `src/pipeline.py` one-command runner) and the M4 suite in `tests/` is wired and
green (golden layout in `tests/golden/`). Run `python src/pipeline.py paper --engine mock`
then `python -m pytest tests/` before and after any change.

## Segmentation (M1) - island-safe, rebuilt 2026-07-31
Line building is 2D (`_char_segments`): a char joins a line segment only with >=55%
vertical overlap AND bounded horizontal gap; adjacent open segments merge when they
meet (superscripts/± arrive out of order). Segments split at ALIGNMENT EDGES (a gap
landing on a left edge shared by >=3 other segments = a neighbouring island starts
there) - but never right after a list/heading marker ("•", "3."). Blocks are built in
2D too: vertical adjacency + x-overlap + similar font size (ratio <= 1.25), with a
tabular-row guard (a row containing multiple segments inside the block's span never
merges into prose). Rotated (non-upright) text is figure content - excluded entirely.
Spec-table value cells/columns classify as `data` via unit-token heuristics
(`_numericish`). These rules killed the brochure failure modes: char interleaving,
label/paragraph fusion, tables translated as run-on prose, lost display titles.
M2 additionally refuses to merge blocks that are not geometrically one island on
landscape pages, and the capital-start continuation heuristic requires real paragraph
length (>=80 chars), so short labels never chain.

## Robustness facts worth knowing (2026-07-31, round 2)
- English removal RECURSES into Form XObjects (InDesign exports draw text there;
  the page stream alone misses it entirely).
- The overlay draws Japanese in the SOURCE block's fill color (white-on-photo
  labels). Spot colors (Separation/DeviceN) are approximated as ink darkness.
- Reflow bands are VERTICALLY ANCHORED (never start above their source y) and
  cap/grow sizes track the page's source body size.
- Photo-dominated documents (median figure coverage >= 30%) use region mode
  even when portrait.
- Bare bullet-marker blocks ("•") are neither translated nor obstacles.
- RECORD ROWS (a line with a nearby numeric row-mate: TOC rows, table rows) are
  sealed one-line blocks - never merged into paragraphs.
- The fragment sweep never deletes tokens of KEPT blocks (TOC page numbers,
  table cells) - see keep_tokens in remove_text_by_content.
- Link underlines (rules inside a text block's bbox) are NOT obstacles.
- Repeated page furniture is detected at the top AND bottom margins.
- Scanned pages with an OCR text layer are REJECTED with a clear message
  (image pixels cannot be edited; overlaying would double-print).
- Security posture (webapp): whole-site Basic auth + per-IP rate limit + job
  wall-clock timeout + page-count cap + strict security headers + non-root
  container. Keep all of these when touching webapp.py / Dockerfile.

## Known remaining issues (open work, prioritised in docs/IMPROVEMENT_PLAN.md)
- A few stray fragments can survive on the title page (author-line superscript affiliation
  markers a/b/c/d; occasional citation number at a paragraph edge).
- Vector rules (horizontal lines: abstract-box borders, section separators, table rules)
  ARE now obstacles: M1 extracts them (`horizontal_rules` -> page `"rules"`), the producer
  exposes them as obstacle bands, the editor's reflow skips them, and QA flags any
  text-on-rule overlap (`rule_overlap`). Do not regress: keep rules in the obstacle set.
- Deck (slides) Japanese generation is less complete than the paper.
- `mock_memo.json` covers the paper sample only; it is a demo stand-in for a real API.
- pypdf gotcha (already fixed, do not regress): merge overlay pages onto the WRITER's
  pages after `w.append(...)`; merging onto reader pages drops the overlay on every page
  after the first with pypdf 6.x.
- deep-translator gotcha (already fixed, do not regress): `GoogleTranslator` mutates
  internal request state and is NOT thread-safe - construct one instance PER CALL, or
  concurrent units receive each other's translations (and the cache pins the corruption).

## Conventions
- Keep modules runnable standalone and import-safe.
- Prefer content/text-based logic over coordinate math (see gotcha above).
- When you change segmentation in M1/M2, re-run the M4 regression before moving on.
- Do not claim a milestone is done without rendering the output and checking it.
