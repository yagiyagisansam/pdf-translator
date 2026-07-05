#!/usr/bin/env python3
"""One-command pipeline: PDF in -> Japanese PDF out.

    python src/pipeline.py samples/paper.pdf --engine mock
    python src/pipeline.py mydoc.pdf --engine anthropic     # needs ANTHROPIC_API_KEY
    python src/pipeline.py paper deck --engine mock         # sample names also work

Steps: M1 layout analysis -> M2 unit building -> translation -> font subsetting
-> M3 Japanese PDF generation. Output lands in the analysis dir (see config.py).
"""
import argparse, json, os, sys, time

from config import OUT, ensure_out, resolve_pdf
import m1_analyze
import m2_translate
import translate_units
import make_jp_font
import m3_generate


class UnsupportedPdfError(ValueError):
    """Input PDF that the pipeline cannot process, with a user-facing reason."""


def validate_pdf(pdf_path):
    """Fail fast with a clear message on inputs the pipeline cannot handle:
    encrypted PDFs and image-only (scanned) PDFs without a text layer."""
    from pypdf import PdfReader
    try:
        rd = PdfReader(pdf_path)
    except Exception as e:
        raise UnsupportedPdfError(f"cannot open as PDF: {e}")
    if rd.is_encrypted:
        raise UnsupportedPdfError(
            "encrypted/password-protected PDFs are not supported "
            "(暗号化されたPDFは未対応です)")
    import pdfplumber
    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf.pages) == 0:
            raise UnsupportedPdfError("PDF has no pages")
        nchars = sum(len(p.chars) for p in pdf.pages[:5])
    if nchars < 50:
        raise UnsupportedPdfError(
            "no extractable text layer - scanned/image-only PDFs need OCR first "
            "(テキスト層がないスキャンPDFは未対応です。先にOCRをかけてください)")


def run_one(pdf_path, name, engine, render):
    t0 = time.time()
    validate_pdf(pdf_path)
    print(f"== [{name}] M1 layout analysis: {pdf_path}")
    doc, _ = m1_analyze.analyze_pdf(pdf_path, name, render=render)
    nb = sum(len(p["blocks"]) for p in doc["pages"])
    print(f"   pages={len(doc['pages'])} blocks={nb} "
          f"cross_page_joins={len(doc['cross_page_joins'])}")

    print(f"== [{name}] M2 translation units")
    layout = json.load(open(f"{OUT}/{name}_layout.json"))
    units = m2_translate.build_units(layout)
    units = m2_translate.apply_glossary_hint(units, m2_translate.load_glossary())
    for u in units:
        masked, mapping = m2_translate.protect(u["source"])
        u["masked"] = masked
        u["tokens"] = mapping
    with open(f"{OUT}/{name}_units.json", "w") as f:
        json.dump(units, f, ensure_ascii=False, indent=1)
    print(f"   units={len(units)} cross_page={sum(1 for u in units if u['cross_page'])}")

    print(f"== [{name}] translate (engine={engine})")
    translate_units.run(name, engine)

    print(f"== [{name}] subset JP fonts")
    make_jp_font.make_fonts([name])

    print(f"== [{name}] M3 generate Japanese PDF")
    out, stripped = m3_generate.generate(name, pdf_path)
    print(f"   -> {out}  ({time.time()-t0:.1f}s total)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="PDF paths or sample names (paper/deck)")
    from translator import ENGINES
    ap.add_argument("--engine", default=os.environ.get("PDF_TRANSLATOR_ENGINE", "mock"),
                    choices=list(ENGINES),
                    help="translation engine (default: mock; 'google' is free & keyless)")
    ap.add_argument("--name", help="override document name (single input only)")
    ap.add_argument("--render", action="store_true",
                    help="also render annotated layout PNGs (slower)")
    args = ap.parse_args()
    if args.name and len(args.inputs) != 1:
        ap.error("--name requires exactly one input")
    ensure_out()
    outputs = []
    for inp in args.inputs:
        pdf_path, name = resolve_pdf(inp)
        if args.name:
            name = args.name
        try:
            outputs.append(run_one(pdf_path, name, args.engine, args.render))
        except UnsupportedPdfError as e:
            print(f"error: [{name}] {e}", file=sys.stderr)
            sys.exit(2)
    print("\nDone:")
    for o in outputs:
        print(" ", o)


if __name__ == "__main__":
    main()
