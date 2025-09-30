import os, subprocess, shutil, json, tempfile, zipfile, urllib.request, glob, shlex, hashlib, textwrap, re
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from supabase import create_client, Client

# ---------- Env (no crash on import) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")

# Storage bucket that holds bundles/ and uvtools_params/
STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "slicer-presets")

# Ephemeral cache dir for downloaded bundles/params on warm instances
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/preset_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Where the AppImage symlink was installed by your Dockerfile (extracted AppRun)
PRUSA_APPIMAGE = "/usr/local/bin/prusaslicer"

# Acceptable model file extensions PrusaSlicer can ingest headlessly
ALLOWED_MODEL_EXTS = {".stl", ".3mf", ".obj", ".amf"}

# Allow-list of UVtools param keys that jobs may override safely
ALLOWLIST_OVERRIDE_KEYS = {
    "layer_height_mm", "bottom_layers", "bottom_exposure_s", "normal_exposure_s",
    "light_off_delay_s", "lift_height_mm", "lift_speed_mm_s", "retract_speed_mm_s",
    "anti_aliasing"
}

app = FastAPI()

# Lazily create Supabase client (so missing envs don't crash import)
def _supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ---------- Utilities ----------
def sh(cmd: str, cwd: Optional[str] = None, env: Optional[dict] = None) -> Tuple[int, str]:
    """Run a shell command and capture combined stdout+stderr."""
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

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _download_storage(object_path: str, dest: Path):
    """Download a private object from Supabase Storage to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = _supabase().storage.from_(STORAGE_BUCKET).download(object_path)
    dest.write_bytes(data)

def resolve_preset(printer_id: str) -> Dict[str, Any]:
    """
    Fetch a printer preset row from DB, and ensure bundle/params are cached locally.
    Table: public.printer_presets
    """
    res = _supabase().table("printer_presets").select("*").eq("id", printer_id).single().execute()
    row = res.data
    if not row:
        raise HTTPException(status_code=400, detail=f"Unknown printer_id={printer_id}")

    bundle_cached = CACHE_DIR / row["bundle_path"].replace("/", "__")
    params_cached = CACHE_DIR / row["uvtools_params_path"].replace("/", "__")

    if not bundle_cached.exists():
        _download_storage(row["bundle_path"], bundle_cached)
    if not params_cached.exists():
        _download_storage(row["uvtools_params_path"], params_cached)

    # Optional integrity check
    if row.get("bundle_sha256"):
        actual = _sha256(bundle_cached)
        if actual != row["bundle_sha256"]:
            raise HTTPException(status_code=400, detail=f"Bundle sha256 mismatch: expected {row['bundle_sha256']} got {actual}")

    return {"row": row, "bundle_local": str(bundle_cached), "params_local": str(params_cached)}

# ---------- Bundle introspection helper ----------
def list_bundle_presets(bundle_path: str) -> dict:
    """
    Parse a PrusaSlicer .ini bundle and return all available preset names
    (printers, sla_prints, sla_materials). Used for error logs / debugging.
    """
    try:
        text = Path(bundle_path).read_text(errors="ignore")
    except Exception:
        return {"printers": [], "sla_prints": [], "sla_materials": []}
    printers = re.findall(r'^\[printer:([^\]]+)\]', text, flags=re.M)
    sla_prints = re.findall(r'^\[sla_print:([^\]]+)\]', text, flags=re.M)
    sla_materials = re.findall(r'^\[sla_material:([^\]]+)\]', text, flags=re.M)
    return {"printers": printers, "sla_prints": sla_prints, "sla_materials": sla_materials}

def merge_overrides(params_path: Path, overrides: Optional[Dict[str, Any]]) -> Path:
    """Allow-list merge of job overrides into UVtools params JSON."""
    params = json.loads(Path(params_path).read_text())
    for k, v in (overrides or {}).items():
        if k in ALLOWLIST_OVERRIDE_KEYS:
            params[k] = v
    merged_path = Path(params_path).parent / (Path(params_path).stem + ".merged.json")
    merged_path.write_text(json.dumps(params))
    return merged_path

# ---------- PrusaSlicer runner (AppImage, headless) ----------
def run_prusaslicer_headless(args: list[str]) -> tuple[int, str, str]:
    """
    Run PrusaSlicer AppImage headlessly with xvfb-run.
    Returns (rc, combined_output, rendered_command_str) for better error logs.
    """
    env = os.environ.copy()
    # Helpful in containerized headless environments:
    env.setdefault("NO_AT_BRIDGE", "1")           # avoid a11y D-Bus probing
    env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")  # mesa llvmpipe
    env.setdefault("GDK_BACKEND", "x11")          # ensure X11, not wayland

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
        "storage_bucket": STORAGE_BUCKET,
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
                update_job(
                    job_id,
                    status="failed",
                    error=f"unsupported_model_extension: {ext or '(none)'}; expected one of {sorted(ALLOWED_MODEL_EXTS)}; path was: {path_in_bucket}"
                )
                return {"ok": False, "error": "unsupported_model_extension"}

            input_model = os.path.join(wd, base_name)
            signed_download(bucket, path_in_bucket, input_model)

            # 2) Resolve preset row (DB) and fetch bundle/params (Storage) to local cache
            printer_id_raw = job.get("printer_id", "")
            if not printer_id_raw:
                update_job(job_id, status="failed", error="missing printer_id")
                return {"ok": False, "error": "missing_printer_id"}

            printer_id = printer_id_raw.strip().lower().replace(" ", "_")
            try:
                preset = resolve_preset(printer_id)
            except HTTPException as he:
                update_job(job_id, status="failed", error=f"preset_resolve_failed: {he.detail}")
                return {"ok": False, "error": "preset_resolve_failed"}

            row = preset["row"]
            bundle_local = preset["bundle_local"]
            params_local = preset["params_local"]

            # 2b) Preflight: ensure PrusaSlicer binary is runnable
            # NOTE: 2.8.1 does not support --version; parse first lines of --help instead.
            rc0, log0, cmd0 = run_prusaslicer_headless(["--help"])
            if ("PrusaSlicer" not in (log0 or "")) and (rc0 != 0):
                update_job(job_id, status="failed", error=f"prusaslicer_boot_failed rc={rc0}\nCMD: {cmd0}\n{(log0 or '')[-4000:]}")
                return {"ok": False, "error": "prusaslicer_boot_failed"}

            # 3) Slice with PrusaSlicer — try a small matrix of accepted flag combos
            conf_dir = os.path.join(wd, "ps_config")
            out_dir  = os.path.join(wd, "out")
            os.makedirs(conf_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            printer_name     = row["printer_profile_name"]
            print_profile    = job.get("print_profile")    or row["print_profile_name"]
            material_profile = job.get("material_profile") or row["material_profile_name"]

            attempts: list[list[str]] = []

            # A) SLA export with explicit presets, prefer --output (directory)
            attempts.append([
                "--export-sla",
                "--datadir", conf_dir, "--loglevel", "3",
                "--output", out_dir,
                "--load", bundle_local,
                "--printer-profile", printer_name,
                "--print-profile", print_profile,
                "--material-profile", material_profile,
                input_model
            ])

            # B) Same but explicit format
            attempts.append([
                "--export-sla",
                "--datadir", conf_dir, "--loglevel", "3",
                "--output", out_dir,
                "--load", bundle_local,
                "--printer-profile", printer_name,
                "--print-profile", print_profile,
                "--material-profile", material_profile,
                "--export-format", "png",
                input_model
            ])

            # C) SLA export with printer only (let bundle defaults pick print/material)
            attempts.append([
                "--export-sla",
                "--datadir", conf_dir, "--loglevel", "3",
                "--output", out_dir,
                "--load", bundle_local,
                "--printer-profile", printer_name,
                input_model
            ])

            # D) Explicit "slice" action to force batch slicing paths
            attempts.append([
                "--slice",
                "--datadir", conf_dir, "--loglevel", "3",
                "--output", out_dir,
                "--load", bundle_local,
                "--printer-profile", printer_name,
                "--print-profile", print_profile,
                "--material-profile", material_profile,
                "--export-format", "png",
                input_model
            ])

            # E) Export a project 3MF for debugging / artifact
            project_out = os.path.join(out_dir, "project.3mf")
            attempts.append([
                "--export-3mf",
                "--datadir", conf_dir, "--loglevel", "3",
                "--output", project_out,     # single file path is correct for --export-3mf
                "--load", bundle_local,
                "--printer-profile", printer_name,
                "--print-profile", print_profile,
                "--material-profile", material_profile,
                input_model
            ])

            success = False
            produced_project = False
            attempt_logs = []
            full_logs = []   # capture full logs for first few attempts

            # Record the PS version-ish header for context
            v_rc, v_log, v_cmd = run_prusaslicer_headless(["--help"])
            ps_version = (v_log or "").strip().splitlines()[:3]

            cmd1, log1 = "", ""
            for i, args in enumerate(attempts, start=1):
                rc_try, log_try, cmd_try = run_prusaslicer_headless(args)
                # Success if rc==0 OR (final attempt exported a 3MF)
                if rc_try == 0:
                    success = True
                    cmd1, log1 = cmd_try, log_try
                    break
                if i == len(attempts) and os.path.exists(project_out):
                    success = True
                    produced_project = True
                    cmd1, log1 = cmd_try, log_try
                    break
                # keep a short tail per attempt for the error report (but store head too for clues)
                attempt_logs.append(f"Attempt {i} rc={rc_try}\nCMD: {cmd_try}\nTAIL:\n{(log_try or '')[-1500:]}\n")
                full_logs.append(f"Attempt {i} HEAD:\n{(log_try or '')[:1000]}\n")

            if not success:
                # Introspect bundle to show available preset names (debug without terminal)
                presets_available = list_bundle_presets(bundle_local)
                # Also capture SLA help to include exact flags for this version
                rc_help, log_help, cmd_help = run_prusaslicer_headless(["--help-sla"])
                update_job(
                    job_id,
                    status="failed",
                    error=textwrap.dedent(f"""prusaslicer_failed rc=1
