import os, subprocess, shutil, json, tempfile, zipfile, urllib.request, glob, shlex
from typing import Dict, Any, Optional

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from supabase import create_client, Client

# ---------- Config toggles ----------
# Set to False once the smoke test has proven the pipeline works.
SMOKE_TEST = True

# Built-in printer used for the smoke test (exists in stock PrusaSlicer)
SMOKE_TEST_PRINTER = "Original Prusa SL1S SPEED"

# ---------- Env (no crash on import) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")

# Where the AppImage symlink was installed by your Dockerfile (extracted AppRun)
PRUSA_APPIMAGE = "/usr/local/bin/prusaslicer"

# Acceptable model file extensions PrusaSlicer can ingest headlessly here
ALLOWED_MODEL_EXTS = {".stl", ".3mf", ".obj", ".amf"}

app = FastAPI()

# Lazily create Supabase client (so missing envs don't crash import)
def _supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ---------- Utilities ----------
def sh(cmd: str, cwd: Optional[str] = None, env: Optional[dict] = None):
    """Run a shell command and capture output."""
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def update_job(job_id: str, **fields):
    """Best-effort DB update (never raise)."""
    try:
        _supabase().table("slice_jobs").update(fields).eq("id", job_id).execute()
    except Exception as e:
        print("DB update failed:", e)

def signed_download(bucket: str, path: str, dest: str):
    """Generate a signed URL and download to dest (handles full/partial URLs)."""
    if not SUPABASE_URL.startswith("https://"):
        raise RuntimeError(f"SUPABASE_URL is missing or invalid: {SUPABASE_URL!r}")

    res = _supabase().storage.from_(bucket).create_signed_url(path, 3600)
    signed = res.get("signedURL") or res.get("signed_url")
    if not signed:
        raise RuntimeError(f"create_signed_url returned no URL for {bucket}:{path} → {res}")

    url = signed if signed.startswith("http") else SUPABASE_URL.rstrip("/") + signed
    if not url.startswith("https://"):
        raise RuntimeError(f"Bad signed URL: {url}")

    print(f"Downloading model from: {url}")
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        f.write(r.read())

def upload_file(bucket: str, path: str, local_path: str, content_type: str):
    with open(local_path, "rb") as f:
        _supabase().storage.from_(bucket).upload(path, f, {"content-type": content_type, "upsert": True})

def find_layers(base_dir: str) -> Optional[str]:
    """Find the directory that actually contains PNG layer files."""
    # Quick common paths
    for candidate in (os.path.join(base_dir, "slices"), os.path.join(base_dir, "sla")):
        if os.path.isdir(candidate) and glob.glob(os.path.join(candidate, "*.png")):
            return candidate
    # Generic recursive: choose dir with the most PNGs
    best_dir, best_count = None, 0
    for root, _, files in os.walk(base_dir):
        count = sum(1 for f in files if f.lower().endswith(".png"))
        if count > best_count:
            best_dir, best_count = root, count
    return best_dir if best_count > 0 else None

# ---------- PrusaSlicer runner (AppImage, headless) ----------
def run_prusaslicer_headless(args: list[str]) -> tuple[int, str, str]:
    """
    Run PrusaSlicer AppImage headlessly with xvfb-run.
    Returns (rc, combined_output, rendered_command_str) for better error logs.
    """
    env = os.environ.copy()
    env.setdefault("NO_AT_BRIDGE", "1")  # avoid a11y D-Bus probing

    base = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", PRUSA_APPIMAGE]
    cmd_list = base + args
    cmd_str = " ".join(shlex.quote(x) for x in cmd_list)
    rc, log = sh(cmd_str, env=env)
    return rc, log, cmd_str

# ---------- Health/Readiness ----------
@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/ready")
def ready():
    return {
        "ok": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and WORKER_TOKEN),
        "has_SUPABASE_URL": bool(SUPABASE_URL),
        "has_SERVICE_ROLE": bool(SUPABASE_SERVICE_ROLE_KEY),
        "has_WORKER_TOKEN": bool(WORKER_TOKEN),
    }

