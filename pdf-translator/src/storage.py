#!/usr/bin/env python3
"""Optional Supabase Storage backend so finished translations survive a redeploy.

Render's free plan has an EPHEMERAL disk: it survives sleep/wake but is wiped on
every redeploy. The web app already persists job metadata + the output PDF to the
local job dir; this module additionally mirrors each finished job to a Supabase
Storage bucket (free tier, 1 GB) so it can be restored after the disk is wiped.

Enable it entirely from a phone browser by setting three env vars on the host
(e.g. Render -> Environment):

    SUPABASE_URL     https://<project-ref>.supabase.co
    SUPABASE_KEY     the project's service_role key (Settings -> API)
    SUPABASE_BUCKET  bucket name (optional, default "translations")

Leave them unset and the app behaves exactly as before (local-disk only). The
service_role key bypasses row-level security, so the bucket can stay PRIVATE and
is only ever reached server-side, behind the app's own password.

Layout in the bucket, one folder per job:
    <job_id>/job.json     the persisted metadata
    <job_id>/doc_ja.pdf   the translated PDF

Every call is best-effort: any network/credential error is swallowed and logged
to stderr, so storage problems can never break a translation or take the app down.
"""
import io
import json
import os
import sys

try:
    import requests
except Exception:                     # pragma: no cover - requests ships with deep-translator
    requests = None

_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY") or ""
_BUCKET = os.environ.get("SUPABASE_BUCKET", "translations")
_TIMEOUT = float(os.environ.get("SUPABASE_TIMEOUT", "30"))
PDF_NAME = "doc_ja.pdf"
META_NAME = "job.json"


def enabled():
    return bool(requests and _URL and _KEY)


def _headers(extra=None):
    h = {"Authorization": f"Bearer {_KEY}", "apikey": _KEY}
    if extra:
        h.update(extra)
    return h


def _log(msg):
    print(f"[storage] {msg}", file=sys.stderr)


def _obj_url(path):
    return f"{_URL}/storage/v1/object/{_BUCKET}/{path}"


def ensure_bucket():
    """Create the bucket (private) if it does not exist. Idempotent, best-effort."""
    if not enabled():
        return
    try:
        r = requests.post(f"{_URL}/storage/v1/bucket",
                          headers=_headers({"Content-Type": "application/json"}),
                          data=json.dumps({"name": _BUCKET, "id": _BUCKET,
                                           "public": False}),
                          timeout=_TIMEOUT)
        # 200 = created, 400/409 = already exists -> both fine
        if r.status_code not in (200, 400, 409):
            _log(f"ensure_bucket unexpected {r.status_code}: {r.text[:120]}")
    except Exception as e:
        _log(f"ensure_bucket failed: {e}")


def _upload_bytes(path, data, content_type):
    r = requests.post(_obj_url(path),
                      headers=_headers({"Content-Type": content_type,
                                        "x-upsert": "true"}),
                      data=data, timeout=_TIMEOUT)
    if r.status_code not in (200, 201):
        _log(f"upload {path} -> {r.status_code}: {r.text[:120]}")
        return False
    return True


def upload_job(job_id, pdf_path, meta):
    """Mirror a finished job (its PDF + metadata) to the bucket. Best-effort."""
    if not enabled():
        return False
    try:
        with open(pdf_path, "rb") as f:
            if not _upload_bytes(f"{job_id}/{PDF_NAME}", f.read(),
                                 "application/pdf"):
                return False
        _upload_bytes(f"{job_id}/{META_NAME}",
                      json.dumps(meta).encode("utf-8"), "application/json")
        return True
    except Exception as e:
        _log(f"upload_job {job_id} failed: {e}")
        return False


def _list_prefixes():
    """Return the job-id folder names in the bucket."""
    r = requests.post(f"{_URL}/storage/v1/object/list/{_BUCKET}",
                      headers=_headers({"Content-Type": "application/json"}),
                      data=json.dumps({"prefix": "", "limit": 1000,
                                       "sortBy": {"column": "name",
                                                  "order": "asc"}}),
                      timeout=_TIMEOUT)
    r.raise_for_status()
    out = []
    for entry in r.json():
        name = entry.get("name")
        # folders come back with a null id; keep only those (the job dirs)
        if name and entry.get("id") is None:
            out.append(name)
    return out


def load_jobs():
    """Return {job_id: meta} reconstructed from the bucket, for restoring the job
    list after a redeploy wiped local disk. Best-effort; returns {} on any error."""
    if not enabled():
        return {}
    jobs = {}
    try:
        for jid in _list_prefixes():
            try:
                r = requests.get(_obj_url(f"{jid}/{META_NAME}"),
                                 headers=_headers(), timeout=_TIMEOUT)
                if r.status_code == 200:
                    jobs[jid] = r.json()
            except Exception as e:
                _log(f"load meta {jid} failed: {e}")
    except Exception as e:
        _log(f"load_jobs failed: {e}")
    return jobs


def fetch_pdf(job_id):
    """Return the translated PDF bytes for a job, or None. Best-effort."""
    if not enabled():
        return None
    try:
        r = requests.get(_obj_url(f"{job_id}/{PDF_NAME}"),
                         headers=_headers(), timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.content
        _log(f"fetch_pdf {job_id} -> {r.status_code}")
    except Exception as e:
        _log(f"fetch_pdf {job_id} failed: {e}")
    return None


def delete_job(job_id):
    """Remove a job's objects from the bucket (mirrors the local TTL sweep)."""
    if not enabled():
        return
    try:
        requests.request(
            "DELETE", f"{_URL}/storage/v1/object/{_BUCKET}",
            headers=_headers({"Content-Type": "application/json"}),
            data=json.dumps({"prefixes": [f"{job_id}/{PDF_NAME}",
                                          f"{job_id}/{META_NAME}"]}),
            timeout=_TIMEOUT)
    except Exception as e:
        _log(f"delete_job {job_id} failed: {e}")