ps_version: {ps_version}
printer_id: {printer_id}
bundle: {row['bundle_path']}  params: {row['uvtools_params_path']}
presets_requested:
  printer={printer_name}
  print={print_profile}
  material={material_profile}
presets_available:
  printers={presets_available.get('printers')}
  sla_prints={presets_available.get('sla_prints')}
  sla_materials={presets_available.get('sla_materials')}

--- ATTEMPTS (TAILS) ---
{chr(10).join(attempt_logs)}

--- ATTEMPTS (HEADS) ---
{chr(10).join(full_logs)}

--help-sla (rc={rc_help}) head:
{(log_help or '')[:1200]}

--help-sla (rc={rc_help}) tail:
{(log_help or '')[-1200:]}
"""))
                return {"ok": False, "error": "prusaslicer_failed"}

            # If we only produced a 3MF (attempt E), treat as partial success.
            if produced_project:
                # We'll proceed to find layers; if none, we still upload the 3MF for inspection.
                pass

            # 3b) Find where PNG layers actually went (discover; don't assume path)
            slices_dir = find_layers(out_dir)
            if not slices_dir:
                update_job(job_id, status="failed", error=f"no_slices_found in {out_dir}.\nCMD: {cmd1}\nPrusa log tail:\n{(log1 or '')[-4000:]}")
                return {"ok": False, "error": "no_slices_found"}

            # 4) Package with UVtools (pack PNG stack to native resin format)
            merged_params_path = merge_overrides(Path(params_local), job.get("overrides"))
            native_format = row["native_format"]  # e.g., 'pwmx', 'ctb_v3', ...
            native_ext = native_format if native_format.startswith("ctb") or native_format.startswith("pwm") else native_format
            native_path = os.path.join(out_dir, f"print.{native_ext}")

            cmd2_list = [
                "uvtools-cli", "pack",
                "--format", native_format,
                "--params", str(merged_params_path),
                "--slices", slices_dir,
                "--out", native_path
            ]
            cmd2 = " ".join(shlex.quote(str(x)) for x in cmd2_list)
            rc2, log2 = sh(cmd2)
            if rc2 != 0:
                update_job(job_id, status="failed", error=f"uvtools_failed rc={rc2}\n{(log2 or '')[-4000:]}")
                return {"ok": False, "error": "uvtools_failed"}

            # 5) Zip layers (for download/debug)
            zip_path = os.path.join(out_dir, "layers.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for fn in sorted(glob.glob(os.path.join(slices_dir, "*.png"))):
                    z.write(fn, arcname=os.path.basename(fn))

            # 6) Upload outputs
            job_prefix = str(job_id)

            def up(bucket_name: str, path: str, local: str, ctype: str):
                with open(local, "rb") as f:
                    _supabase().storage.from_(bucket_name).upload(path, f, {"content-type": ctype, "upsert": True})

            # Native
            up("native",   f"{job_prefix}/print.{native_ext}", native_path, "application/octet-stream")
            # Optional project (.3mf) if PrusaSlicer happened to create one
            proj = next((f for f in os.listdir(out_dir) if f.endswith(".3mf")), None)
            if proj:
                up("projects", f"{job_prefix}/{proj}", os.path.join(out_dir, proj), "model/3mf")
            # Layers zip
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
