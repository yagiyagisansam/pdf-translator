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
6. **Layout method is "B": keep block positions + auto-shrink/​reflow within each block's
   collision-free region.** (Not full column reflow.)

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

## FIRST TASKS for Claude Code (in order)
1. **De-hardcode paths.** Every module currently writes to the absolute path
   `OUT = "/home/claude/analysis"` and the sample PDF paths are hardcoded in `__main__`.
   Replace with a CLI/config: input PDF path, output dir, engine. This is prerequisite to
   everything else and to running outside the original sandbox.
2. **Add the M4 test suite** (see `tests/`): residual-English count, block-overlap detector,
   figure-overlap detector, layout-JSON regression vs a golden file. Wire it so every change
   is checked — this is what prevents the regressions that plagued early development.
3. Only then resume feature work (remaining polish items below).

## Known remaining issues (open work, not yet solved)
- A few stray fragments can survive on the title page (author-line superscript affiliation
  markers a/b/c/d; occasional citation number at a paragraph edge).
- Deck (slides) Japanese generation is less complete than the paper.
- `mock_memo.json` covers the paper sample only; it is a demo stand-in for a real API.

## Conventions
- Keep modules runnable standalone and import-safe.
- Prefer content/text-based logic over coordinate math (see gotcha above).
- When you change segmentation in M1/M2, re-run the M4 regression before moving on.
- Do not claim a milestone is done without rendering the output and checking it.
