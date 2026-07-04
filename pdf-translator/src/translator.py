#!/usr/bin/env python3
"""Pluggable translation engine for the PDF translator.

The rest of the pipeline depends ONLY on the Translator.translate_batch() interface,
so the same code works for any PDF. In production you select an API-backed engine
(Anthropic / OpenAI); in the offline sandbox demo you select MockTranslator.

A unit's text arrives MASKED: non-translatable spans (formulas, numbers+units,
citation markers, DOIs, abbreviations) are already replaced by placeholders ⟦Tn⟧.
The translator must keep those placeholders verbatim. Glossary terms bias terminology.
"""
from __future__ import annotations
import os, json
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional


class Translator:
    """Base interface. Implementations translate English -> Japanese, preserving ⟦Tn⟧."""
    def translate_batch(self, items: List[Dict]) -> List[str]:
        """items: [{"text": masked_en, "glossary": {en: ja}, "kind": "body|heading|..."}]
        returns: list of masked_ja in the same order."""
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
    """Production engine using the Anthropic Messages API."""
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 max_workers: int = 4):
        self.model = model or os.environ.get("PDF_TRANSLATOR_MODEL", "claude-opus-4-8")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.max_workers = max_workers

    def translate_batch(self, items: List[Dict]) -> List[str]:
        import anthropic  # imported lazily so the sandbox doesn't require it
        # SDK retries 429/5xx/connection errors with exponential backoff.
        client = anthropic.Anthropic(api_key=self.api_key, max_retries=4)

        def one(it):
            msg = client.messages.create(
                model=self.model, max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user",
                           "content": _build_user_prompt(it["text"], it.get("glossary", {}), it.get("kind", "body"))}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()

        return _map_concurrent(one, items, self.max_workers)


class OpenAITranslator(Translator):
    """Production engine using the OpenAI Chat Completions API."""
    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None,
                 max_workers: int = 4):
        self.model = model or os.environ.get("PDF_TRANSLATOR_MODEL", "gpt-4o")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.max_workers = max_workers

    def translate_batch(self, items: List[Dict]) -> List[str]:
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key, max_retries=4)

        def one(it):
            r = client.chat.completions.create(
                model=self.model, temperature=0.2,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": _build_user_prompt(it["text"], it.get("glossary", {}), it.get("kind", "body"))}],
            )
            return r.choices[0].message.content.strip()

        return _map_concurrent(one, items, self.max_workers)


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


def get_translator(name: str = None, **kw) -> Translator:
    name = (name or os.environ.get("PDF_TRANSLATOR_ENGINE") or "mock").lower()
    if name == "anthropic":
        kw.pop("memo_path", None)
        return AnthropicTranslator(**kw)
    if name == "openai":
        kw.pop("memo_path", None)
        return OpenAITranslator(**kw)
    if name == "mock":
        memo_path = kw.get("memo_path")
        if not memo_path:
            from config import MOCK_MEMO
            memo_path = MOCK_MEMO
        return MockTranslator(memo_path)
    raise ValueError(f"unknown translator engine: {name}")
