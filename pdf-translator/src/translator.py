#!/usr/bin/env python3
"""Pluggable translation engine for the PDF translator.

The rest of the pipeline depends ONLY on the Translator.translate_batch() interface,
so the same code works for any PDF.

Engines (cheapest first):
  mock             offline demo memo (samples only), zero cost
  google           FREE, no API key: deep-translator -> Google web endpoint.
                   Good quality; no glossary support; be polite with volume.
  anthropic-batch  Anthropic Message Batches API: 50% of standard price,
                   asynchronous (typically minutes). Best for bulk documents.
  anthropic        Anthropic Messages API, interactive latency.
  openai           OpenAI Chat Completions API.

Cost design (api-cost-optimizer):
- Units are GROUPED into one request (up to ~6k chars) so the system prompt is
  paid once per group instead of once per unit (~30 units -> ~4 requests).
- max_tokens is sized from the input length instead of a large flat value.
- Prompt caching is intentionally NOT used: the shared prefix (~250 tokens) is
  far below the model's minimum cacheable prefix, so it can never hit.
- The 50% Batch API discount is exposed as the 'anthropic-batch' engine.

A unit's text arrives MASKED: non-translatable spans (numbers+units, citation
markers, DOIs, abbreviations) are already replaced by placeholders ⟦Tn⟧, which
every engine must keep verbatim (validated + retried in translate_units.py).
"""
from __future__ import annotations
import os, json, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional


class Translator:
    """Base interface. Implementations translate English -> Japanese, preserving ⟦Tn⟧."""
    def translate_batch(self, items: List[Dict]) -> List[str]:
        """items: [{"text": masked_en, "glossary": {en: ja}, "kind": "body|heading|..."}]
        returns: list of masked_ja in the same order ('' = could not translate)."""
        raise NotImplementedError


SYSTEM_PROMPT = (
    "You are a professional English->Japanese translator for academic papers and "
    "technical slides. Translate naturally and accurately into Japanese. "
    "CRITICAL RULES:\n"
    "1. Keep every placeholder of the form ⟦Tn⟧ EXACTLY as-is, in a natural position.\n"
    "2. Do not translate or alter proper nouns, author names, journal names.\n"
    "3. Use the provided glossary for the given terms.\n"
    "4. Output ONLY the Japanese translation, no explanations, no quotes.\n"
    "5. Headings stay concise; do not add numbering that isn't present."
)

# ---- request grouping (shared by the API engines) ----------------------------
SEG_MARK = "⟪SEG{i}⟫"
_SEG_RE = re.compile(r"⟪SEG(\d+)⟫")

GROUP_MAX_CHARS = 6000
GROUP_MAX_UNITS = 10


def _groups(items):
    """Split items into contiguous groups bounded by char/unit budgets."""
    group, chars = [], 0
    for i, it in enumerate(items):
        n = len(it["text"])
        if group and (chars + n > GROUP_MAX_CHARS or len(group) >= GROUP_MAX_UNITS):
            yield group
            group, chars = [], 0
        group.append((i, it))
        chars += n
    if group:
        yield group


def _group_prompt(group):
    glossary = {}
    for _, it in group:
        glossary.update(it.get("glossary", {}))
    g = ""
    if glossary:
        g = ("Glossary (use these):\n"
             + "\n".join(f"- {k} = {v}" for k, v in glossary.items()) + "\n\n")
    parts = [g + "Translate every segment below into Japanese (keep ⟦Tn⟧ placeholders). "
             "Repeat each segment's marker line ⟪SEGn⟫ verbatim before its translation. "
             "Output nothing else."]
    for i, (_, it) in enumerate(group, 1):
        parts.append(f"{SEG_MARK.format(i=i)}\n(type: {it.get('kind', 'body')})\n{it['text']}")
    return "\n\n".join(parts)


def _parse_group(text, n):
    """Split a grouped response back into n translations; None on any mismatch."""
    found = {}
    matches = list(_SEG_RE.finditer(text))
    if len(matches) != n:
        return None
    for k, m in enumerate(matches):
        idx = int(m.group(1))
        end = matches[k + 1].start() if k + 1 < len(matches) else len(text)
        seg = text[m.end():end].strip()
        seg = re.sub(r"^\(type:[^)]*\)\s*", "", seg)  # in case the model echoes it
        if idx in found or not (1 <= idx <= n):
            return None
        found[idx] = seg
    return [found[i] for i in range(1, n + 1)] if len(found) == n else None


