#!/usr/bin/env python3
"""General translation step: feeds masked units to a pluggable Translator and
writes <name>_bilingual.json. Works for ANY document - no per-file hardcoding.

Usage:
    python3 translate_units.py <name> [engine]
engine: mock (default, sandbox) | anthropic | openai
"""
import sys, json
from translator import get_translator
from m2_translate import restore

OUT = "/home/claude/analysis"

def run(name: str, engine: str = "mock"):
    units = json.load(open(f"{OUT}/{name}_units.json"))
    translator = get_translator(engine, memo_path=f"{OUT}/mock_memo.json")
    # References are excluded upstream (M1 classifies them as type 'reference', which is
    # not in TRANSLATABLE), so every unit here is body/heading/caption/title to translate.
    items = [{"text": u["masked"], "glossary": u.get("glossary", {}),
              "kind": u.get("type", "body")} for u in units]
    results = translator.translate_batch(items)
    for u, masked_ja in zip(units, results):
        if masked_ja:
            u["target_masked"] = masked_ja
            u["target"] = restore(masked_ja, u["tokens"])
        else:
            u["target_masked"] = None
            u["target"] = None  # unknown text: leave source untranslated
    json.dump(units, open(f"{OUT}/{name}_bilingual.json", "w"), ensure_ascii=False, indent=1)
    n = sum(1 for u in units if u.get("target"))
    print(f"[{name}] engine={engine} translated {n}/{len(units)} units")

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "paper"
    engine = sys.argv[2] if len(sys.argv) > 2 else "mock"
    run(name, engine)
