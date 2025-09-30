import os, subprocess, shutil, json, tempfile, zipfile, urllib.request, glob, shlex, hashlib, textwrap, re
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

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

# Where the AppImage (extracted AppRun) was installed by your Dockerfile.
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
    p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")

def update_job(job_id: str, **fields):
    try:
        _supabase().table("slice_jobs").update(fields).eq("id", job_id).execute()
    except Exception as e:
        print("DB update failed:", e)

def signed_download(bucket: str, path: str, dest: str):
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
    for candidate in (os.path.join(base_dir, "slices"), os.path.join(base_dir, "sla")):
        if os.path.isdir(candidate) and glob.glob(os.path.join(candidate, "*.png")):
            return candidate
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
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = _supabase().storage.from_(STORAGE_BUCKET).download(object_path)
    dest.write_bytes(data)

def resolve_preset(printer_id: str) -> Dict[str, Any]:
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
    if row.get("bundle_sha256"):
        actual = _sha256(bundle_cached)
        if actual != row["bundle_sha256"]:
            raise HTTPException(status_code=400, detail=f"Bundle sha256 mismatch: expected {row['bundle_sha256']} got {actual}")
    return {"row": row, "bundle_local": str(bundle_cached), "params_local": str(params_cached)}

def list_bundle_presets(bundle_path: str) -> dict:
    try:
        text = Path(bundle_path).read_text(errors="ignore")
    except Exception:
        return {"printers": [], "sla_prints": [], "sla_materials": []}
    printers = re.findall(r'^\[printer:([^\]]+)\]', text, flags=re.M)
    sla_prints = re.findall(r'^\[sla_print:([^\]]+)\]', text, flags=re.M)
    sla_materials = re.findall(r'^\[sla_material:([^\]]+)\]', text, flags=re.M)
    return {"printers": printers, "sla_prints": sla_prints, "sla_materials": sla_materials}

def merge_overrides(params_path: Path, overrides: Optional[Dict[str, Any]]) -> Path:
    params = json.loads(Path(params_path).read_text())
    for k, v in (overrides or {}).items():
        if k in ALLOWLIST_OVERRIDE_KEYS:
            params[k] = v
    merged_path = Path(params_path).parent / (Path(params_path).stem + ".merged.json")
    merged_path.write_text(json.dumps(params))
    return merged_path

# ---------- PrusaSlicer runner & datadir probing ----------
def _base_env() -> dict:
    env = os.environ.copy()
    env.setdefault("NO_AT_BRIDGE", "1")
    env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    env.setdefault("GDK_BACKEND", "x11")
    ps_home = env.get("PS_HOME", "/tmp/ps_home")
    os.makedirs(ps_home, exist_ok=True)
    env.setdefault("HOME", ps_home)
    env.setdefault("XDG_CONFIG_HOME", os.path.join(ps_home, ".config"))
    env.setdefault("XDG_CACHE_HOME", os.path.join(ps_home, ".cache"))
    return env

def run_prusaslicer_headless(args: List[str]) -> Tuple[int, str, str]:
    env = _base_env()
    base = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", PRUSA_APPIMAGE]
    cmd_list = base + args
    cmd_str = " ".join(shlex.quote(x) for x in cmd_list)
    rc, log = sh(cmd_str, env=env)
    return rc, log, cmd_str

# --- NEW: robust datadir validation ---
def _is_valid_datadir(p: str) -> bool:
    """
    Quick static checks to avoid bogus hits (like /usr/bin/resources).
    We require the directory to contain at least two of these markers:
      - profiles/   - vendor/   - shaders/   - icons/   - localization/
    """
    if not p or not os.path.isdir(p):
        return False
    bad_parts = ("/bin/", "/sbin/")
    if any(bp in (p + "/") for bp in bad_parts):
        return False
    markers = 0
    for name in ("profiles", "vendor", "shaders", "icons", "localization"):
        if os.path.isdir(os.path.join(p, name)):
            markers += 1
    return markers >= 2

