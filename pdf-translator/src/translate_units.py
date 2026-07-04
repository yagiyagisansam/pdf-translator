#!/usr/bin/env python3
"""General translation step: feeds masked units to a pluggable Translator and
writes <name>_bilingual.json. Works for ANY document - no per-file hardcoding.

Usage:
    python3 translate_units.py <name> [engine]
engine: mock (default, sandbox) | anthropic | openai

Robustness for API engines:
- ⟦Tn⟧ placeholder round-trip is validated; failing units are retried once and
  fall back to untranslated (English stays) rather than losing masked values.
- Results are cached by content hash in <out>/translation_cache.json so
  re-running a document (or a revised document with unchanged paragraphs)
  does not re-pay API calls.
"""
import hashlib, os, re, sys, json
from translator import get_translator
from m2_translate import restore
from config import OUT, MOCK_MEMO, ensure_out

PLACEHOLDER_RE = re.compile(r"⟦T(\d+)⟧")

CACHE_PATH_TMPL = "{out}/translation_cache.json"


def _placeholders_ok(masked_src: str, masked_ja: str) -> bool:
    """The translation must contain exactly the placeholders of the source
    (order-free): a lost ⟦Tn⟧ silently drops a number/citation on restore."""
    return sorted(PLACEHOLDER_RE.findall(masked_src)) == \
        sorted(PLACEHOLDER_RE.findall(masked_ja))


def _cache_key(engine: str, item: dict) -> str:
    payload = json.dumps(
        [engine, os.environ.get("PDF_TRANSLATOR_MODEL", ""),
         item["text"], item.get("glossary", {}), item.get("kind", "")],
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _load_cache(path):
    try:
        return json.load(open(path))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(name: str, engine: str = "mock"):
    ensure_out()
    units = json.load(open(f"{OUT}/{name}_units.json"))
    translator = get_translator(engine, memo_path=MOCK_MEMO)
    # References are excluded upstream (M1 classifies them as type 'reference', which is
    # not in TRANSLATABLE), so every unit here is body/heading/caption/title to translate.
    items = [{"text": u["masked"], "glossary": u.get("glossary", {}),
              "kind": u.get("type", "body")} for u in units]
    # The mock memo stores FINAL inlined Japanese (values substituted), so
    # placeholder round-trip validation only applies to real API engines.
    validate = engine in ("anthropic", "openai")

    cache_path = CACHE_PATH_TMPL.format(out=OUT)
    cache = _load_cache(cache_path)
    keys = [_cache_key(engine, it) for it in items]
    results = [cache.get(k) for k in keys]

    miss = [i for i, r in enumerate(results) if not r]
    if miss:
        fresh = translator.translate_batch([items[i] for i in miss])
        for i, r in zip(miss, fresh):
            results[i] = r
        if validate:
            # one retry for units whose placeholders did not survive
            bad = [i for i in miss
                   if results[i] and not _placeholders_ok(items[i]["text"], results[i])]
            if bad:
                print(f"[{name}] retrying {len(bad)} unit(s) with broken ⟦Tn⟧ placeholders",
                      file=sys.stderr)
                redo = translator.translate_batch([items[i] for i in bad])
                for i, r in zip(bad, redo):
                    results[i] = r
            for i in miss:
                if results[i] and not _placeholders_ok(items[i]["text"], results[i]):
                    print(f"[{name}] unit {units[i]['uid']}: placeholders still broken; "
                          f"leaving English (source preserved)", file=sys.stderr)
                    results[i] = None
        # persist only validated results
        for i in miss:
            if results[i]:
                cache[keys[i]] = results[i]
        json.dump(cache, open(cache_path, "w"), ensure_ascii=False)

    for u, masked_ja in zip(units, results):
        if masked_ja:
            u["target_masked"] = masked_ja
            u["target"] = restore(masked_ja, u["tokens"])
        else:
            u["target_masked"] = None
            u["target"] = None  # unknown text: leave source untranslated
    json.dump(units, open(f"{OUT}/{name}_bilingual.json", "w"), ensure_ascii=False, indent=1)
    n = sum(1 for u in units if u.get("target"))
    print(f"[{name}] engine={engine} translated {n}/{len(units)} units "
          f"({len(items) - len(miss)} from cache)")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "paper"
    engine = sys.argv[2] if len(sys.argv) > 2 else "mock"
    run(name, engine)
