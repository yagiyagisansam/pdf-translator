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


def _digits_ok(token_values, out: str) -> bool:
    """Every numeric run inside the protected token values must appear in the
    output at least as often (multiset containment)."""
    from collections import Counter
    need = Counter()
    for v in token_values:
        need.update(re.findall(r"\d+(?:\.\d+)?", v))
    have = Counter(re.findall(r"\d+(?:\.\d+)?", out))
    return all(have[d] >= c for d, c in need.items())


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
    # placeholder round-trip validation applies to every real engine but mock.
    validate = engine != "mock"

    cache_path = CACHE_PATH_TMPL.format(out=OUT)
    cache = _load_cache(cache_path)
    keys = [_cache_key(engine, it) for it in items]
    results = [cache.get(k) for k in keys]

    def _safe_batch(fn, batch):
        """Never let an engine/network exception crash the whole run - a raise
        here would discard every already-translated unit. Degrade to untranslated
        (English preserved downstream) instead."""
        try:
            return fn(batch)
        except Exception as e:
            print(f"[{name}] translation engine error ({type(e).__name__}: {e}); "
                  f"leaving {len(batch)} unit(s) untranslated", file=sys.stderr)
            return [None] * len(batch)

    miss = [i for i, r in enumerate(results) if not r]
    direct = {}  # index -> final Japanese produced from UNMASKED source
    if miss:
        fresh = _safe_batch(translator.translate_batch, [items[i] for i in miss])
        for i, r in zip(miss, fresh):
            results[i] = r
        if validate:
            # one retry for units whose placeholders did not survive
            bad = [i for i in miss
                   if results[i] and not _placeholders_ok(items[i]["text"], results[i])]
            if bad:
                print(f"[{name}] retrying {len(bad)} unit(s) with broken ⟦Tn⟧ placeholders",
                      file=sys.stderr)
                retry = getattr(translator, "translate_batch_fine", translator.translate_batch)
                redo = _safe_batch(retry, [items[i] for i in bad])
                for i, r in zip(bad, redo):
                    results[i] = r
            still = [i for i in miss
                     if results[i] and not _placeholders_ok(items[i]["text"], results[i])]
            for i in still:
                results[i] = None
            # last resort for engines that allow it (google): translate the
            # UNMASKED source and accept it only if every protected number
            # survived verbatim - better Japanese than leaving English.
            if still and getattr(translator, "SUPPORTS_UNMASKED_FALLBACK", False):
                outs = _safe_batch(translator.translate_batch,
                    [{"text": units[i]["source"], "kind": items[i]["kind"]} for i in still])
                for i, out in zip(still, outs):
                    if out and _digits_ok(units[i]["tokens"].values(), out):
                        direct[i] = out
                        still_msg = "translated unmasked (numbers verified)"
                    else:
                        still_msg = "leaving English (source preserved)"
                    print(f"[{name}] unit {units[i]['uid']}: placeholders broken; "
                          f"{still_msg}", file=sys.stderr)
            else:
                for i in still:
                    print(f"[{name}] unit {units[i]['uid']}: placeholders still broken; "
                          f"leaving English (source preserved)", file=sys.stderr)
        # persist only validated results
        for i in miss:
            if results[i]:
                cache[keys[i]] = results[i]
        json.dump(cache, open(cache_path, "w"), ensure_ascii=False)

    for i, (u, masked_ja) in enumerate(zip(units, results)):
        if masked_ja:
            u["target_masked"] = masked_ja
            u["target"] = restore(masked_ja, u["tokens"])
        elif i in direct:
            u["target_masked"] = None
            u["target"] = direct[i]  # unmasked-fallback translation
        else:
            u["target_masked"] = None
            u["target"] = None  # unknown text: leave source untranslated
    json.dump(units, open(f"{OUT}/{name}_bilingual.json", "w"), ensure_ascii=False, indent=1)
    n = sum(1 for u in units if u.get("target"))
    print(f"[{name}] engine={engine} translated {n}/{len(units)} units "
          f"({len(items) - len(miss)} from cache)")


# ---- cost pre-estimation (api-cost-optimizer) --------------------------------
# Heuristic, keyless: EN input ~4 chars/token; JA output tokens ~0.5x EN chars.
PRICES_PER_MTOK = {  # (input USD, output USD) per million tokens
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def estimate(name: str):
    units = json.load(open(f"{OUT}/{name}_units.json"))
    from translator import GROUP_MAX_CHARS, GROUP_MAX_UNITS
    chars = sum(len(u["masked"]) for u in units)
    groups = max(1, -(-len(units) // GROUP_MAX_UNITS),
                 -(-chars // GROUP_MAX_CHARS))
    in_tok = chars / 4 + groups * 280 + len(units) * 12   # system prompt per group
    out_tok = chars * 0.5
    print(f"[{name}] units={len(units)} masked_chars={chars} "
          f"~{groups} requests  est input≈{in_tok/1000:.1f}k tok  output≈{out_tok/1000:.1f}k tok")
    for model, (pi, po) in PRICES_PER_MTOK.items():
        cost = in_tok / 1e6 * pi + out_tok / 1e6 * po
        print(f"  {model:<18} ${cost:.3f}   (anthropic-batch: ${cost/2:.3f})")
    print("  google / mock      $0 (free)")
    print("  (heuristic estimate; unchanged paragraphs are served from the "
          "translation cache at $0 on re-runs)")


if __name__ == "__main__":
    import argparse
    from translator import ENGINES
    ap = argparse.ArgumentParser(description="Translate <name>_units.json -> <name>_bilingual.json")
    ap.add_argument("name", nargs="?", default="paper")
    ap.add_argument("engine", nargs="?", default="mock", choices=ENGINES)
    ap.add_argument("--estimate", action="store_true",
                    help="print an API cost estimate and exit (no translation)")
    args = ap.parse_args()
    if args.estimate:
        estimate(args.name)
    else:
        run(args.name, args.engine)