def _probe_datadir_candidates() -> Tuple[Optional[str], List[str]]:
    """
    Pick a datadir by:
      1) trusting PRUSA_DATADIR if it exists and looks valid,
      2) trying stable/common locations,
      3) scanning under /opt/prusaslicer for likely resource roots,
    then confirming with a --help banner check.
    """
    tried: List[str] = []

    # 0) Respect explicit override if it passes static checks
    override = os.environ.get("PRUSA_DATADIR")
    if override:
        tried.append(override)
        if _is_valid_datadir(override):
            return override, tried

    # 1) Start from resolved AppRun
    try:
        resolved = Path(PRUSA_APPIMAGE).resolve()
    except Exception:
        resolved = Path(PRUSA_APPIMAGE)
    base = resolved.parent

    # 2) Preferred stable locations (Dockerfile created a symlink to this)
    candidates: List[str] = []
    for c in [
        base / "../share/prusa-slicer",
        base / "../share/PrusaSlicer",
        Path("/opt/prusaslicer/usr/share/prusa-slicer"),
        Path("/opt/prusaslicer/usr/share/PrusaSlicer"),
        Path("/opt/prusaslicer/share/prusa-slicer"),
        Path("/opt/prusaslicer/share/PrusaSlicer"),
        Path("/opt/prusaslicer/resources"),
        Path("/opt/prusaslicer/Resources"),
        Path("/opt/prusaslicer/usr/resources"),
        Path("/opt/prusaslicer/usr/Resources"),
        Path("/opt/prusaslicer/usr/lib/PrusaSlicer/resources"),
        Path("/opt/prusaslicer/usr/lib64/PrusaSlicer/resources"),
    ]:
        cs = str(Path(c).resolve())
        if cs not in candidates:
            candidates.append(cs)

    # 3) Scan under /opt/prusaslicer up to depth 5 for dirs named like resources roots
    root = Path("/opt/prusaslicer")
    if root.exists():
        for p in root.rglob("*"):
            try:
                if not p.is_dir():
                    continue
                name = p.name
                # ignore obviously wrong trees
                pstr = str(p.resolve())
                if "/bin/" in pstr or "/sbin/" in pstr:
                    continue
                if name in ("prusa-slicer", "PrusaSlicer", "resources", "Resources"):
                    if pstr not in candidates:
                        candidates.append(pstr)
            except Exception:
                continue

    # 4) Evaluate candidates with static markers first
    stat_ok = [p for p in candidates if _is_valid_datadir(p)]
    tried.extend(candidates)

    # 5) Confirm with a light runtime check (banner, and no 'Configuration wasn't found')
    for c in stat_ok:
        rc, log, _ = run_prusaslicer_headless(["--datadir", c, "--help"])
        if "PrusaSlicer" in (log or "") and "Configuration wasn't found" not in (log or ""):
            return c, tried

    # If nothing passes runtime check, but override existed and stat-ok, use it anyway to aid debugging.
    if override and _is_valid_datadir(override):
        return override, tried

    return None, tried

# ---------- Health/Readiness ----------
@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/ready")
def ready():
    chosen, tried = _probe_datadir_candidates()
    return {
        "ok": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and WORKER_TOKEN and chosen),
        "has_SUPABASE_URL": bool(SUPABASE_URL),
        "has_SERVICE_ROLE": bool(SUPABASE_SERVICE_ROLE_KEY),
        "has_WORKER_TOKEN": bool(WORKER_TOKEN),
        "storage_bucket": STORAGE_BUCKET,
        "datadir": chosen,
        "datadir_tried": tried[:12],
    }

