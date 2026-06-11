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

## Usage (current state — paths are being de-hardcoded; see CLAUDE.md task 1)
```bash
cd src
python m1_analyze.py            # -> analysis/<name>_layout.json (+ annotated PNGs)
python m2_translate.py          # -> analysis/<name>_units.json
python translate_units.py paper mock      # or: anthropic | openai
python make_jp_font.py          # build subset JP fonts
python m3_generate.py paper     # -> analysis/paper_ja.pdf
```
Select a production engine with an API key:
```bash
export ANTHROPIC_API_KEY=...    # then: python translate_units.py paper anthropic
export OPENAI_API_KEY=...       # then: python translate_units.py paper openai
```

## Status
M1 (layout) and M2 (translation pipeline) complete and verified. M3 (Japanese PDF
generation) largely working: figures preserved, searchable Japanese, cross-page/column
stitching, collision-aware placement. Remaining polish and the M4 automated checks are
the next work — see CLAUDE.md and SPEC.md.
