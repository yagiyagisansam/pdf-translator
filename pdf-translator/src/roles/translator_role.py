#!/usr/bin/env python3
"""翻訳者 (Translator).

Reads the producer's layout, builds reading-order translation UNITS (stitching
sentences split across columns/pages), protects non-translatable tokens
(numbers+units, citations, DOIs, abbreviations) as ⟦Tn⟧, translates EN->JA with
the selected engine, and restores the tokens. References are never translated.

Free 'google' engine by default; 'anthropic'/'anthropic-batch'/'openai' when a
key is present. Placeholder round-trip is validated with retry + fallback in
translate_units. Writes <out>/<name>_bilingual.json.
"""
import json
import m2_translate
import translate_units
from config import OUT, ensure_out


def translate(name, engine="google"):
    ensure_out()
    layout = json.load(open(f"{OUT}/{name}_layout.json"))
    units = m2_translate.build_units(layout)
    units = m2_translate.apply_glossary_hint(units, m2_translate.load_glossary())
    for u in units:
        masked, mapping = m2_translate.protect(u["source"])
        u["masked"] = masked
        u["tokens"] = mapping
    with open(f"{OUT}/{name}_units.json", "w") as f:
        json.dump(units, f, ensure_ascii=False, indent=1)
    translate_units.run(name, engine)  # writes <name>_bilingual.json
    return json.load(open(f"{OUT}/{name}_bilingual.json"))


if __name__ == "__main__":
    import argparse
    from translator import ENGINES
    ap = argparse.ArgumentParser(description="Translator role: layout -> bilingual")
    ap.add_argument("name", nargs="?", default="paper")
    ap.add_argument("engine", nargs="?", default="google", choices=ENGINES)
    args = ap.parse_args()
    translate(args.name, args.engine)
