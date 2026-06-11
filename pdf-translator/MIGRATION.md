# MIGRATION — GitHub + Claude Code (beginner-friendly)

This repo is ready to push to GitHub and continue in Claude Code. Steps below assume you
have never used git much. Commands are copy-paste; replace `YOUR-NAME` where shown.

## 0. One-time installs
- Git: https://git-scm.com/downloads  (verify: `git --version`)
- Node.js 18+: https://nodejs.org      (verify: `node --version`)
- Claude Code: `npm install -g @anthropic-ai/claude-code`  (verify: `claude --version`)

## 1. Create an empty repository on github.com
1. Click **+** (top-right) → **New repository**.
2. Name it `pdf-translator`. Set it **Private**. Do **not** add README/.gitignore
   (this package already has them). Click **Create repository**.
3. Copy the repo URL shown, e.g. `https://github.com/YOUR-NAME/pdf-translator.git`.

## 2. Put this package under git and push it
Unzip the delivered `pdf-translator.zip`, then in a terminal:
```bash
cd pdf-translator
git init
git add .
git commit -m "Initial commit: M1-M3 pipeline, translator interface, M4 tests, docs"
git branch -M main
git remote add origin https://github.com/YOUR-NAME/pdf-translator.git
git push -u origin main
```
If git asks who you are (first time only):
```bash
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
```

## 3. Start Claude Code in the repo
```bash
cd pdf-translator
claude
```
Claude Code reads `CLAUDE.md` automatically. A good first message:
> Read CLAUDE.md and SPEC.md, then do FIRST TASK 1: de-hardcode the `/home/claude/analysis`
> paths into a CLI/config. Keep everything runnable. Then run the tests in `tests/`.

## 4. Everyday git (so you can undo mistakes)
```bash
git status                 # what changed
git add -A && git commit -m "describe the change"   # save a checkpoint
git push                   # upload to GitHub
git log --oneline          # history
git revert <commit>        # undo a specific commit safely
git restore <file>         # discard local edits to a file
```
Tip: commit after each working change. Small commits make it easy to revert just the
breakage — which is exactly what the M4 tests + git are here to make painless.

## 5. Running the pipeline after migration
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# fonts (Ubuntu/Debian): sudo apt-get install fonts-noto-cjk
# then run the pipeline (see README.md) and: pytest tests/
```

## Notes
- `analysis/` and generated PDFs/fonts are git-ignored (see `.gitignore`); they are
  rebuilt by running the pipeline, so they don't belong in version control.
- The `data/mock_memo.json` is only for the offline demo. Real translation uses an API
  engine selected in `src/translator.py` with `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.
- Never commit API keys. Put them in a local `.env` (already git-ignored) or your shell.
