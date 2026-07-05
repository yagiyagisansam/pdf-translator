#!/usr/bin/env python3
"""Shared configuration for the pipeline: repository-relative paths, no hardcoding.

Every path can be overridden by environment variable so the pipeline runs
anywhere (local checkout, CI, container):
  PDF_TRANSLATOR_OUT   output/analysis directory   (default: <repo>/analysis)
  PDF_TRANSLATOR_DATA  data directory (mock memo)  (default: <repo>/data)
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OUT = os.environ.get("PDF_TRANSLATOR_OUT", os.path.join(ROOT, "analysis"))
DATA_DIR = os.environ.get("PDF_TRANSLATOR_DATA", os.path.join(ROOT, "data"))
SAMPLES_DIR = os.path.join(ROOT, "samples")

MOCK_MEMO = os.path.join(DATA_DIR, "mock_memo.json")

# Default sample inputs (used when the CLI is given a bare name instead of a path)
SAMPLES = {
    "paper": os.path.join(SAMPLES_DIR, "paper.pdf"),
    "deck": os.path.join(SAMPLES_DIR, "deck.pdf"),
}


def ensure_out():
    os.makedirs(OUT, exist_ok=True)
    return OUT


def make_jp_font_for(name):
    """Subset the embedding JP fonts to the characters used by <name>'s
    translations. Thin wrapper so callers don't import make_jp_font directly."""
    import make_jp_font
    make_jp_font.make_fonts([name])


def resolve_pdf(name_or_path):
    """Accept either a sample name ('paper') or a filesystem path to a PDF.
    Returns (pdf_path, name)."""
    if os.path.exists(name_or_path) and name_or_path.lower().endswith(".pdf"):
        base = os.path.splitext(os.path.basename(name_or_path))[0]
        return os.path.abspath(name_or_path), base
    if name_or_path in SAMPLES:
        return SAMPLES[name_or_path], name_or_path
    raise FileNotFoundError(
        f"'{name_or_path}' is neither an existing PDF path nor a known sample "
        f"({', '.join(SAMPLES)})")