# ---------- Main job endpoint ----------
@app.post("/jobs")
def start_job(payload: Dict[str, Any], authorization: str = Header(None)):
    try:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            return JSONResponse({"ok": False, "error": "missing_env SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY"}, status_code=200)
        if not WORKER_TOKEN:
            return JSONResponse({"ok": False, "error": "missing_env WORKER_TOKEN"}, status_code=200)

        if authorization != f"Bearer {WORKER_TOKEN}":
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=200)

        job_id = payload.get("job_id")
        if not job_id:
            return JSONResponse({"ok": False, "error": "missing job_id"}, status_code=200)

        s = _supabase()
        job_q = s.table("slice_jobs").select("*").eq("id", job_id).single().execute()
        job = job_q.data
        if not job:
            return JSONResponse({"ok": False, "error": "job_not_found"}, status_code=200)

        update_job(job_id, status="processing", error=None)

        wd = tempfile.mkdtemp(prefix=f"job_{job_id}_")
        try:
            # Resolve valid datadir
            datadir, tried = _probe_datadir_candidates()
            if not datadir:
                update_job(
                    job_id,
                    status="failed",
                    error=textwrap.dedent(f"""prusaslicer_datadir_not_found
PRUSA_APPIMAGE={PRUSA_APPIMAGE}
Tried datadirs: {tried}"""),
                )
                return {"ok": False, "error": "prusaslicer_datadir_not_found"}

            # Input model
            in_spec = job["input_path"]
            if ":" not in in_spec:
                update_job(job_id, status="failed", error=f"bad_input_path_format (expected 'bucket:path'): {in_spec}")
                return {"ok": False, "error": "bad_input_path_format"}
            bucket, path_in_bucket = in_spec.split(":", 1)

            base_name = os.path.basename(path_in_bucket.split("?")[0])
            _, ext = os.path.splitext(base_name)
            ext = ext.lower()
            if ext not in ALLOWED_MODEL_EXTS:
                update_job(job_id, status="failed", error=f"unsupported_model_extension: {ext or '(none)'}; expected one of {sorted(ALLOWED_MODEL_EXTS)}; path was: {path_in_bucket}")
                return {"ok": False, "error": "unsupported_model_extension"}

            input_model = os.path.join(wd, base_name)
            signed_download(bucket, path_in_bucket, input_model)

            # Presets
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

            # Preflight
            rc0, log0, cmd0 = run_prusaslicer_headless(["--datadir", datadir, "--help"])
            if ("PrusaSlicer" not in (log0 or "")) and (rc0 != 0):
                update_job(job_id, status="failed", error=f"prusaslicer_boot_failed rc={rc0}\nCMD: {cmd0}\n{(log0 or '')[-4000:]}")
                return {"ok": False, "error": "prusaslicer_boot_failed"}

            out_dir = os.path.join(wd, "out")
            os.makedirs(out_dir, exist_ok=True)

            printer_name     = row["printer_profile_name"]
            print_profile    = job.get("print_profile")    or row["print_profile_name"]
            material_profile = job.get("material_profile") or row["material_profile_name"]

            attempts: List[List[str]] = []

            attempts.append([
                "--export-sla",
                "--datadir", datadir,
                "--loglevel", "3",
                "--output", out_dir,
                "--load", bundle_local,
                "--printer-profile", printer_name,
                "--print-profile", print_profile,
                "--material-profile", material_profile,
                input_model
            ])

            attempts.append([
                "--slice",
                "--datadir", datadir,
                "--loglevel", "3",
                "--output", out_dir,
                "--load", bundle_local,
                "--printer-profile", printer_name,
                "--print-profile", print_profile,
                "--material-profile", material_profile,
                input_model
            ])

            project_out = os.path.join(out_dir, "project.3mf")
            attempts.append([
                "--export-3mf",
                "--datadir", datadir,
                "--loglevel", "3",
                "--output", project_out,
                "--load", bundle_local,
                "--printer-profile", printer_name,
                "--print-profile", print_profile,
                "--material-profile", material_profile,
                input_model
            ])

            success = False
            produced_project = False
            attempt_logs: List[str] = []
            full_logs: List[str] = []

            v_rc, v_log, v_cmd = run_prusaslicer_headless(["--datadir", datadir, "--help"])
            ps_version = (v_log or "").strip().splitlines()[:3]

            cmd1, log1 = "", ""
            for i, args in enumerate(attempts, start=1):
                rc_try, log_try, cmd_try = run_prusaslicer_headless(args)
                if rc_try == 0:
                    success = True
                    cmd1, log1 = cmd_try, log_try
                    break
                if i == len(attempts) and os.path.exists(project_out):
                    success = True
                    produced_project = True
                    cmd1, log1 = cmd_try, log_try
                    break
                attempt_logs.append(f"Attempt {i} rc={rc_try}\nCMD: {cmd_try}\nTAIL:\n{(log_try or '')[-1500:]}\n")
                full_logs.append(f"Attempt {i} HEAD:\n{(log_try or '')[:1000]}\n")

            if not success:
                presets_available = list_bundle_presets(bundle_local)
                rc_help, log_help, cmd_help = run_prusaslicer_headless(["--datadir", datadir, "--help-sla"])
                update_job(
                    job_id,
                    status="failed",
                    error=textwrap.dedent(f"""prusaslicer_failed rc=1
ps_version: {ps_version}
printer_id: {printer_id}
bundle: {row['bundle_path']}  params: {row['uvtools_params_path']}
chosen_datadir: {datadir}

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

            if produced_project:
                pass

            slices_dir = find_layers(out_dir)
            if not slices_dir:
                update_job(job_id, status="failed", error=f"no_slices_found in {out_dir}.\nCMD: {cmd1}\nPrusa log tail:\n{(log1 or '')[-4000:]}")
                return {"ok": False, "error": "no_slices_found"}

            merged_params_path = merge_overrides(Path(params_local), job.get("overrides"))
            native_format = row["native_format"]
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

            zip_path = os.path.join(out_dir, "layers.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for fn in sorted(glob.glob(os.path.join(slices_dir, "*.png"))):
                    z.write(fn, arcname=os.path.basename(fn))

            job_prefix = str(job_id)

            def up(bucket_name: str, path: str, local: str, ctype: str):
                with open(local, "rb") as f:
                    _supabase().storage.from_(bucket_name).upload(path, f, {"content-type": ctype, "upsert": True})

            up("native",   f"{job_prefix}/print.{native_ext}", native_path, "application/octet-stream")
            proj = next((f for f in os.listdir(out_dir) if f.endswith(".3mf")), None)
            if proj:
                up("projects", f"{job_prefix}/{proj}", os.path.join(out_dir, proj), "model/3mf")
            up("slices",   f"{job_prefix}/layers.zip", zip_path, "application/zip")

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
        return JSONResponse({"ok": False, "error": f"fatal: {e}"}, status_code=200)
