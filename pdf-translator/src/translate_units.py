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
import hashlib, os, re, sys, json, time
from translator import get_translator
from m2_translate import restore
from config import OUT, MOCK_MEMO, ensure_out

PLACEHOLDER_RE = re.compile(r"⟦T(\d+)⟧")

CACHE_PATH_TMPL = "{out}/translation_cache.json"


def _looks_japanese(s: str) -> bool:
    return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" or "｡" <= c <= "ﾟ"
               for c in s or "")


def _echoish(s: str) -> bool:
    """True when a 'translation' is mostly untranslated Latin text - a full or
    partial ENGLISH ECHO from the free endpoint (it returns the input, or
    translates only some sentences). Accepting one strips the original English
    and redraws the same English as 'Japanese'."""
    if not s:
        return False
    latin = sum(c.isascii() and c.isalpha() for c in s)
    return latin > 0.45 * max(len(s), 1)


def _sentence_translate(translator, unit, item):
    """Translate a unit SENTENCE BY SENTENCE with per-sentence validation: each
    sentence's placeholders must round-trip (or get inlined+digit-verified on a
    one-shot retry) and each output must actually be Japanese. Long IR/legal
    paragraphs with dozens of ⟦Tn⟧ reliably lose several on the free endpoint
    as one request; per-sentence, drops are rare and repairable. Returns the
    joined Japanese or None."""
    parts = [p for p in re.split(r"(?<=[.!?;:])\s+", item["text"]) if p.strip()]
    if len(parts) < 2:
        return None
    tok_re = re.compile(r"⟦T\d+⟧")
    outs = []
    for p in parts:
        try:
            o = translator.translate_batch([{"text": p, "kind": item["kind"]}])[0]
        except Exception:
            o = None
        if o and _placeholders_ok(p, o) and not _echoish(o):
            outs.append(o)
            continue
        # inline this sentence's tokens and retry once (values are literals
        # whose digits we can verify)
        keys = tok_re.findall(p)
        q = p
        for k in keys:
            q = q.replace(k, unit["tokens"].get(k, ""))
        try:
            o2 = translator.translate_batch([{"text": q, "kind": item["kind"]}])[0]
        except Exception:
            o2 = None
        if o2 and not _echoish(o2) and \
                _digits_ok([unit["tokens"].get(k, "") for k in keys], o2):
            outs.append(o2)
            continue
        # THIS sentence resists translation (the engine keeps dropping one of
        # its numbers): keep it in ENGLISH with its literal values restored.
        # One English sentence inside a Japanese paragraph beats failing the
        # whole unit back to English (numbers stay verbatim either way).
        outs.append(q)
    joined = " ".join(outs)
    # if the salvage left the unit MOSTLY English it is no translation at all
    return None if _echoish(joined) else joined


def _placeholders_ok(masked_src: str, masked_ja: str) -> bool:
    """The translation must contain exactly the placeholders of the source
    (order-free): a lost ⟦Tn⟧ silently drops a number/citation on restore."""
    return sorted(PLACEHOLDER_RE.findall(masked_src)) == \
        sorted(PLACEHOLDER_RE.findall(masked_ja))


