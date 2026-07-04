# 修正案 — 完成版に向けた改善計画

現状は「M1〜M3 が動作するプロトタイプ+M4 テスト」の段階であり、完成版とは言えません。
本ドキュメントは、今回の最適化(2026-07)で実施済みの項目と、完成版に到達するために
必要な残作業を優先度付きで整理したものです。

## 今回実施済み(このブランチ)

| 項目 | 内容 |
|---|---|
| パスの脱ハードコード | `/home/claude/analysis`・`/mnt/user-data/uploads` を全廃。`src/config.py` に集約し、`PDF_TRANSLATOR_OUT` 等の環境変数と CLI 引数で任意の PDF / 出力先を指定可能に |
| ワンコマンド化 | `python src/pipeline.py <pdf> --engine mock|anthropic|openai` で M1→M2→翻訳→フォント→M3 を一括実行(paper サンプルで約4秒) |
| **重大バグ修正: 2ページ目以降の日本語欠落** | pypdf 6.x では「reader のページに merge_page してから add_page」する旧実装だと、オーバーレイのフォント資源が共有されている 2 ページ目以降の日本語が**無言で消える**。writer に append してから merge する方式に修正し、全ページに日本語が入ることを検証済み |
| M3 の高速化 | `_wrap` の文字幅をフォント×サイズ単位でキャッシュ(従来は1文字進むごとに行全体を再計測する O(n²))。フォントサイズ探索×全ユニットで効いてくる主要ホットパス |
| Mock エンジンの高速化 | prefix を初期化時に正規化+最長順ソート。従来は毎ユニット×全 memo エントリを都度正規化 |
| API エンジンの堅牢化 | SDK リトライ(max_retries=4)+順序保存の並列リクエスト(既定4並列)。廃止予定だった `claude-sonnet-4-20250514` を `claude-opus-4-8` に更新(`PDF_TRANSLATOR_MODEL` で変更可) |
| mock memo のパス修正 | 移行後は `analysis/` に memo が存在せず mock が常に空振りしていた問題を修正(`data/mock_memo.json` を参照) |
| M4 テストの配線 | M3 が実際に描画した行ボックスを `<name>_placed.json` に出力し、「日本語と図の重なりゼロ」「描画行同士の重なりゼロ」を実データで検査。残存英語はサンプル固有の許可リストを廃止し、パイプライン自身のデータから許可語を導出する汎用ロジックに。golden レイアウト(`tests/golden/`)による M1 回帰検査も有効化。**5テスト全て green** |
| 用語集の外部化 | `data/glossary.json` を置けばコード変更なしで用語を上書き可能に |
| フォントパス探索 | Noto CJK を複数の標準パスから探索、`NOTO_CJK_REGULAR/BOLD` で上書き可 |

## 完成版までの残作業(優先度順)

### P1 — 品質:既知のレイアウト解析(M1)不具合
1. **Table キャプションの行混在**(最重要)
   paper サンプル p.2 で「Table 1」と「Assisted and normal CMVJ …」の2行が
   文字単位で混ざった1ブロック(`TAasbslieste1dand…`)になる。原因は
   `cluster_lines` の行クラスタリング(中心y±0.6×中央値身長)が、近接する
   小さいフォント行を同一行に併合するため。→ フォントサイズが大きく異なる
   文字は同一行に併合しない条件を追加する。これが直れば残存英語テストの
   しきい値を 8 → ほぼ 0 に締められる。
2. **上付き引用番号による重複ブロック**(p.4 の `7,17 reducedthepeakforce…` 等)
   上付き文字が別の行として分離され、本文とほぼ同じ領域を占める重複ブロックを
   生成する。→ 行クラスタリング後に「小さいフォントで直前行と x 範囲が重なる行」
   を前行へ吸収する後処理を追加。
3. **タイトルページの散在断片**(著者行の a/b/c/d、段落端の引用番号)
   CLAUDE.md 記載の既知課題。フラグメント除去(pass 2)の「両隣が削除済み」条件を
   「近傍 n 個中の多数が削除済み」に緩めるのが有力。
4. **単語分割の残骸**("Spratf / ord" のような行またぎ分割)
   英語除去後に残ったオペを再結合しないため。除去対象ユニットに部分一致する
   短いオペも kill 対象に含める(現在は3文字以下のみ)。

### P2 — 機能:実用に必要
5. **deck(スライド)系の完成**
   mock memo が paper のみ対応。API エンジンでの deck 検証と、スライド特有の
   レイアウト(テキストボックス散在、背景色、縦横比)への placement 対応。
6. **翻訳キャッシュ**
   同一ユニットの再翻訳を避ける content-hash キャッシュ(`analysis/cache/`)。
   API コスト・再実行時間を大幅削減し、失敗時の途中再開も可能になる。
7. **⟦Tn⟧ プレースホルダ検証**
   API 応答でプレースホルダが欠落/重複した場合の検出と自動リトライ
   (現在は無検証で restore するため、欠落すると数値が消える)。
8. **バッチ翻訳**
   1ユニット=1リクエストは遅く高コスト。複数ユニットを1リクエストに束ねる、
   もしくは Anthropic Message Batches API(50%割引)対応。
9. **フォント整合**
   本文サイズに対する見出しの相対サイズ維持、太字/明朝の使い分け、
   キャップ 10.5pt/床 5.5pt の再検討(縮小されすぎたページの可読性)。

### P3 — プロダクト化(M5)
10. **Web アプリ化**(SPEC の将来要件)
    FastAPI + ジョブキューで「PDF アップロード → 進捗表示 → ダウンロード」。
    パイプラインは今回の CLI 化で関数として呼べる形になっているため、
    薄い API 層を被せるだけでよい。
11. **CI 整備**
    GitHub Actions で `pip install -r requirements.txt` + `fonts-noto-cjk` +
    `python src/pipeline.py paper` + `pytest tests/` を PR ごとに実行。
12. **エラーハンドリングとログ**
    暗号化 PDF・スキャン(画像のみ)PDF・フォント抽出不能などの明示的な
    エラーメッセージ。`logging` への移行。
13. **対訳プレビューの汎用化**
    `m2_preview.py` の deck 用ハードコード(`DECK_JP`)を撤去し、
    bilingual.json から任意文書のレビュー用 HTML を生成する形に統一。

## 検証手順(この状態の再現)

```bash
pip install -r requirements.txt
sudo apt-get install fonts-noto-cjk   # Debian/Ubuntu
python src/pipeline.py paper --engine mock   # -> analysis/paper_ja.pdf
python -m pytest tests/                      # 5 passed
```
