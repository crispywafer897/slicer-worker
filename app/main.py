import os, subprocess, shutil, json, tempfile, zipfile, urllib.request
from fastapi import FastAPI, Header, HTTPException
from supabase import create_client, Client
from typing import Dict, Any

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WORKER_TOKEN = os.environ["WORKER_TOKEN"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = FastAPI()

def sh(cmd: str, cwd=None):
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def signed_download(bucket: str, path: str, dest: str):
    if not SUPABASE_URL or not SUPABASE_URL.startswith("https://"):
        raise RuntimeError(f"SUPABASE_URL is missing or invalid: {SUPABASE_URL!r}")

    # Ask Supabase for a signed URL (v2 SDK returns dict)
    res = supabase.storage.from_(bucket).create_signed_url(path, 3600)

    # Handle both possible keys
    signed = res.get("signedURL") or res.get("signed_url")
    if not signed:
        raise RuntimeError(f"create_signed_url returned no URL for {bucket}:{path} → {res}")

    # If the SDK returned a full URL, use it as-is; otherwise prefix with project URL
    if signed.startswith("http://") or signed.startswith("https://"):
        url = signed
    else:
        # signed typically begins with "/storage/v1/object/sign/..."
        url = SUPABASE_URL.rstrip("/") + signed

    # Optional: quick sanity guard
    if not url.startswith("https://"):
        raise RuntimeError(f"Bad signed URL: {url}")

    # Download
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        f.write(r.read())

def upload_file(bucket: str, path: str, local_path: str, content_type: str):
    with open(local_path, "rb") as f:
        supabase.storage.from_(bucket).upload(path, f, {"content-type": content_type, "upsert": True})

@app.post("/jobs")
def start_job(payload: Dict[str, Any], authorization: str = Header(None)):
    if authorization != f"Bearer {WORKER_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")

    job_id = payload.get("job_id")
    if not job_id:
        raise HTTPException(status_code=400, detail="missing job_id")

    job_q = supabase.table("slice_jobs").select("*").eq("id", job_id).single().execute()
    if not job_q.data:
        raise HTTPException(status_code=404, detail="job not found")
    job = job_q.data

    supabase.table("slice_jobs").update({"status":"processing"}).eq("id", job_id).execute()

    # Create temp workspace
    import uuid, os
    wd = os.path.join("/tmp", f"job_{uuid.uuid4().hex}")
    os.makedirs(wd, exist_ok=True)

    try:
        # Download input model from Supabase Storage (format 'bucket:path')
        in_spec = job["input_path"]                  # e.g. 'models:user-123/foo/print_ready.3mf'
        bucket, path = in_spec.split(":", 1)
        input_model = os.path.join(wd, "model.3mf")
        signed_download(bucket, path, input_model)

        # Choose profiles based on printer_id / print_profile
        printer_id = job["printer_id"]               # e.g. 'elegoo_mars3'
        print_profile = job["print_profile"]         # e.g. '0.05mm_standard'
        printer_json   = f"/profiles/printers/{printer_id}.json"
        prusa_ini      = f"/profiles/resin_presets/generic_0.05mm.ini" if print_profile=="0.05mm_standard" else f"/profiles/resin_presets/{print_profile}.ini"
        uvtools_params = f"/profiles/uvtools_params/{printer_id}_std_resin.json"

        out_dir = os.path.join(wd, "out")
        os.makedirs(out_dir, exist_ok=True)

        # 1) Slice to PNG stack + .3mf with PrusaSlicer (headless SLA)
        # NOTE: exact flags can vary by version; use --help if needed. :contentReference[oaicite:1]{index=1}
        cmd1 = f'prusaslicer --no-gui --export-sla --export-3mf --load "{prusa_ini}" --output "{out_dir}" "{input_model}"'
        rc1, log1 = sh(cmd1)

        # 2) Package PNGs to native resin file with UVtools (ChiTu formats)
        fmt = json.load(open(printer_json))["format"]     # e.g. "ctb_v3"
        native_ext = "ctb" if "ctb" in fmt else "cbddlp"
        slices_dir = os.path.join(out_dir, "slices")
        native_path = os.path.join(out_dir, f"print.{native_ext}")
        cmd2 = f'uvtools-cli pack --format {fmt} --printer-profile "{printer_json}" --slices "{slices_dir}" --params "{uvtools_params}" --out "{native_path}"'
        rc2, log2 = sh(cmd2)

        # 3) Zip slices for debugging
        zip_path = os.path.join(out_dir, "layers.zip")
        import zipfile, os
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for fn in sorted(os.listdir(slices_dir)):
                z.write(os.path.join(slices_dir, fn), arcname=fn)

        # 4) Upload outputs back to Supabase Storage (same “bucket:path” style)
        job_prefix = job_id
        native_store = f"{job_prefix}/print.{native_ext}"
        project_3mf  = next((f for f in os.listdir(out_dir) if f.endswith(".3mf")), None)

        upload_file("native",   native_store, native_path, "application/octet-stream")
        if project_3mf:
            upload_file("projects", f"{job_prefix}/{project_3mf}", os.path.join(out_dir, project_3mf), "model/3mf")
        upload_file("slices",   f"{job_prefix}/layers.zip", zip_path, "application/zip")

        report = {"layers": len(os.listdir(slices_dir))}
        error_txt = None if (rc1==0 and rc2==0) else "slicer failed, see logs"

        # 5) Update the DB row
        supabase.table("slice_jobs").update({
            "status": "succeeded" if rc1==0 and rc2==0 else "failed",
            "output_native_path": f"native:{native_store}",
            "output_project_path": f"projects:{job_prefix}/{project_3mf}" if project_3mf else None,
            "output_slices_zip_path": f"slices:{job_prefix}/layers.zip",
            "report": report,
            "error": error_txt
        }).eq("id", job_id).execute()

        return {"ok": rc1==0 and rc2==0}
    finally:
        shutil.rmtree(wd, ignore_errors=True)
