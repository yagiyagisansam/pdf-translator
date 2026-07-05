#!/usr/bin/env python3
"""M5: Web app - upload an English PDF, get the layout-preserving Japanese PDF.

    pip install fastapi uvicorn python-multipart
    python src/webapp.py            # -> http://localhost:8000

Each job runs the CLI pipeline in a subprocess with its own output directory
(PDF_TRANSLATOR_OUT), so concurrent jobs never share font/analysis files and a
crash in one job cannot take the server down. Progress is streamed from the
pipeline's stage prints. Engine 'mock' works offline; 'anthropic'/'openai'
need the corresponding API key in the server's environment.
"""
import os, re, subprocess, sys, threading, time, uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_DIR = os.environ.get("PDF_TRANSLATOR_JOBS",
                          os.path.join(ROOT, "analysis", "webjobs"))
MAX_UPLOAD_MB = int(os.environ.get("PDF_TRANSLATOR_MAX_MB", "50"))
MAX_PARALLEL = int(os.environ.get("PDF_TRANSLATOR_WORKERS", "2"))

app = FastAPI(title="PDF EN→JA Translator")
JOBS = {}  # job_id -> {status, stage, error, result, filename, created}
_slots = threading.Semaphore(MAX_PARALLEL)

STAGE_LABELS = {
    "M1": "レイアウト解析中",
    "M2": "翻訳ユニット構築中",
    "translate": "翻訳中",
    "subset": "フォント生成中",
    "M3": "日本語PDF生成中",
}


def _stage_label(line):
    for key, label in STAGE_LABELS.items():
        if key in line:
            return label
    return None


def _run_job(job_id):
    job = JOBS[job_id]
    out_dir = os.path.join(JOBS_DIR, job_id)
    env = dict(os.environ, PDF_TRANSLATOR_OUT=out_dir)
    cmd = [sys.executable, os.path.join(ROOT, "src", "pipeline.py"),
           os.path.join(out_dir, "input.pdf"), "--name", "doc",
           "--engine", job["engine"]]
    tail = []
    with _slots:
        job["status"] = "running"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)
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


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), engine: str = Form("mock")):
    if engine not in ("mock", "google", "anthropic", "openai"):
        raise HTTPException(400, "unknown engine")
    if engine == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set on the server")
    if engine == "openai" and not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(400, "OPENAI_API_KEY is not set on the server")
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
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {"id": job_id, "status": job["status"], "stage": job["stage"],
            "error": job["error"]}


@app.get("/api/jobs/{job_id}/download")
def job_download(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job["status"] != "done":
        raise HTTPException(409, "job not finished")
    base = os.path.splitext(os.path.basename(job["filename"]))[0]
    return FileResponse(job["result"], media_type="application/pdf",
                        filename=f"{base}_ja.pdf")


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
