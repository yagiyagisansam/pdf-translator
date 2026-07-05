#!/usr/bin/env python3
"""M5: Web app - upload an English PDF, get the layout-preserving Japanese PDF.

    pip install fastapi uvicorn python-multipart
    python src/webapp.py            # -> http://localhost:8000

Each job runs the 4-role orchestrator in a subprocess with its own output
directory (PDF_TRANSLATOR_OUT), so concurrent jobs never share font/analysis
files and a crash in one job cannot take the server down.

Operations:
- Job metadata is PERSISTED to <job>/job.json and reloaded on startup, so a
  restart doesn't lose finished jobs / their downloads.
- Finished job directories are swept after PDF_TRANSLATOR_JOB_TTL_H hours
  (default 24); a background thread also runs the sweep hourly.
- Optional auth: if PDF_TRANSLATOR_TOKEN is set, every /api call must send it as
  `Authorization: Bearer <token>` (or ?token=). Unset = open (local/free use).

Engines: 'google' (free, keyless) default; 'gemini' (free tier, needs a Google
AI Studio key); 'anthropic'/'openai' paid; 'mock' offline demo.
"""
import json as _json
import os, re, shutil, subprocess, sys, threading, time, uuid

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_DIR = os.environ.get("PDF_TRANSLATOR_JOBS",
                          os.path.join(ROOT, "analysis", "webjobs"))
MAX_UPLOAD_MB = int(os.environ.get("PDF_TRANSLATOR_MAX_MB", "50"))
MAX_PARALLEL = int(os.environ.get("PDF_TRANSLATOR_WORKERS", "2"))
JOB_TTL_H = float(os.environ.get("PDF_TRANSLATOR_JOB_TTL_H", "24"))
AUTH_TOKEN = os.environ.get("PDF_TRANSLATOR_TOKEN")

app = FastAPI(title="PDF EN→JA Translator")
JOBS = {}  # job_id -> {status, stage, error, result, filename, created}
_slots = threading.Semaphore(MAX_PARALLEL)
_PERSIST_KEYS = ("status", "stage", "error", "result", "filename", "engine", "created")


def _job_file(job_id):
    return os.path.join(JOBS_DIR, job_id, "job.json")


def _persist(job_id):
    job = JOBS.get(job_id)
    if not job:
        return
    try:
        with open(_job_file(job_id), "w") as f:
            _json.dump({k: job.get(k) for k in _PERSIST_KEYS}, f)
    except OSError:
        pass


def _load_jobs():
    if not os.path.isdir(JOBS_DIR):
        return
    for jid in os.listdir(JOBS_DIR):
        jf = _job_file(jid)
        if os.path.exists(jf):
            try:
                d = _json.load(open(jf))
                # a job left 'running' by a crash is marked errored on reload
                if d.get("status") == "running":
                    d["status"] = "error"; d["error"] = "server restarted mid-job"
                JOBS[jid] = d
            except (OSError, ValueError):
                pass


def _sweep():
    """Delete job dirs older than the TTL."""
    cutoff = time.time() - JOB_TTL_H * 3600
    for jid, job in list(JOBS.items()):
        if job.get("created", 0) < cutoff:
            shutil.rmtree(os.path.join(JOBS_DIR, jid), ignore_errors=True)
            JOBS.pop(jid, None)


def _require_auth(request):
    if not AUTH_TOKEN:
        return
    sent = request.headers.get("authorization", "")
    if sent.startswith("Bearer "):
        sent = sent[7:]
    if not sent:
        sent = request.query_params.get("token", "")
    if sent != AUTH_TOKEN:
        raise HTTPException(401, "invalid or missing token")

# The orchestrator prints "  [<role>] <detail>" per step.
ROLE_LABELS = {
    "producer": "PDF製作者: レイアウト解析中",
    "translator": "翻訳者: 翻訳中",
    "editor": "編集者: 配置中",
    "qa": "確認者: 検査中",
    "done": "完了処理中",
}
_STAGE_RE = re.compile(r"\[(producer|translator|editor|qa|done)\]\s*(.*)")


def _stage_label(line):
    m = _STAGE_RE.search(line)
    if not m:
        return None
    role, detail = m.group(1), m.group(2).strip()
    base = ROLE_LABELS.get(role, role)
    return f"{base}（{detail}）" if detail else base


def _run_job(job_id):
    job = JOBS[job_id]
    out_dir = os.path.join(JOBS_DIR, job_id)
    env = dict(os.environ, PDF_TRANSLATOR_OUT=out_dir)
    # 4-role orchestrator: producer -> translator -> editor -> qa (retry loop)
    cmd = [sys.executable, "-m", "roles.orchestrator",
           os.path.join(out_dir, "input.pdf"), "--name", "doc",
           "--engine", job["engine"]]
    tail = []
    with _slots:
        job["status"] = "running"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                cwd=os.path.join(ROOT, "src"))
        for line in proc.stdout:
            line = line.rstrip()
            tail = (tail + [line])[-8:]
            label = _stage_label(line)
            if label:
                job["stage"] = label
        proc.wait()
    result = os.path.join(out_dir, "doc_ja.pdf")
    if proc.returncode == 0 and os.path.exists(result):
        job["status"] = "done"
        job["stage"] = "完了"
        job["result"] = result
    else:
        job["status"] = "error"
        # surface the pipeline's own message (e.g. encrypted / scanned PDF)
        err = next((l for l in reversed(tail) if l.startswith("error:")), None)
        job["error"] = (err or "\n".join(tail))[:500]
    _persist(job_id)


