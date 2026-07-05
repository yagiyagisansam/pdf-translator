#!/usr/bin/env python3
"""Role orchestrator: 翻訳者 -> PDF製作者 -> 編集者 -> 確認者, with a bounded
QA-driven retry loop.

Flow:
  1. producer.analyze    source PDF -> layout spec
  2. translator.translate layout -> Japanese units (engine selectable)
  3. editor.build        layout + Japanese -> reconstructed PDF
  4. qa.review           pass/fail + defects
     on failure, map each defect to a role fix and re-run the affected step,
     tightening parameters (lower font floor on layout defects; re-translate on
     Japanese defects), up to `max_rounds`.

Progress is reported through an optional callback(stage, detail) so the web app
can surface which role is working.
"""
import os

from config import ensure_out, resolve_pdf, make_jp_font_for
from roles import producer, translator_role, editor, qa


def _emit(cb, stage, detail=""):
    if cb:
        cb(stage, detail)


def convert(pdf_path, name, engine="google", max_rounds=3, progress=None):
    ensure_out()
    from pipeline import validate_pdf  # encrypted / scanned-PDF gate with JP message
    validate_pdf(pdf_path)
    _emit(progress, "producer", "レイアウト解析")
    producer.analyze(pdf_path, name)

    _emit(progress, "translator", f"翻訳 (engine={engine})")
    translator_role.translate(name, engine)

    make_jp_font_for(name)  # subset fonts to the produced Japanese

    floor = 5.5
    report = None
    for rnd in range(1, max_rounds + 1):
        _emit(progress, "editor", f"配置 (round {rnd})")
        report = editor.build(name, pdf_path, floor=floor)

        _emit(progress, "qa", f"検査 (round {rnd})")
        verdict = qa.review(name, report)
        if verdict["ok"]:
            _emit(progress, "done", f"合格 (round {rnd})")
            return {"out_path": report["out_path"], "rounds": rnd,
                    "ok": True, "defects": []}

        # map defects to fixes for the next round
        roles_hit = {d["role"] for d in verdict["defects"]}
        params = {d["param"] for d in verdict["defects"]}
        _emit(progress, "qa", "不適合→修正: "
              + "; ".join(d["detail"] for d in verdict["defects"])[:200])
        if "shrink" in params:
            floor = max(4.5, floor - 1.0)
        if "reengine" in params and engine == "google" and os.environ.get("ANTHROPIC_API_KEY"):
            engine = "anthropic"  # escalate translation quality if a key exists
            _emit(progress, "translator", "再翻訳 (anthropic)")
            translator_role.translate(name, engine)
            make_jp_font_for(name)
        # 'restrip' is handled by re-running editor.build (idempotent) next round

    _emit(progress, "done", f"未解決の指摘ありで出力 ({len(verdict['defects'])}件)")
    return {"out_path": report["out_path"], "rounds": max_rounds,
            "ok": False, "defects": verdict["defects"]}


if __name__ == "__main__":
    import argparse
    from translator import ENGINES
    ap = argparse.ArgumentParser(description="Role orchestrator: PDF -> Japanese PDF")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--engine", default="google", choices=ENGINES)
    ap.add_argument("--name")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    if args.name and len(args.inputs) != 1:
        ap.error("--name requires exactly one input")
    import sys
    from pipeline import UnsupportedPdfError
    for inp in args.inputs:
        src, name = resolve_pdf(inp)
        try:
            res = convert(src, args.name or name, args.engine, args.rounds,
                          progress=lambda s, d: print(f"  [{s}] {d}"))
        except UnsupportedPdfError as e:
            print(f"error: [{name}] {e}", file=sys.stderr)
            sys.exit(2)
        print(f"[{name}] {'OK' if res['ok'] else 'WITH DEFECTS'} "
              f"in {res['rounds']} round(s) -> {res['out_path']}")