def _max_tokens_for(chars):
    """Japanese output is roughly comparable to the English char count in tokens;
    cap generously but not at a blanket maximum."""
    return min(16000, max(1024, int(chars * 1.2) + 256))


def _build_user_prompt(text: str, glossary: Dict[str, str], kind: str) -> str:
    g = ""
    if glossary:
        g = "Glossary (use these):\n" + "\n".join(f"- {k} = {v}" for k, v in glossary.items()) + "\n\n"
    return f"{g}Type: {kind}\nTranslate to Japanese (keep ⟦Tn⟧ placeholders):\n{text}"


def _map_concurrent(fn, items, max_workers):
    """Order-preserving concurrent map (unit order must survive translation)."""
    if max_workers <= 1 or len(items) <= 1:
        return [fn(it) for it in items]
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(fn, items))


class AnthropicTranslator(Translator):
    """Anthropic Messages API. Units are grouped (see module docstring) with a
    per-unit fallback when a grouped response cannot be parsed."""
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 max_workers: int = 4):
        self.model = model or os.environ.get("PDF_TRANSLATOR_MODEL", "claude-opus-4-8")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_workers = max_workers

    def _client(self):
        import anthropic  # lazy so offline runs don't require it
        # SDK retries 429/5xx/connection errors with exponential backoff.
        return anthropic.Anthropic(api_key=self.api_key, max_retries=4)

    def _complete(self, client, prompt, chars):
        msg = client.messages.create(
            model=self.model, max_tokens=_max_tokens_for(chars),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

    def translate_batch(self, items: List[Dict]) -> List[str]:
        client = self._client()
        out = [""] * len(items)

        def one_group(group):
            chars = sum(len(it["text"]) for _, it in group)
            text = self._complete(client, _group_prompt(group), chars)
            parsed = _parse_group(text, len(group))
            if parsed is None:  # fall back to per-unit requests for this group
                parsed = [self._complete(
                    client,
                    _build_user_prompt(it["text"], it.get("glossary", {}), it.get("kind", "body")),
                    len(it["text"])) for _, it in group]
            return group, parsed

        for group, parsed in _map_concurrent(one_group, list(_groups(items)), self.max_workers):
            for (i, _), tr in zip(group, parsed):
                out[i] = tr
        return out


class AnthropicBatchTranslator(AnthropicTranslator):
    """Anthropic Message Batches API: 50% of standard token prices. Asynchronous -
    the call blocks while polling (typically minutes, up to 24h worst case), so
    use it for bulk/offline runs, not the interactive web app."""
    POLL_SECONDS = 15

    def translate_batch(self, items: List[Dict]) -> List[str]:
        client = self._client()
        groups = list(_groups(items))
        requests = []
        for gi, group in enumerate(groups):
            chars = sum(len(it["text"]) for _, it in group)
            requests.append({
                "custom_id": f"g{gi}",
                "params": {
                    "model": self.model, "max_tokens": _max_tokens_for(chars),
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": _group_prompt(group)}],
                },
            })
        batch = client.messages.batches.create(requests=requests)
        while True:
            b = client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            time.sleep(self.POLL_SECONDS)
        out = [""] * len(items)
        fallback = AnthropicTranslator(self.model, self.api_key, self.max_workers)
        for r in client.messages.batches.results(batch.id):
            gi = int(r.custom_id[1:])
            group = groups[gi]
            parsed = None
            if r.result.type == "succeeded":
                text = "".join(b_.text for b_ in r.result.message.content
                               if getattr(b_, "type", "") == "text").strip()
                parsed = _parse_group(text, len(group))
            if parsed is None:  # errored/expired or unparseable -> interactive retry
                parsed = fallback.translate_batch([it for _, it in group])
            for (i, _), tr in zip(group, parsed):
                out[i] = tr
        return out


class OpenAITranslator(Translator):
    """OpenAI Chat Completions API with the same grouping strategy."""
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 max_workers: int = 4):
        self.model = model or os.environ.get("PDF_TRANSLATOR_MODEL", "gpt-4o")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.max_workers = max_workers

    def translate_batch(self, items: List[Dict]) -> List[str]:
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key, max_retries=4)

        def complete(prompt):
            r = client.chat.completions.create(
                model=self.model, temperature=0.2,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": prompt}],
            )
            return r.choices[0].message.content.strip()

        out = [""] * len(items)

        def one_group(group):
            parsed = _parse_group(complete(_group_prompt(group)), len(group))
            if parsed is None:
                parsed = [complete(_build_user_prompt(
                    it["text"], it.get("glossary", {}), it.get("kind", "body")))
                    for _, it in group]
            return group, parsed

        for group, parsed in _map_concurrent(one_group, list(_groups(items)), self.max_workers):
            for (i, _), tr in zip(group, parsed):
                out[i] = tr
        return out


