---
title: PDF EN JA Translator
emoji: 📄
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# PDF 英日翻訳(レイアウト保持)

英語のPDF(論文・スライド)を、図表の位置を保ったまま日本語に変換するWebアプリ。
翻訳・PDF製作・編集・確認の4ロールで処理し、参考文献は英語のまま残します。

- アプリ本体とドキュメント: [`pdf-translator/`](pdf-translator/) — 詳細は
  [`pdf-translator/README.md`](pdf-translator/README.md)
- **スマホから使う(無料ホスティング)**:
  [`pdf-translator/docs/DEPLOY.md`](pdf-translator/docs/DEPLOY.md)

> このリポジトリ直下の `Dockerfile` でどのコンテナ環境でも起動できます
> (Hugging Face Spaces / Render / Cloud Run / Railway)。上のフロントマターは
> Hugging Face Spaces 用の設定です。