def _digits_ok(token_values, out: str) -> bool:
    """Every numeric run inside the protected token values must appear in the
    output. Japanese legitimately REGROUPS scale ("80 million" -> "8,000万",
    "13.4 billion" -> "134億"), so runs are compared by SIGNIFICAND: separators
    stripped, trailing zeros ignored (bounded, so a dropped digit still fails)."""
    def sig(s):
        d = re.sub(r"\D", "", s)
        return d.rstrip("0") or ("0" if d else ""), len(d)

    outs = [sig(r) for r in re.findall(r"[\d.,]+", out)]
    for v in token_values:
        dv, lv = sig(v)
        if not dv and lv == 0:
            continue
        if not any(ro == dv and abs(lo - lv) <= 4 for ro, lo in outs):
            return False
    return True


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
    if engine != "mock":
        # cached entries written before the echo guard existed may hold
        # English echoes / half-translated text - retranslate those
        from m2_translate import _has_words
        for i, r in enumerate(results):
            if r and _echoish(r) and _has_words(units[i]["source"]):
                results[i] = None

    # TOKEN-ONLY units ("July 16, 2026" masked to a single ⟦T0⟧, a bare URL, a
    # model number line): there is nothing for an engine to translate, and free
    # endpoints often echo garbage or nothing for them. The masked text IS the
    # correct output - restore() then yields the protected value (with dates
    # already rendered as 2026年7月16日). No engine call, fully deterministic.
    if validate:
        for i, it in enumerate(items):
            if results[i]:
                continue
            rest = re.sub(r"⟦T\d+⟧", "", it["text"])
            if it["text"].strip() and not re.search(r"[A-Za-z]{2,}", rest):
                results[i] = it["text"]

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
            # an ENGLISH ECHO (the endpoint returning its input, fully or per
            # sentence) is a failure, not a translation: without this it gets
            # accepted, the original is stripped, and the same English is
            # redrawn as "Japanese"
            from m2_translate import _has_words
            for i in miss:
                if results[i] and _echoish(results[i]) \
                        and _has_words(units[i]["source"]):
                    results[i] = None
            # one retry for units the engine returned NOTHING for (free
            # endpoints intermittently return an empty translation) - a second
            # attempt usually succeeds and an English paragraph left behind is
            # far more visible than one extra request
            for attempt in range(3):
                empty = [i for i in miss
                         if not results[i] and items[i]["text"].strip()]
                if not empty:
                    break
                print(f"[{name}] retrying {len(empty)} unit(s) with empty results "
                      f"(attempt {attempt + 1})", file=sys.stderr)
                if attempt:
                    time.sleep(1.5 * attempt)   # brief backoff before hitting
                                                # the free endpoint again
                redo = _safe_batch(translator.translate_batch,
                                   [items[i] for i in empty])
                for i, r in zip(empty, redo):
                    if r:
                        results[i] = r
            # sentence-split fallback: a long unit the endpoint keeps returning
            # empty for usually succeeds in smaller pieces - far better than
            # leaving the whole paragraph in English
            empty = [i for i in miss
                     if not results[i] and items[i]["text"].strip()]
            for i in empty:
                res = _sentence_translate(translator, units[i], items[i])
                if res:
                    results[i] = res
                    print(f"[{name}] unit {units[i]['uid']}: translated in "
                          f"sentence pieces", file=sys.stderr)
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
            # PARTIAL-UNMASK repair: when the engine DROPPED a few specific
            # tokens (kept the rest), inline just those tokens' literal values
            # into the masked source and retranslate. The surviving tokens stay
            # protected; the inlined values are numeric literals whose digits
            # we can verify - far better than dumping the whole paragraph back
            # to English because one ⟦Tn⟧ went missing.
            tok_re = re.compile(r"⟦T\d+⟧")
            repaired = set()
            for i in list(miss):
                out = results[i]
                # empty results ENTER the loop too - its first branch retries
                # them (a transient engine blank otherwise died silently here,
                # skipping both repair and the unmasked fallback below)
                if out and _placeholders_ok(items[i]["text"], out):
                    continue
                # progressive inlining: each round inlines whatever tokens the
                # engine dropped LAST time and retries - drops are sporadic, so
                # the set of surviving tokens grows until the round-trip closes
                txt = items[i]["text"]
                vals = []
                for _ in range(4):
                    if out is None or out == "":
                        # transient empty - retry the same text once more
                        out = _safe_batch(translator.translate_batch,
                                          [{"text": txt,
                                            "kind": items[i]["kind"]}])[0]
                        continue
                    need = set(tok_re.findall(txt))
                    got = set(tok_re.findall(out))
                    missing, extra = need - got, got - need
                    if extra or len(missing) > 6:
                        break
                    if not missing:
                        if _digits_ok(vals, out) and not _echoish(out):
                            results[i] = out
                            repaired.add(i)
                            print(f"[{name}] unit {units[i]['uid']}: repaired by "
                                  f"inlining {len(vals)} dropped token(s)",
                                  file=sys.stderr)
                        break
                    for k in sorted(missing):
                        v = units[i]["tokens"].get(k, "")
                        txt = txt.replace(k, v)
                        vals.append(v)
                    out = _safe_batch(translator.translate_batch,
                                      [{"text": txt, "kind": items[i]["kind"]}])[0]
                if i not in repaired:
                    # too many drops for whole-unit repair (a 4KB paragraph
                    # with 30+ tokens): per-sentence translation with
                    # per-sentence validation
                    res = _sentence_translate(translator, units[i], items[i])
                    if res:
                        results[i] = res
                        repaired.add(i)
                        print(f"[{name}] unit {units[i]['uid']}: repaired "
                              f"sentence-by-sentence", file=sys.stderr)
            still = [i for i in miss
                     if i not in repaired
                     and (not results[i]
                          or not _placeholders_ok(items[i]["text"], results[i]))]
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

    import unicodedata as _ud
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
        # NFC: the engine sometimes returns DECOMPOSED kana (ホ + combining
        # dakuten); the subset font has no combining-mark glyph and the label
        # renders as tofu. Compose before the font is subset from this file.
        if u["target"]:
            u["target"] = _ud.normalize("NFC", u["target"])
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
