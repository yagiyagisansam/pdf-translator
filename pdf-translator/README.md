# PDF EN→JA Translator (layout-preserving)

Convert English PDFs — academic papers and slide decks — into Japanese PDFs while
**preserving the exact placement of figures, tables, and other non-text elements**.
English text is removed from the page in place (no white-box masking) and Japanese is
drawn at the same positions, auto-fitted so it never overlaps neighbouring text or figures.

This is built as **general-purpose software**, not tuned to any specific file.

## Features
- Layout analysis: block segmentation, 2-column detection, reading order, figure/caption/
  heading/reference classification, cross-page continuation detection.
- Reading-order stitching: sentences split across columns or pages are translated as a unit.
- Token protection: numbers+units, citation markers, DOIs, emails, abbreviations (CMVJ, ES…)
  are masked before translation and restored after, so they survive verbatim.
- Pluggable translation engine: Anthropic or OpenAI in production; an offline mock for demos.
- In-place English removal via PDF content-stream editing; figures/vectors untouched.
- Japanese overlay with embedded subset Noto CJK fonts (searchable, selectable text).
- References are intentionally left in English.

## Repository layout
```
src/      pipeline modules (m1_analyze, m2_translate, translate_units, make_jp_font, m3_generate)
          translator.py — swappable translation engines
data/     mock_memo.json — offline demo translations (stand-in for an API)
samples/  paper.pdf (2-column paper), deck.pdf (slides) — validation inputs
docs/     sample_output_paper_ja.pdf — example generated output
tests/    M4 verification suite (residual English, overlap, layout regression)
SPEC.md   requirements, design decisions, and hard-won lessons
CLAUDE.md context for AI coding assistants — read first
```

## Setup
Requires Python 3.11+ and the Noto CJK fonts.
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# Noto CJK (Debian/Ubuntu): sudo apt-get install fonts-noto-cjk
```

## Web app (M5)
```bash
python src/webapp.py    # -> http://localhost:8000
```
Upload an English PDF, watch progress, download the Japanese PDF. Each job runs
in its own subprocess and output directory, so concurrent jobs are isolated.
Set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` on the server to enable the API
engines; `mock` works offline for the bundled samples. Knobs:
`PDF_TRANSLATOR_MAX_MB` (upload cap, default 50), `PDF_TRANSLATOR_WORKERS`
(parallel jobs, default 2), `HOST`/`PORT`.

## CLI
One command runs the whole pipeline (M1 -> M2 -> translate -> fonts -> M3):
```bash
python src/pipeline.py samples/paper.pdf --engine mock   # offline demo
python src/pipeline.py mydoc.pdf --engine anthropic      # needs ANTHROPIC_API_KEY
python src/pipeline.py paper deck                        # sample names also work
```
Output lands in `analysis/` (override with `PDF_TRANSLATOR_OUT`). Individual stages
remain runnable standalone with the same arguments:
```bash
python src/m1_analyze.py mydoc.pdf        # -> analysis/<name>_layout.json (+ PNGs)
python src/m2_translate.py mydoc          # -> analysis/<name>_units.json
python src/translate_units.py mydoc mock  # or: anthropic | openai
python src/make_jp_font.py mydoc          # build subset JP fonts
python src/m3_generate.py mydoc.pdf       # -> analysis/<name>_ja.pdf
```
Environment knobs: `PDF_TRANSLATOR_ENGINE`, `PDF_TRANSLATOR_MODEL` (default
`claude-opus-4-8`), `PDF_TRANSLATOR_OUT`, `NOTO_CJK_REGULAR`/`NOTO_CJK_BOLD`,
plus `data/glossary.json` for terminology overrides.

## Verification
```bash
python -m pytest tests/     # M4 suite: residual English, overlap, layout regression
```

## Status
M1-M3 pipeline works end to end (figures preserved, searchable Japanese,
cross-page/column stitching, collision-aware placement) and the M4 suite is wired
and green. Remaining work towards a finished product is prioritised in
`docs/IMPROVEMENT_PLAN.md`; see also CLAUDE.md and SPEC.md.
