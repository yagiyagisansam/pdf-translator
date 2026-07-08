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
   - **自分だけが使えるようにする**には `PDF_TRANSLATOR_TOKEN` に好きな
     **パスワード**を設定します。サイト全体にログイン画面が出て、パスワードを
     知っている人だけが開けます(下の「自分だけアクセス可にする」参照)。

> コードを更新して `main` にマージすると、Render が自動で再デプロイします。

### 自動デプロイが動かない場合(Deploy Hook で確実に自動化)

Render 側の自動デプロイ(GitHub 連携)が効かないことがあります。その場合は
**Deploy Hook** を GitHub Actions から叩く方式に切り替えると、`main` への
マージで確実にデプロイされます(Claude からの手動トリガーも可能になります)。
スマホだけで設定できます:

1. **Render の Deploy Hook URL をコピー**:
   Render ダッシュボード → サービス(pdf-en-ja-translator) → **Settings** →
   **Deploy Hook** の URL(`https://api.render.com/deploy/srv-...?key=...`)をコピー。
2. **GitHub にシークレットとして登録**:
   GitHub のリポジトリ → **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret** で
   - Name: `RENDER_DEPLOY_HOOK`
   - Secret: コピーした URL
   を保存。
3. 以後、`main` に push/マージされるたびに `.github/workflows/render-deploy.yml`
   が Hook を叩いて Render がデプロイします。手動で今すぐデプロイしたい時は
   GitHub → **Actions** → **Deploy to Render** → **Run workflow**。

> シークレット未設定の間はこのワークフローは何もしません(CI は壊れません)。
> 二重デプロイを避けたい場合は、Render 側の Settings → **Auto-Deploy** を
> **Off** にしてください(Hook 経由のみでデプロイされるようになります)。

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

## 自分だけアクセス可にする(パスワード保護)

`PDF_TRANSLATOR_TOKEN` に好きなパスワードを設定するだけです。

- **Render のデプロイ画面**で、`PDF_TRANSLATOR_TOKEN` の Value 欄に
  パスワード(例 `myp@ss123`)を入力 → Deploy。
- あとから変更する場合: Render のサービス → **Environment** →
  `PDF_TRANSLATOR_TOKEN` を追加/編集 → 保存(自動で再デプロイ)。

設定後にサイトを開くと、ブラウザに**ログイン画面**が出ます:
- **ユーザー名**: 何でもOK(例 `me`)
- **パスワード**: 設定した値

一度入れればスマホが記憶します。空にすればURLを知っている人は誰でも使えます。

## 再デプロイでも消さない(Supabase ストレージ連携・スマホのみ)

Render 無料プランはディスクが非永続で、**再デプロイのたびに保存物が消えます**。
Supabase の無料ストレージ(1GB・カード不要)に連携すると、翻訳済みPDFが
**再デプロイ後も残り**、「保存された翻訳」欄からダウンロードし続けられます。
設定はすべてスマホのブラウザだけで完結します。

1. スマホで **https://supabase.com** を開き、GitHub でサインイン → **New project**
   を作成(リージョンは任意、無料プランでOK)。数分でプロビジョニングされます。
2. 左メニュー **Settings(歯車)→ API** を開き、次の2つを控える:
   - **Project URL**(`https://xxxx.supabase.co`)
   - **service_role** キー(`Project API keys` の `service_role` の「Reveal」→コピー。
     ※ `anon` ではなく **service_role** の方。サーバ側だけで使う秘密鍵です)
3. Render のサービス → **Environment** で次を追加して保存(自動で再デプロイ):
   - `SUPABASE_URL` = 手順2の Project URL
   - `SUPABASE_KEY` = 手順2の service_role キー
   - `SUPABASE_BUCKET` = `translations`(任意。未設定でもこの名前が使われます)
4. これだけです。バケットはアプリ起動時に**自動作成**(非公開)されます。以後の
   翻訳は完了時に自動でアップロードされ、再デプロイしても一覧に残ります。

> service_role キーはサーバ内でのみ使い、画面やログには出しません。バケットは
> 非公開のまま、アプリのパスワード保護の内側からのみアクセスされます。未設定なら
> 従来どおりローカルディスクのみで動作します(再デプロイで消えます)。

## 処理中にスリープして消えるのを防ぐ(自動)

Render 無料プランは**約15分アクセスが無いとスリープ**します。スマホでタブを閉じ
たり別アプリに切り替えると、ブラウザの進捗確認(ポーリング)が止まり、**翻訳の途中
でインスタンスが眠って処理ごと消える**ことがあります。

これを防ぐため、アプリは**翻訳ジョブの実行中だけ、自分の公開URLへ定期アクセス**
してスリープを回避します(Render が自動で渡す `RENDER_EXTERNAL_URL` を使用。設定
不要)。完了済みのPDFは「保存された翻訳」欄(+Supabase 連携時はクラウド)に残り
ます。ジョブが**完了するまでは、できれば画面を開いたまま**にしておくと最も確実です。

- 別ホストで公開URLが自動で入らない場合は、環境変数 `PDF_TRANSLATOR_PUBLIC_URL`
  に公開URL(例 `https://xxxx.onrender.com`)を設定すると同じ効果になります。
- 常時起こしておきたい場合は、リポジトリの GitHub Actions
  (`.github/workflows/keepalive.yml`)が10分ごとに `/healthz` を叩きます。

## メモ

- 生成した日本語PDFはサーバに保存され、トップページの「保存された翻訳」欄から
  いつでも再ダウンロードできます。ログインが切れて開き直しても、またサーバ再起動
  後も一覧に残ります(既定7日間で自動削除、`PDF_TRANSLATOR_JOB_TTL_H` で変更可)。
  ※ 標準では Render の**再デプロイ**時に保存物が消えます。上の「再デプロイでも
  消さない(Supabase 連携)」を設定すると、再デプロイ後も残り続けます。
- アップロード上限は既定50MB(`PDF_TRANSLATOR_MAX_MB`)。
- 暗号化PDF・テキスト層のないスキャンPDFは未対応(明確なエラーを表示します)。