class GoogleFreeTranslator(Translator):
    """FREE keyless engine: deep-translator's Google web endpoint. No account,
    no API key, no quota purchase - suitable for verification and light personal
    use. Preserves ⟦Tn⟧ placeholders in practice (validated upstream anyway).
    Limits: ~5000 chars/request (long units are split on sentence boundaries),
    no glossary support, and it is a best-effort public endpoint - keep
    concurrency low and expect throttling on heavy volume."""
    CHUNK = 4500
    # translate_units may retry a placeholder-mangling unit UNMASKED (numbers
    # pass through Google verbatim; units like 'weeks' localize naturally).
    SUPPORTS_UNMASKED_FALLBACK = True

    def __init__(self, max_workers: int = 2, **_):
        self.max_workers = max_workers

    def _split(self, text):
        if len(text) <= self.CHUNK:
            return [text]
        chunks, cur = [], ""
        for sent in re.split(r"(?<=[.!?。])\s+", text):
            if cur and len(cur) + len(sent) + 1 > self.CHUNK:
                chunks.append(cur); cur = sent
            else:
                cur = f"{cur} {sent}".strip()
        if cur:
            chunks.append(cur)
        return chunks

    def translate_batch(self, items: List[Dict]) -> List[str]:
        from deep_translator import GoogleTranslator

        def one(it):
            text = it["text"].strip()
            if not text:
                return ""
            # Google drops ⟦Tn⟧ tokens glued to words ("second⟦T5⟧-week"), so pad
            # them with spaces for the request and tighten them again afterwards.
            text = re.sub(r"(⟦T\d+⟧)", r" \1 ", text)
            # One translator PER CALL: the instance mutates internal request
            # state and is NOT thread-safe - sharing it across workers crossed
            # responses between units (title got the author line's translation).
            gt = GoogleTranslator(source="en", target="ja")
            try:
                out = "".join((gt.translate(c) or "").strip() for c in self._split(text))
            except Exception:
                return ""  # unit falls back to English (safe default)
            return re.sub(r"\s*(⟦T\d+⟧)\s*", r"\1", out)

        return _map_concurrent(one, items, self.max_workers)

    def translate_batch_fine(self, items: List[Dict]) -> List[str]:
        """Retry path: sentence-sized chunks. Very long units with dozens of
        placeholders occasionally lose one on the public endpoint; per-sentence
        requests keep only a handful of placeholders in flight at a time."""
        fine = GoogleFreeTranslator(max_workers=self.max_workers)
        fine.CHUNK = 300
        return fine.translate_batch(items)


