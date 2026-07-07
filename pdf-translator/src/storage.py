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

def _base_url(raw):
    """Normalize SUPABASE_URL to just scheme://host. Users often paste the "Data
    API / RESTful endpoint" (…supabase.co/rest/v1) or a URL with a trailing path;
    keeping any path makes /storage/v1/... resolve under PostgREST instead of the
    Storage service and fail with PGRST125 'Invalid path'. Strip it so only the
    project origin is used."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    from urllib.parse import urlsplit
    p = urlsplit(raw)
    return f"{p.scheme}://{p.netloc}" if p.netloc else raw.rstrip("/")


_URL = _base_url(os.environ.get("SUPABASE_URL"))
_KEY = (os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
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
    """Create the bucket (private) if it does not exist. Idempotent, best-effort.
    Returns (ok, status, message). Only 200 (created) or a duplicate-name error
    count as ok; every other response is logged with its body so a real failure
    (bad key scope, name rejected, ...) is visible instead of silently swallowed."""
    if not enabled():
        return False, 0, "disabled"
    try:
        r = requests.post(f"{_URL}/storage/v1/bucket",
                          headers=_headers({"Content-Type": "application/json"}),
                          data=json.dumps({"name": _BUCKET, "id": _BUCKET,
                                           "public": False}),
                          timeout=_TIMEOUT)
        body = r.text[:200]
        exists = r.status_code == 409 or (
            r.status_code == 400 and "exist" in body.lower())
        ok = r.status_code == 200 or exists
        if not ok:
            _log(f"ensure_bucket {r.status_code}: {body}")
        return ok, r.status_code, body
    except Exception as e:
        _log(f"ensure_bucket failed: {e}")
        return False, 0, str(e)


def _upload_bytes(path, data, content_type, _retry=True):
    r = requests.post(_obj_url(path),
                      headers=_headers({"Content-Type": content_type,
                                        "x-upsert": "true"}),
                      data=data, timeout=_TIMEOUT)
    if r.status_code in (200, 201):
        return True
    # bucket missing (auto-create failed at startup) -> create it now and retry
    if _retry and "bucket not found" in r.text.lower():
        ok, _, _ = ensure_bucket()
        if ok:
            return _upload_bytes(path, data, content_type, _retry=False)
    _log(f"upload {path} -> {r.status_code}: {r.text[:120]}")
    return False


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


def _key_role():
    """Identify the configured key WITHOUT exposing the secret. Supabase anon and
    service_role keys are BOTH JWTs starting with 'eyJ', so they look identical -
    the only way to tell them apart is the `role` claim inside. Only service_role
    bypasses RLS; an anon key hits 'row-level security policy' on upload. Returns
    e.g. 'service_role', 'anon', 'secret(sb_secret_)', or 'unknown'."""
    k = _KEY
    if not k:
        return None
    if k.startswith("sb_secret_"):
        return "secret(sb_secret_)"        # new-style secret key (elevated)
    if k.startswith("sb_publishable_"):
        return "publishable(sb_publishable_)"  # new-style public key (RLS applies)
    if k.startswith("eyJ") and k.count(".") == 2:
        try:
            import base64
            payload = k.split(".")[1]
            payload += "=" * (-len(payload) % 4)      # restore base64 padding
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return claims.get("role") or "jwt(no role claim)"
        except Exception:
            return "jwt(undecodable)"
    return "unknown"


def config_status():
    """Non-secret view of the storage configuration (for the diag endpoint)."""
    return {
        "requests_available": requests is not None,
        "url_set": bool(_URL),
        "url": _URL,                 # project origin only (not a secret)
        "key_set": bool(_KEY),
        "key_role": _key_role(),     # 'service_role' is required; 'anon' -> RLS error
        "bucket": _BUCKET,
        "enabled": enabled(),
    }


def selftest():
    """Round-trip a tiny object (create bucket -> upload -> read -> delete) to
    prove the configured URL + key + bucket actually work. Returns a dict of
    step results and, on failure, the HTTP status/message so a misconfigured key
    (anon instead of service_role, wrong URL, ...) is obvious. Never raises."""
    out = dict(config_status(), bucket_ok=False, upload_ok=False,
               read_ok=False, delete_ok=False, error=None)
    if not enabled():
        out["error"] = "storage not enabled (SUPABASE_URL / SUPABASE_KEY unset)"
        return out
    path = "_diag/selftest.txt"
    payload = b"ok"
    try:
        # ensure bucket (records the create status so a real failure is visible)
        b_ok, b_status, b_msg = ensure_bucket()
        out["bucket_ok"] = b_ok
        out["bucket_create_status"] = b_status
        out["bucket_create_msg"] = b_msg
        if not b_ok:
            out["error"] = (f"bucket create -> HTTP {b_status}: {b_msg[:160]}. "
                            f"If it says the bucket is missing, create a PRIVATE "
                            f"bucket named '{_BUCKET}' in Supabase -> Storage.")
            return out
        # upload
        r = requests.post(_obj_url(path),
                          headers=_headers({"Content-Type": "text/plain",
                                            "x-upsert": "true"}),
                          data=payload, timeout=_TIMEOUT)
        out["upload_ok"] = r.status_code in (200, 201)
        if not out["upload_ok"]:
            out["error"] = f"upload -> HTTP {r.status_code}: {r.text[:160]}"
            return out
        # read back
        r = requests.get(_obj_url(path), headers=_headers(), timeout=_TIMEOUT)
        out["read_ok"] = (r.status_code == 200 and r.content == payload)
        if not out["read_ok"]:
            out["error"] = f"read -> HTTP {r.status_code}"
        # cleanup (best effort)
        r = requests.request(
            "DELETE", f"{_URL}/storage/v1/object/{_BUCKET}",
            headers=_headers({"Content-Type": "application/json"}),
            data=json.dumps({"prefixes": [path]}), timeout=_TIMEOUT)
        out["delete_ok"] = r.status_code in (200, 204)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out
