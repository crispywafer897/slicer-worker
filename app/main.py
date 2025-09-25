import os, subprocess, shutil, json, tempfile, zipfile, urllib.request, glob
from typing import Dict, Any, Optional

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from supabase import create_client, Client

# --- Env ---
SUPABASE_URL = os.environ["SUPABASE_URL"]                     # e.g. https://<ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WORKER_TOKEN = os.environ["WORKER_TOKEN"]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = FastAPI()


# ---------- Utilities ----------
def sh(cmd: str, cwd: Optional[str] = None):
    """Run a shell command and capture output."""
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def update_job(job_id: str, **fields):
    """Best-effort DB update (never raise)."""
    try:
        supabase.table("slice_jobs").update(fields).eq("id", job_id).execute()
    except Exception as e:
        print("DB update failed:", e)

def signed_download(bucket: str, path: str, dest: str):
    """Generate a signed URL and download to dest (handles full/partial URLs)."""
    if not SUPABASE_URL or not SUPABASE_URL.startswith("https://"):
        raise RuntimeError(f"SUPABASE_URL is missing or invalid: {SUPABASE_URL!r}")

    res = supabase.storage.from_(bucket).create_signed_url(path, 3600)
    signed = res.get("signedURL") or res.get("signed_url")
    if not signed:
        raise RuntimeError(f"create_signed_url returned no URL for {bucket}:{path} → {res}")

    if signed.startswith("http://") or signed.startswith("https://"):
        url = signed
    else:
        url = SUPABASE_URL.rstrip("/") + signed  # signed usually starts with /storage/...

    if not url.startswith("https://"):
        raise RuntimeError(f"Bad signed URL: {url}")

    print(f"Downloading model from: {url}")
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        f.write(r.read())

def upload_file(bucket: str, path: str, local_path: str, content_type: str):
    with open(local_path, "rb") as f:
        supabase.storage.from_(bucket).upload(path, f, {"content-type": content_type, "upsert": True})

def find_layers(base_dir: str) -> Optional[str]:
    """Find the directory that actually contains PNG layer files."""
    # Fast checks
    for candidate in (os.path.join(base_dir, "slices"), os.path.join(base_dir, "sla")):
        if os.path.isdir(candidate) and glob.glob(os.path.join(candidate, "*.png")):
            return candidate
    # Recursive: pick dir with most PNGs
    best_dir, best_count = None, 0
    for root, _, files in os.walk(base_dir):
        count = sum(1 for f in files if f.lower().endswith(".png"))
        if count > best_count:
            best_dir, best_count = root, count
    return best_dir if best_count > 0 else None