class GeminiTranslator(Translator):
    """FREE LLM engine via Google AI Studio (Gemini). A Google AI Studio API key
    is free to obtain and has a free request tier - set GEMINI_API_KEY (or
    GOOGLE_API_KEY). Higher quality and glossary-aware vs. the keyless Google
    engine, at no cost within the free tier. Units are grouped per request (see
    module docstring) with a per-unit fallback when a group can't be parsed.
    Model via PDF_TRANSLATOR_GEMINI_MODEL (default gemini-2.0-flash)."""
    ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
                "{model}:generateContent")

    def __init__(self, model=None, api_key=None, max_workers=None):
        self.model = model or os.environ.get("PDF_TRANSLATOR_GEMINI_MODEL",
                                             "gemini-2.0-flash")
        self.api_key = (api_key or os.environ.get("GEMINI_API_KEY")
                        or os.environ.get("GOOGLE_API_KEY"))
        # The free tier is rate-limited per MINUTE (gemini-2.0-flash ~15 RPM), so
        # bursty concurrency just trips 429. Default to serial requests spaced by a
        # minimum interval that stays under the limit.
        if max_workers is None:
            max_workers = int(os.environ.get("PDF_TRANSLATOR_GEMINI_WORKERS", "1"))
        self.max_workers = max_workers
        # Requests are spaced >= min_interval apart. The free tier is per-MINUTE
        # limited (~10 RPM for the flash models), so start conservatively; the
        # interval also ADAPTS UP on a 429 to self-tune below whatever the real cap
        # is, and decays back toward the base after sustained success.
        self.base_interval = float(
            os.environ.get("PDF_TRANSLATOR_GEMINI_MIN_INTERVAL", "6.0"))
        self.min_interval = self.base_interval
        self._throttle_lock = threading.Lock()
        self._last_call = 0.0
        # circuit breaker: only trips when the DAILY quota is exhausted (won't
        # recover today) or after many hard failures in a row - NOT on ordinary
        # per-minute 429s, which are retried with backoff so the whole doc still
        # translates (the earlier "only page 2 translated" was the breaker giving
        # up on recoverable rate limits).
        self._fail_streak = 0
        self._circuit_broken = False

    def _throttle(self):
        """Space requests at least min_interval apart (across threads) to stay
        under the free-tier requests-per-minute cap and avoid 429s."""
        import time as _t
        with self._throttle_lock:
            wait = self.min_interval - (_t.monotonic() - self._last_call)
            if wait > 0:
                _t.sleep(wait)
            self._last_call = _t.monotonic()

    @staticmethod
    def _retry_delay(r):
        """Seconds to wait per the server, from Retry-After or the 429 body's
        retryDelay (e.g. '31s'), or None."""
        ra = r.headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except ValueError:
                pass
        try:
            for d in r.json().get("error", {}).get("details", []):
                rd = d.get("retryDelay")
                if rd and rd.endswith("s"):
                    return float(rd[:-1])
        except Exception:
            pass
        return None

    @staticmethod
    def _is_daily_quota(r):
        """True if a 429 is the DAILY free-tier quota (won't recover today) rather
        than the recoverable per-minute cap."""
        try:
            t = r.text.lower()
        except Exception:
            return False
        return "perday" in t or "per day" in t or "requests_per_day" in t

    def _bump_interval(self, factor=1.5, add=0.0, cap=25.0):
        with self._throttle_lock:
            self.min_interval = min(cap, self.min_interval * factor + add)

    def _ease_interval(self):
        with self._throttle_lock:
            self.min_interval = max(self.base_interval, self.min_interval * 0.97)

    def _complete(self, prompt, max_tokens):
        import time as _t
        import requests
        url = self.ENDPOINT.format(model=self.model)
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens},
        }
        if self._circuit_broken:
            return ""                       # daily quota gone - fail fast
        last = ""
        rate_limited = False   # exited due to a recoverable per-minute 429?
        for attempt in range(8):
            self._throttle()
            try:
                r = requests.post(url, params={"key": self.api_key}, json=body,
                                  timeout=120)
            except requests.RequestException as e:
                last = type(e).__name__          # network blip - transient, retry
                _t.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 200:
                data = r.json()
                with self._throttle_lock:
                    self._fail_streak = 0
                self._ease_interval()
                cands = data.get("candidates", [])
                if not cands:
                    return ""
                parts = cands[0].get("content", {}).get("parts", [])
                return "".join(p.get("text", "") for p in parts).strip()
            if r.status_code == 429:
                if self._is_daily_quota(r):
                    self._circuit_broken = True
                    print("[gemini] daily free-tier quota exhausted; leaving the "
                          "rest untranslated (try again tomorrow)", file=sys.stderr)
                    return ""
                # recoverable per-minute cap: slow down and wait as told, keep going
                last = "429"; rate_limited = True
                self._bump_interval(factor=1.0, add=1.5)
                d = self._retry_delay(r)
                _t.sleep(min(d if d is not None else min(2 ** attempt, 45), 60))
                continue
            if r.status_code in (500, 503):
                last = f"{r.status_code}"; rate_limited = False
                _t.sleep(min(2 ** attempt, 30))
                continue
            # any other status (400/403/404 ...): don't crash the whole job -
            # leave this group's units in English and move on
            print(f"[gemini] HTTP {r.status_code}: {r.text[:120]}; leaving those "
                  f"units untranslated", file=sys.stderr)
            return ""
        # Exhausted retries. A pure per-minute rate-limit is RECOVERABLE - do NOT
        # count it toward the circuit breaker (that was the "only page 2 translated"
        # regression); just leave this group English and keep translating the rest.
        # Only hard failures (5xx / network) accrue toward the breaker.
        if rate_limited:
            print("[gemini] rate-limited past retries; this group left English, "
                  "continuing", file=sys.stderr)
            return ""
        with self._throttle_lock:
            self._fail_streak += 1
            streak = self._fail_streak
        if streak >= 6 and not self._circuit_broken:
            self._circuit_broken = True
            print("[gemini] too many failures in a row; leaving the rest "
                  "untranslated", file=sys.stderr)
        else:
            print(f"[gemini] request failed after retries ({last})", file=sys.stderr)
        return ""

    def translate_batch(self, items):
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        out = [""] * len(items)

        def one_group(group):
            chars = sum(len(it["text"]) for _, it in group)
            raw = self._complete(_group_prompt(group), _max_tokens_for(chars))
            if not raw:
                # rate-limited/failed group: don't hammer the API per-unit, just
                # leave them untranslated
                return group, [""] * len(group)
            parsed = _parse_group(raw, len(group))
            if parsed is None:
                parsed = [self._complete(_build_user_prompt(
                    it["text"], it.get("glossary", {}), it.get("kind", "body")),
                    _max_tokens_for(len(it["text"]))) for _, it in group]
            return group, parsed

        for group, parsed in _map_concurrent(one_group, list(_groups(items)),
                                             self.max_workers):
            for (i, _), tr in zip(group, parsed):
                out[i] = tr
        return out


