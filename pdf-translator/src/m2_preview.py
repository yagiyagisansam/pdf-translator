#!/usr/bin/env python3
# M2 step 3: translate key deck units + build bilingual HTML preview for review.
import json, html
from m2_translate import restore

OUT = "/home/claude/analysis"

DECK_JP = {
    0: "タイトル: NASA-海軍 遠隔医療: 動揺病に対する自律訓練フィードバック練習(AFTE) 連絡先(氏名のみ): Michael Acromite",
    8: "⟦T0⟧-海軍 遠隔医療プロジェクト: 『動揺病改善のための自律訓練フィードバック練習』",
    9: "発表者: CDR Michael Acromite 海軍航空宇宙医学研究所(⟦T0⟧)",
    10: "SICKNESS' • 問題の定義",
    11: "– 動揺病は海軍航空、特に航空訓練に悪影響を及ぼす。– ⟦T0⟧の研究では、海軍航空学生の81%が1回以上のフライトで航空病を報告した。",
    12: "1回以上のフライトで。さらに、平均以下の飛行成績の82%が動揺病と関連していた。",
    13: "また実運用の場では禁忌である。– 年間約⟦T0⟧–⟦T1⟧件の重度で難治性の症例は、より精密な評価と、自己ペース航空病脱感作(⟦?⟧)プログラムにおけるより積極的な生理的脱感作を必要とする。",
    14: "自己ペース航空病脱感作(⟦T0⟧)プログラムにおけるより積極的な生理的脱感作。",
    15: "SICKNESS' • 機会",
    16: "– NASAのAFTEは、確立された類似のSPAD",
    25: "SICKNESS' • ⟦T0⟧-海軍 協働支援",
}

def build_html(name, title):
    units = json.load(open(f"{OUT}/{name}_bilingual.json")) if name == "paper" \
            else json.load(open(f"{OUT}/{name}_units.json"))
    if name == "deck":
        for u in units:
            jp = DECK_JP.get(u["uid"])
            u["target"] = restore(jp, u["tokens"]) if jp else None
    rows = []
    for u in units:
        if not u.get("target"):
            continue
        badge = []
        if u.get("cross_page"):
            badge.append('<span class="b cp">ページ跨ぎ連結</span>')
        badge.append(f'<span class="b ty">{u["type"]}</span>')
        rows.append(f"""
        <tr>
          <td class="meta">UID{u['uid']}<br>{''.join(badge)}</td>
          <td class="en">{html.escape(u['source'])}</td>
          <td class="jp">{html.escape(u['target'])}</td>
        </tr>""")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<style>
body{{font-family:'Noto Sans CJK JP',sans-serif;margin:24px;color:#1a1a1a}}
h1{{font-size:18px}} .sub{{color:#666;font-size:13px;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%}}
td{{border:1px solid #ddd;padding:8px 10px;vertical-align:top;font-size:13px;line-height:1.6}}
th{{background:#f0f3f7;padding:8px;font-size:12px;border:1px solid #ddd}}
.meta{{width:90px;color:#555;font-size:11px}}
.en{{width:46%;color:#333}} .jp{{width:46%}}
.b{{display:inline-block;font-size:10px;padding:1px 5px;border-radius:3px;margin-top:4px}}
.cp{{background:#fde2e2;color:#b00}} .ty{{background:#e6eefc;color:#1a4}}
</style></head><body>
<h1>{title}</h1>
<div class="sub">M2 翻訳プレビュー（英日対訳）／ ⟦Tn⟧ で保護した数式・数値・引用番号・略語は原文のまま復元</div>
<table><tr><th>ユニット</th><th>英語（原文）</th><th>日本語（訳）</th></tr>
{''.join(rows)}
</table></body></html>"""

if __name__ == "__main__":
    open(f"{OUT}/paper_bilingual.html", "w").write(
        build_html("paper", "論文: アシステッドジャンプが垂直跳び高に及ぼす効果"))
    open(f"{OUT}/deck_bilingual.html", "w").write(
        build_html("deck", "資料: NASA-海軍 遠隔医療プロジェクト"))
    print("bilingual previews written")