# ---------- Health ----------
@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- Main job endpoint ----------
@app.post("/jobs")
def start_job(payload: Dict[str, Any], authorization: str = Header(None)):
    """
    Always respond 200 with {"ok": True/False, "error": "..."}.
    Write progress/errors to slice_jobs so the UI never sees opaque 500s.
    """
    try:
        # Auth
        if authorization != f"Bearer {WORKER_TOKEN}":
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=200)

        job_id = payload.get("job_id")
        if not job_id:
            return JSONResponse({"ok": False, "error": "missing job_id"}, status_code=200)

        # Fetch job
        job_q = supabase.table("slice_jobs").select("*").eq("id", job_id).single().execute()
        job = job_q.data
        if not job:
            return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=200)

        # Mark processing ASAP
        update_job(job_id, status="processing", error=None)

        # Workspace
        wd = tempfile.mkdtemp(prefix=f"job_{job_id}_")
        try:
            # 1) Input model
            in_spec = job["input_path"]  # "bucket:path/inside.ext"
            if ":" not in in_spec:
                raise ValueError(f"input_path must be 'bucket:path', got: {in_spec}")
            bucket, path = in_spec.split(":", 1)
            input_model = os.path.join(wd, "model.3mf")  # fine for STL too; just a filename
            signed_download(bucket, path, input_model)

            # 2) Profiles
            printer_id = job["printer_id"]
            print_profile = job["print_profile"]
            printer_json   = f"/profiles/printers/{printer_id}.json"
            prusa_ini      = f"/profiles/resin_presets/{'generic_0.05mm.ini' if print_profile == '0.05mm_standard' else print_profile + '.ini'}"
            uvtools_params = f"/profiles/uvtools_params/{printer_id}_std_resin.json"

            for req_path, label in [(printer_json, "printer profile"),
                                    (prusa_ini, "resin preset"),
                                    (uvtools_params, "uvtools params")]:
                if not os.path.exists(req_path):
                    raise FileNotFoundError(f"missing {label}: {req_path}")

            out_dir = os.path.join(wd, "out")
            os.makedirs(out_dir, exist_ok=True)

            # 3) Slice with PrusaSlicer (Flatpak + xvfb). Try explicit SLA output dir, then fallback.
            slices_target = os.path.join(out_dir, "slices")
            os.makedirs(slices_target, exist_ok=True)

            cmd1_options = [
                # Preferred: direct SLA output (may not exist on older builds)
                (
                    'xvfb-run -a -s "-screen 0 1024x768x24" '
                    'flatpak run --command=PrusaSlicer com.prusa3d.PrusaSlicer '
                    f'--no-gui --export-sla --export-3mf --load "{prusa_ini}" '
                    f'--output "{out_dir}" --sla-output "{slices_target}" "{input_model}"'
                ),
                # Fallback: let Prusa decide folder; we'll discover it
                (
                    'xvfb-run -a -s "-screen 0 1024x768x24" '
                    'flatpak run --command=PrusaSlicer com.prusa3d.PrusaSlicer '
                    f'--no-gui --export-sla --export-3mf --load "{prusa_ini}" '
                    f'--output "{out_dir}" "{input_model}"'
                ),
            ]

            rc1, log1 = -1, ""
            for cmd in cmd1_options:
                rc1, log1 = sh(cmd)
                if rc1 == 0:
                    break

            if rc1 != 0:
                # Surface full Prusa log for quick diagnosis
                update_job(job_id, status="failed", error=f"prusaslicer_failed rc={rc1}\n{log1[-4000:]}")
                return {"ok": False, "error": "prusaslicer_failed"}

            # 3b) Find where PNG layers actually went
            slices_dir = find_layers(out_dir)
            if not slices_dir:
                update_job(job_id, status="failed",
                           error=f"no_slices_found in {out_dir}. Prusa log tail:\n{log1[-4000:]}")
                return {"ok": False, "error": "no_slices_found"}

            # 4) Package with UVtools
            fmt = json.load(open(printer_json))["format"]  # e.g., "ctb_v3"
            native_ext = "ctb" if "ctb" in fmt else "cbddlp"
            native_path = os.path.join(out_dir, f"print.{native_ext}")

            cmd2 = (
                f'uvtools-cli pack --format {fmt} '
                f'--printer-profile "{printer_json}" '
                f'--slices "{slices_dir}" '
                f'--params "{uvtools_params}" '
                f'--out "{native_path}"'
            )
            rc2, log2 = sh(cmd2)
            if rc2 != 0:
                update_job(job_id, status="failed", error=f"uvtools_failed rc={rc2}\n{log2[-4000:]}")
                return {"ok": False, "error": "uvtools_failed"}

            # 5) Zip layers (for download/debug)
            zip_path = os.path.join(out_dir, "layers.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for fn in sorted(glob.glob(os.path.join(slices_dir, "*.png"))):
                    z.write(fn, arcname=os.path.basename(fn))

            # 6) Upload outputs
            job_prefix = str(job_id)

            def up(bucket, path, local, ctype):
                with open(local, "rb") as f:
                    supabase.storage.from_(bucket).upload(path, f, {"content-type": ctype, "upsert": True})

            up("native",   f"{job_prefix}/print.{native_ext}", native_path, "application/octet-stream")
            proj = next((f for f in os.listdir(out_dir) if f.endswith(".3mf")), None)
            if proj:
                up("projects", f"{job_prefix}/{proj}", os.path.join(out_dir, proj), "model/3mf")
            up("slices",   f"{job_prefix}/layers.zip", zip_path, "application/zip")

            # 7) Finalize
            report = {"layers": len(glob.glob(os.path.join(slices_dir, "*.png")))}
            update_job(
                job_id,
                status="succeeded",
                report=report,
                output_native_path=f"native:{job_prefix}/print.{native_ext}",
                output_project_path=(f"projects:{job_prefix}/{proj}" if proj else None),
                output_slices_zip_path=f"slices:{job_prefix}/layers.zip",
                error=None,
            )
            return {"ok": True}

        except Exception as e:
            update_job(job_id, status="failed", error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}

        finally:
            shutil.rmtree(wd, ignore_errors=True)

    except Exception as e:
        # Never bubble 500s back to the function; record and return 200
        return JSONResponse({"ok": False, "error": f"fatal: {e}"}, status_code=200)
