# スマホだけで無料公開する手順(デプロイ)

このアプリは、リポジトリ直下の `Dockerfile` でそのまま起動できます。パソコン・
ターミナルは不要で、**スマホのブラウザだけ**で無料ホスティングに公開できます。
公開後は、発行されたURLをスマホで開くだけで翻訳アプリが使えます。

推奨は **Render**(GitHubから直接デプロイでき、手順が最少)。代替として
**Hugging Face Spaces**(完全無料・クレジットカード不要)も使えます。

---

## A. Render で公開(推奨・最少タップ)

無料プラン・クレジットカード不要。アイドル後はスリープし、次アクセス時に約1分で
復帰します(個人利用なら問題なし)。

1. スマホのブラウザで **https://render.com** を開き、**「Get Started」→ GitHubで
   サインイン**(ワンタップ)。
2. **New → Blueprint** を選ぶ。
3. リポジトリ一覧から **`yagiyagisansam/pdf-translator`** を選ぶ。
   Render がリポジトリ直下の `render.yaml` を自動で読み込みます。
4. **Apply**(または Create)を押す。ビルド(数分)が終わると
   **`https://pdf-en-ja-translator-xxxx.onrender.com`** のようなURLが発行されます。
5. そのURLをスマホで開けば翻訳アプリが使えます。
   - 翻訳エンジンで **「Gemini」** を選ぶと、キー入力欄にその場でキーを貼り付けて
     使えます(Google AI Studio で無料発行。手順は下の「Geminiキー」参照)。
   - キーを毎回貼るのが面倒なら、Render の Service → **Environment** で
     `GEMINI_API_KEY` を一度設定すれば以後は入力不要です。
   - 誰でもアクセスできるのを避けたい場合は、同じく `PDF_TRANSLATOR_TOKEN` を
     設定してください(設定するとAPIにトークンが必要になります)。

> コードを更新して `main` にマージすると、Render が自動で再デプロイします。

---

## B. Hugging Face Spaces で公開(代替・完全無料)

クレジットカード不要。GitHub の変更を Space へ自動同期するには、下の
GitHub Actions を使います。

1. スマホのブラウザで **https://huggingface.co** に登録/ログイン。
2. **Settings → Access Tokens → New token**(Role: **Write**)を作成しコピー。
3. **New Space** を作成: SDK は **Docker**、名前は任意(例 `pdf-en-ja`)。
4. GitHub の **リポジトリ Settings → Secrets and variables → Actions →
   New repository secret** で以下を登録(すべてスマホのブラウザで可能):
   - `HF_TOKEN` = 手順2のトークン
   - `HF_SPACE` = `あなたのHFユーザー名/Space名`(例 `yagi/pdf-en-ja`)
5. リポジトリに何かをコミット(または下記ワークフローを手動実行)すると、
   `.github/workflows/deploy-hf.yml` が Space へコードを同期し、Space が自動
   ビルドします。完了後、Space のURLをスマホで開けば使えます。

---

## Gemini(無料LLM)キーの取り方 — スマホのみ

1. スマホのブラウザで **https://aistudio.google.com** を開き Google でログイン。
2. **「Get API key」→「Create API key」** で無料キーを発行しコピー。
3. アプリ画面でエンジン **「Gemini」** を選び、キー入力欄に**貼り付け**る。
   (`export` などのコマンドは不要です。)

無料枠の目安: `gemini-2.0-flash` は約15リクエスト/分・1500/日。本アプリは
まとめて送るので1論文あたり数リクエストで収まります。キーレスの
**「Google 翻訳」** エンジンならキー自体が不要です。

---

## メモ

- 生成した日本語PDFは一時的にサーバへ置かれ、既定24時間で自動削除されます
  (`PDF_TRANSLATOR_JOB_TTL_H` で変更可)。翻訳後は早めにダウンロードしてください。
- アップロード上限は既定50MB(`PDF_TRANSLATOR_MAX_MB`)。
- 暗号化PDF・テキスト層のないスキャンPDFは未対応(明確なエラーを表示します)。