# ---------- Main job endpoint ----------
@app.post("/jobs")
def start_job(payload: Dict[str, Any], authorization: str = Header(None)):
    """
    Always return 200 with {"ok": True/False, "error": "..."}.
    Write progress/errors to slice_jobs so the UI never sees opaque 500s.
    """
    try:
        # Env sanity for this revision
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            return JSONResponse({"ok": False, "error": "missing_env SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY"}, status_code=200)
        if not WORKER_TOKEN:
            return JSONResponse({"ok": False, "error": "missing_env WORKER_TOKEN"}, status_code=200)

        # Auth
        if authorization != f"Bearer {WORKER_TOKEN}":
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=200)

        job_id = payload.get("job_id")
        if not job_id:
            return JSONResponse({"ok": False, "error": "missing job_id"}, status_code=200)

        s = _supabase()

        # Fetch job
        job_q = s.table("slice_jobs").select("*").eq("id", job_id).single().execute()
        job = job_q.data
        if not job:
            return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=200)

        # Mark processing ASAP
        update_job(job_id, status="processing", error=None)

        # Workspace
        wd = tempfile.mkdtemp(prefix=f"job_{job_id}_")
        try:
            # 1) Input model (spec is "bucket:path/in/bucket.ext")
            in_spec = job["input_path"]
            if ":" not in in_spec:
                update_job(job_id, status="failed", error=f"bad_input_path_format (expected 'bucket:path'): {in_spec}")
                return {"ok": False, "error": "bad_input_path_format"}
            bucket, path_in_bucket = in_spec.split(":", 1)

            # Preserve original filename & extension — PrusaSlicer relies on the extension
            base_name = os.path.basename(path_in_bucket.split("?")[0])
            _, ext = os.path.splitext(base_name)
            ext = ext.lower()
            if ext not in ALLOWED_MODEL_EXTS:
                update_job(job_id, status="failed",
                           error=f"unsupported_model_extension: {ext or '(none)'}; "
                                 f"expected one of {sorted(ALLOWED_MODEL_EXTS)}; "
                                 f"path was: {path_in_bucket}")
                return {"ok": False, "error": "unsupported_model_extension"}

            input_model = os.path.join(wd, base_name)
            signed_download(bucket, path_in_bucket, input_model)

            out_dir = os.path.join(wd, "out")
            os.makedirs(out_dir, exist_ok=True)

            # 2) Preflight: ensure PrusaSlicer binary is runnable; don't fail hard on rc alone
            rc0, log0, cmd0 = run_prusaslicer_headless(["--version"])
            if ("PrusaSlicer" not in log0) and (rc0 != 0):
                update_job(job_id, status="failed",
                           error=f"prusaslicer_boot_failed rc={rc0}\nCMD: {cmd0}\n{log0[-4000:]}")
                return {"ok": False, "error": "prusaslicer_boot_failed"}

            # =============================
            # 3) SMOKE TEST (built-in printer)
            # =============================
            if SMOKE_TEST:
                conf_dir = os.path.join(wd, "ps_config_smoke")
                os.makedirs(conf_dir, exist_ok=True)

                base_common = [
                    "--no-gui",
                    "--export-sla",
                    "--datadir", conf_dir,
                    "--loglevel", "3",
                ]
                smoke_args = base_common + ["--printer-profile", SMOKE_TEST_PRINTER, input_model]

                rc_smoke, log_smoke, cmd_smoke = run_prusaslicer_headless(smoke_args)
                if rc_smoke != 0:
                    update_job(job_id, status="failed",
                               error=f"prusaslicer_failed (builtin smoke test) rc={rc_smoke}\n"
                                     f"CMD: {cmd_smoke}\n{log_smoke[-3000:]}")
                    return {"ok": False, "error": "prusaslicer_failed"}

                # Locate layers from smoke test
                slices_dir = find_layers(out_dir)
                if not slices_dir:
                    update_job(job_id, status="failed",
                               error=f"no_slices_found (smoke test) in {out_dir}.\nCMD: {cmd_smoke}\n"
                                     f"Prusa log tail:\n{log_smoke[-3000:]}")
                    return {"ok": False, "error": "no_slices_found"}

                # Zip layers for inspection
                zip_path = os.path.join(out_dir, "layers_smoke.zip")
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                    for fn in sorted(glob.glob(os.path.join(slices_dir, "*.png"))):
                        z.write(fn, arcname=os.path.basename(fn))

                # Upload just the smoke test slices for now
                job_prefix = str(job_id)
                with open(zip_path, "rb") as f:
                    _supabase().storage.from_("slices").upload(
                        f"{job_prefix}/layers_smoke.zip", f,
                        {"content-type": "application/zip", "upsert": True}
                    )

                report = {"layers": len(glob.glob(os.path.join(slices_dir, "*.png"))),
                          "note": "Smoke test used built-in printer: " + SMOKE_TEST_PRINTER}
                update_job(
                    job_id,
                    status="succeeded_smoke",
                    report=report,
                    output_native_path=None,
                    output_project_path=None,
                    output_slices_zip_path=f"slices:{job_prefix}/layers_smoke.zip",
                    error=None,
                )
                return {"ok": True, "smoke_test": True}

            # =============================
            # If SMOKE_TEST is False, your real slicing flow would go here:
            #  - Load your preset bundle (printer + SLA print + material)
            #  - Call with --printer-profile/--material-profile/--print-profile names that exist
            #  - Then package with UVtools, upload, finalize
            # =============================

            update_job(job_id, status="failed",
                       error="SMOKE_TEST is disabled and no real slicing flow was provided in this build.")
            return {"ok": False, "error": "not_implemented"}

        except Exception as e:
            update_job(job_id, status="failed", error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}

        finally:
            shutil.rmtree(wd, ignore_errors=True)

    except Exception as e:
        # Never bubble 500s back to the function; record and return 200
        return JSONResponse({"ok": False, "error": f"fatal: {e}"}, status_code=200)