ENGINE_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


@app.post("/api/jobs")
async def create_job(request: Request, file: UploadFile = File(...),
                     engine: str = Form("google")):
    _require_auth(request)
    _sweep()
    if engine not in ("mock", "google", "gemini", "anthropic", "openai"):
        raise HTTPException(400, "unknown engine")
    if engine in ENGINE_KEYS and not os.environ.get(ENGINE_KEYS[engine]):
        raise HTTPException(400, f"{ENGINE_KEYS[engine]} is not set on the server")
    if engine == "gemini" and not (os.environ.get("GEMINI_API_KEY")
                                   or os.environ.get("GOOGLE_API_KEY")):
        raise HTTPException(400, "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set "
                                 "on the server")
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD_MB}MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "not a PDF file")
    job_id = uuid.uuid4().hex[:12]
    out_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "input.pdf"), "wb") as f:
        f.write(data)
    JOBS[job_id] = {"status": "queued", "stage": "待機中", "error": None,
                    "result": None, "filename": file.filename or "document.pdf",
                    "engine": engine, "created": time.time()}
    _persist(job_id)
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    _require_auth(request)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {"id": job_id, "status": job["status"], "stage": job["stage"],
            "error": job["error"]}


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str, request: Request):
    _require_auth(request)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] != "done":
        raise HTTPException(409, "job not finished")
    result = job.get("result") or os.path.join(JOBS_DIR, job_id, "doc_ja.pdf")
    if not os.path.exists(result):
        raise HTTPException(410, "output expired (job directory was swept)")
    base = os.path.splitext(os.path.basename(job["filename"]))[0]
    return FileResponse(result, media_type="application/pdf",
                        filename=f"{base}_ja.pdf")


@app.on_event("startup")
def _startup():
    _load_jobs()
    _sweep()

    def _loop():
        while True:
            time.sleep(3600)
            try:
                _sweep()
            except Exception:
                pass
    threading.Thread(target=_loop, daemon=True).start()


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF 英日翻訳</title>
<style>
:root{color-scheme:light dark}
body{font-family:'Noto Sans CJK JP','Hiragino Sans',sans-serif;max-width:640px;
     margin:48px auto;padding:0 20px;line-height:1.7}
h1{font-size:22px}
.card{border:1px solid #8884;border-radius:10px;padding:24px;margin-top:16px}
label{display:block;margin:12px 0 4px;font-size:14px}
input[type=file],select{width:100%;padding:8px;border:1px solid #8886;border-radius:6px}
button{margin-top:16px;padding:10px 22px;border:0;border-radius:6px;
       background:#2563eb;color:#fff;font-size:15px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
#status{margin-top:16px;font-size:14px}
.err{color:#dc2626;white-space:pre-wrap}
a.dl{display:inline-block;margin-top:8px;font-weight:bold}
small{color:#888}
</style></head><body>
<h1>PDF 英日翻訳(レイアウト保持)</h1>
<p>英語のPDF(論文・スライド)をアップロードすると、図表の位置を保持したまま
本文を日本語に置き換えたPDFを生成します。参考文献は英語のまま保持されます。</p>
<div class="card">
  <label>PDFファイル(最大 __MAX__MB)</label>
  <input type="file" id="file" accept="application/pdf">
  <label>翻訳エンジン</label>
  <select id="engine">
    <option value="google" selected>Google 翻訳(無料・APIキー不要)</option>
    <option value="gemini">Gemini / Google AI Studio(無料枠・要 APIキー)</option>
    <option value="anthropic">Anthropic API(要 ANTHROPIC_API_KEY・有料)</option>
    <option value="openai">OpenAI API(要 OPENAI_API_KEY・有料)</option>
    <option value="mock">mock(オフラインデモ・サンプル専用)</option>
  </select>
  <button id="go">翻訳する</button>
  <div id="status"></div>
</div>
<p><small>暗号化PDF・テキスト層のないスキャンPDFは未対応です。</small></p>
<script>
const $=id=>document.getElementById(id);
$('go').onclick=async()=>{
  const f=$('file').files[0];
  if(!f){$('status').textContent='ファイルを選択してください';return;}
  $('go').disabled=true;
  $('status').textContent='アップロード中…';
  const fd=new FormData();fd.append('file',f);fd.append('engine',$('engine').value);
  try{
    const r=await fetch('/api/jobs',{method:'POST',body:fd});
    if(!r.ok){throw new Error((await r.json()).detail||r.statusText);}
    const {id}=await r.json();
    while(true){
      await new Promise(s=>setTimeout(s,1500));
      const j=await (await fetch('/api/jobs/'+id)).json();
      if(j.status==='done'){
        $('status').innerHTML='完了 <a class="dl" href="/api/jobs/'+id+
          '/download">日本語PDFをダウンロード</a>';
        break;
      }
      if(j.status==='error'){
        $('status').innerHTML='<span class="err">失敗: '+j.error+'</span>';
        break;
      }
      $('status').textContent=j.stage+'…';
    }
  }catch(e){$('status').innerHTML='<span class="err">'+e.message+'</span>';}
  $('go').disabled=false;
};
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.replace("__MAX__", str(MAX_UPLOAD_MB))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