class MockTranslator(Translator):
    """Offline engine for the sandbox demo. Looks up a JSON memo of pre-made
    translations keyed by a normalized prefix of the source. This stands in for the
    API so the full pipeline can be demonstrated without network access.
    It is NOT part of the production path - any unknown text falls back to a marker."""
    def __init__(self, memo_path: str):
        self.memo = []
        if os.path.exists(memo_path):
            raw = json.load(open(memo_path))  # list of {prefix, ja}
            # normalize prefixes once; longest-first so the first startswith hit
            # is the longest match (the early-development bug was first-hit-wins)
            self.memo = sorted(
                ((self._norm(m["prefix"]), m["ja"]) for m in raw),
                key=lambda t: -len(t[0]))

    @staticmethod
    def _norm(s: str) -> str:
        return "".join(s.split())

    def translate_batch(self, items: List[Dict]) -> List[str]:
        out = []
        for it in items:
            key = self._norm(it["text"])
            out.append(next((ja for p, ja in self.memo if key.startswith(p)), ""))
        return out


ENGINES = ("mock", "google", "gemini", "anthropic", "anthropic-batch", "openai")


def get_translator(name: str = None, **kw) -> Translator:
    name = (name or os.environ.get("PDF_TRANSLATOR_ENGINE") or "mock").lower()
    memo_path = kw.pop("memo_path", None)
    if name == "anthropic":
        return AnthropicTranslator(**kw)
    if name == "anthropic-batch":
        return AnthropicBatchTranslator(**kw)
    if name == "openai":
        return OpenAITranslator(**kw)
    if name == "google":
        return GoogleFreeTranslator(**kw)
    if name == "gemini":
        kw.pop("memo_path", None)
        return GeminiTranslator(**kw)
    if name == "mock":
        if not memo_path:
            from config import MOCK_MEMO
            memo_path = MOCK_MEMO
        return MockTranslator(memo_path)
    raise ValueError(f"unknown translator engine: {name} (choose from {ENGINES})")
