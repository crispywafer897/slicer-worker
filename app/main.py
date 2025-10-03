import os, subprocess, shutil, json, tempfile, zipfile, urllib.request, glob, shlex, hashlib, textwrap, re, time, base64, logging
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("startup")
log.info("Starting slicer-worker app bootstrap...")

try:
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import JSONResponse
except Exception as e:
    log.exception("FastAPI import failed at startup")
    raise

try:
    from supabase import create_client, Client
    _SUPABASE_IMPORT_OK = True
    _SUPABASE_IMPORT_ERR = ""
except Exception as _imp_err:
    create_client = None
    Client = None
    _SUPABASE_IMPORT_OK = False
    _SUPABASE_IMPORT_ERR = f"{type(_imp_err).__name__}: {_imp_err}"
    log.warning("Supabase import failed (service will still start): %s", _SUPABASE_IMPORT_ERR)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")

STORAGE_BUCKET = os.environ.get("STORAGE_BUCKET", "slicer-presets")
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/preset_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

PRUSA_APPIMAGE = "/usr/local/bin/prusaslicer"

ALLOWED_MODEL_EXTS = {".stl", ".3mf", ".obj", ".amf"}
NATIVE_EXTS = (".pwmx", ".ctb", ".ctb2", ".ctb7", ".photon", ".phz", ".cbddlp", ".sl1", ".sl1s", ".pw0", ".pws")

ALLOWLIST_OVERRIDE_KEYS = {
    "layer_height_mm", "bottom_layers", "bottom_exposure_s", "normal_exposure_s",
    "light_off_delay_s", "lift_height_mm", "lift_speed_mm_s", "retract_speed_mm_s",
    "anti_aliasing"
}

if os.environ.get("PRUSA_DATADIR") and not os.environ.get("PS_FORCE_DATADIR"):
    log.info("Ignoring PRUSA_DATADIR; use PS_FORCE_DATADIR to opt-in to a custom data directory.")

app = FastAPI()

def _supabase():
    if not _SUPABASE_IMPORT_OK:
        raise RuntimeError(f"supabase lib import failed: {_SUPABASE_IMPORT_ERR}")
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

def sh(cmd: str, cwd: Optional[str] = None, env: Optional[dict] = None, timeout: Optional[int] = None) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, 
            shell=True, 
            cwd=cwd, 
            capture_output=True, 
            text=True, 
            env=env,
            timeout=timeout
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        return (124, f"TIMEOUT after {timeout}s\nstdout: {e.stdout or ''}\nstderr: {e.stderr or ''}")

def update_job(job_id: str, **fields):
    try:
        _supabase().table("slice_jobs").update(fields).eq("id", job_id).execute()
    except Exception as e:
        log.warning("DB update failed: %s", e)

def signed_download(bucket: str, path: str, dest: str):
    if not SUPABASE_URL.startswith("https://"):
        raise RuntimeError(f"SUPABASE_URL is missing or invalid: {SUPABASE_URL!r}")
    res = _supabase().storage.from_(bucket).create_signed_url(path, 3600)
    signed = res.get("signedURL") or res.get("signed_url")
    if not signed:
        raise RuntimeError(f"create_signed_url returned no URL for {bucket}:{path}")
    url = signed if signed.startswith("http") else SUPABASE_URL.rstrip("/") + signed
    if not url.startswith("https://"):
        raise RuntimeError(f"Bad signed URL: {url}")
    log.info("Downloading model from signed URL")
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        f.write(r.read())

def upload_file(bucket: str, path: str, local_path: str, content_type: str, upsert: bool = True, max_retries: int = 3):
    file_size_mb = Path(local_path).stat().st_size / (1024 * 1024)
    log.info(f"Uploading {path} to {bucket} ({file_size_mb:.1f} MB)")
    
    if file_size_mb > 50:
        log.info(f"Using streaming upload for large file")
        
        for attempt in range(max_retries):
            try:
                start = time.time()
                
                import requests
                url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
                headers = {
                    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                    "Content-Type": content_type,
                    "x-upsert": "true" if upsert else "false",
                }
                
                with open(local_path, 'rb') as f:
                    response = requests.post(url, headers=headers, data=f, timeout=600)
                    response.raise_for_status()
                
                elapsed = time.time() - start
                log.info(f"Upload succeeded in {elapsed:.1f}s (attempt {attempt + 1})")
                return
            except Exception as e:
                elapsed = time.time() - start
                log.warning(f"Upload attempt {attempt + 1} failed after {elapsed:.1f}s: {e}")
                if attempt == max_retries - 1:
                    log.error(f"All {max_retries} upload attempts failed for {path}")
                    raise
                time.sleep(2 ** attempt)
    else:
        data = Path(local_path).read_bytes()
        file_options = {
            "contentType": content_type,
            "upsert": "true" if upsert else "false",
            "cacheControl": "3600",
        }
        
        for attempt in range(max_retries):
            try:
                start = time.time()
                _supabase().storage.from_(bucket).upload(path, data, file_options)
                elapsed = time.time() - start
                log.info(f"Upload succeeded in {elapsed:.1f}s (attempt {attempt + 1})")
                return
            except Exception as e:
                elapsed = time.time() - start
                log.warning(f"Upload attempt {attempt + 1} failed after {elapsed:.1f}s: {e}")
                if attempt == max_retries - 1:
                    log.error(f"All {max_retries} upload attempts failed for {path}")
                    raise
                time.sleep(2 ** attempt)

def find_layers(base_dir: str) -> Optional[str]:
    standard_locations = [
        os.path.join(base_dir, "slices"),
        os.path.join(base_dir, "sla"),
        os.path.join(base_dir, "SLA"),
    ]
    
    if os.path.exists(base_dir):
        for item in os.listdir(base_dir):
            item_path = os.path.join(base_dir, item)
            if os.path.isdir(item_path):
                standard_locations.append(os.path.join(item_path, "slices"))
                standard_locations.append(os.path.join(item_path, "sla"))
                standard_locations.append(item_path)
    
    for candidate in standard_locations:
        if os.path.isdir(candidate):
            pngs = glob.glob(os.path.join(candidate, "*.png"))
            if pngs:
                log.info(f"Found {len(pngs)} PNG files in {candidate}")
                return candidate
    
    best_dir, best_count = None, 0
    for root, _, files in os.walk(base_dir):
        png_files = [f for f in files if f.lower().endswith(".png")]
        count = len(png_files)
        if count > best_count:
            best_dir, best_count = root, count
            log.info(f"Found {count} PNGs in {root}")
    
    if best_dir:
        log.info(f"Using directory with most PNGs: {best_dir} ({best_count} files)")
    else:
        log.warning(f"No PNG files found anywhere under {base_dir}")
    
    return best_dir if best_count > 0 else None

def find_native_artifact(base_dir: str) -> Optional[str]:
    candidates: List[str] = []
    for root, _, files in os.walk(base_dir):
        for fn in files:
            if fn.lower().endswith(NATIVE_EXTS):
                candidates.append(os.path.join(root, fn))
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p))
    return candidates[-1]

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
            raise HTTPException(status_code=400, detail=f"Bundle sha256 mismatch")
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

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def _has_uvtools() -> bool:
    from shutil import which
    if any(which(name) for name in ("uvtools-cli","uvtools","UVtools")):
        return True
    return Path("/usr/local/bin/uvtools-cli").exists()

def _unpack_sl1_to_pngs(native_path: str, out_dir: str) -> Optional[str]:
    try:
        with zipfile.ZipFile(native_path, 'r') as z:
            z.extractall(out_dir)
    except Exception:
        return None
    return find_layers(out_dir)

def _config_root_from_env(env: dict) -> Path:
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", "/tmp/ps_home"), ".config")
    return Path(xdg) / "PrusaSlicer"

def _ensure_minimal_user_config(env: dict) -> Path:
    root = _config_root_from_env(env)
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("printer", "print", "filament", "snapshots"):
        (root / sub).mkdir(exist_ok=True)
    ini = root / "PrusaSlicer.ini"
    if not ini.exists():
        ini.write_text("[preferences]\nversion = 2.8.1\nmode = Expert\n")
    return root

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

def run_prusaslicer_headless(args: List[str], timeout: int = 900) -> Tuple[int, str, str]:
    env = _base_env()
    _ensure_minimal_user_config(env)
    base = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", PRUSA_APPIMAGE]
    cmd_list = base + args
    cmd_str = " ".join(shlex.quote(x) for x in cmd_list)
    rc, logtxt = sh(cmd_str, env=env, timeout=timeout)
    return rc, logtxt, cmd_str

def _resolve_datadir_opt_in() -> Optional[str]:
    forced = os.environ.get("PS_FORCE_DATADIR")
    if forced and os.path.isdir(forced):
        return forced
    if os.environ.get("PS_FORCE_DATADIR") and not (forced and os.path.isdir(forced)):
        log.warning("PS_FORCE_DATADIR was set but not a valid directory: %r", os.environ.get("PS_FORCE_DATADIR"))
    return None

def _maybe_with_datadir(args: List[str], datadir: Optional[str]) -> List[str]:
    return (["--datadir", datadir] if datadir else []) + args

def _extract_section(text: str, kind: str, name: str) -> Dict[str, str]:
    pat = re.compile(rf"^\[{re.escape(kind)}:{re.escape(name)}\]\s*([\s\S]*?)(?=^\[|\Z)", re.M)
    m = pat.search(text)
    if not m:
        return {}
    block = m.group(1)
    out: Dict[str, str] = {}
    for ln in block.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("#", ";", "[")):
            continue
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out

def materialize_cli_config(bundle_path: str, printer_name: str, print_name: str, material_name: str, dest_dir: str) -> Path:
    txt = Path(bundle_path).read_text(errors="ignore")
    merged: Dict[str, str] = {}
    for kind, name in (("printer", printer_name), ("sla_print", print_name), ("sla_material", material_name)):
        section = _extract_section(txt, kind, name)
        if not section:
            raise HTTPException(status_code=400, detail=f"Missing section [{kind}:{name}] in bundle")
        merged.update(section)
    merged.setdefault("printer_technology", "SLA")
    out = Path(dest_dir) / "merged_cli.ini"
    with out.open("w") as f:
        for k, v in merged.items():
            f.write(f"{k} = {v}\n")
    return out

def _create_sl1_from_pngs(slices_dir: str, params_obj: Dict[str, Any], output_path: str) -> bool:
    try:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            png_files = sorted(glob.glob(os.path.join(slices_dir, "*.png")))
            if not png_files:
                return False
            for i, png_file in enumerate(png_files):
                zf.write(png_file, f"{i:05d}.png")
            
            # Extract parameters with proper defaults
            layer_height = params_obj.get('layer_height_mm', 0.1)
            bottom_layers = params_obj.get('bottom_layers', 6)
            normal_exp = params_obj.get('normal_exposure_s', 3.0)
            bottom_exp = params_obj.get('bottom_exposure_s', 20.0)
            
            # Motion parameters (critical for CTB)
            lift_height = params_obj.get('lift_height_mm', 0.1)
            lift_speed = params_obj.get('lift_speed_mm_s', 0.05)
            retract_speed = params_obj.get('retract_speed_mm_s', 0.05)
            bottom_lift_height = params_obj.get('bottom_lift_height_mm', 0.1)
            bottom_lift_speed = params_obj.get('bottom_lift_speed_mm_s', 0.05)
            
            # Rest times
            rest_after_lift = params_obj.get('rest_time_after_lift_s', 0.2)
            rest_after_retract = params_obj.get('rest_time_after_retract_s', 0.8)
            rest_before_lift = params_obj.get('rest_time_before_lift_s', 0.2)
            
            # PWM values
            light_pwm = params_obj.get('light_pwm', 255)
            bottom_light_pwm = params_obj.get('bottom_light_pwm', 255)
            
            # Machine name (CRITICAL)
            machine_name = params_obj.get('machine_name', 'ELEGOO Saturn 4 Ultra')
            
            # Create prusaslicer.ini with full metadata
            prusaslicer_ini = f"""expTime = {normal_exp}
expTimeFirst = {bottom_exp}
fileCreationTimestamp = 2024-01-01 at 12:00:00 UTC
jobDir = job1
layerHeight = {layer_height}
materialName = Generic
numFade = 0
numFast = {len(png_files) - bottom_layers}
numSlow = {bottom_layers}
printTime = 0
printerModel = {machine_name}
printerVariant = default
printProfile = default
"""
            zf.writestr("prusaslicer.ini", prusaslicer_ini)
            
            # Create config.ini with extended metadata for CTB conversion
            config_ini = f"""[general]
fileVersion = 1
jobDir = job1
layerHeight = {layer_height}
initialLayerCount = {bottom_layers}
printTime = 0
materialName = Generic
printerModel = {machine_name}
printerVariant = default
printProfile = default
materialProfile = default
numFade = 0
numSlow = {bottom_layers}
numFast = {len(png_files) - bottom_layers}
expTime = {normal_exp}
expTimeFirst = {bottom_exp}
liftHeight = {lift_height}
liftSpeed = {lift_speed}
retractSpeed = {retract_speed}
bottomLiftHeight = {bottom_lift_height}
bottomLiftSpeed = {bottom_lift_speed}
restTimeAfterLift = {rest_after_lift}
restTimeAfterRetract = {rest_after_retract}
restTimeBeforeLift = {rest_before_lift}
lightPWM = {light_pwm}
bottomLightPWM = {bottom_light_pwm}
"""
            zf.writestr("config.ini", config_ini)
        return True
    except Exception as e:
        log.error(f"Failed to create SL1: {e}")
        return False

def uvtools_version() -> Tuple[int, str]:
    if not _has_uvtools():
        return (127, "uvtools-cli not found on PATH")
    return sh("uvtools-cli --version")

def _write_min_png(dir_path: str, name: str):
    import struct
    import zlib
    
    width, height = 100, 100
    
    png_sig = b'\x89PNG\r\n\x1a\n'
    
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0)
    ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
    
    raw_data = b''
    for y in range(height):
        raw_data += b'\x00'
        raw_data += b'\x00' * width
    
    compressed = zlib.compress(raw_data, 9)
    idat_crc = zlib.crc32(b'IDAT' + compressed) & 0xffffffff
    idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + struct.pack('>I', idat_crc)
    
    iend_crc = 0xae426082
    iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
    
    png_data = png_sig + ihdr + idat + iend
    
    p = Path(dir_path) / f"{name}.png"
    p.write_bytes(png_data)

def uvtools_synthetic_pack_test() -> Tuple[int, str]:
    if not _has_uvtools():
        return (127, "uvtools-cli not found on PATH")
    with tempfile.TemporaryDirectory() as td:
        layers = Path(td) / "layers"
        layers.mkdir(parents=True, exist_ok=True)
        _write_min_png(str(layers), "00000")
        _write_min_png(str(layers), "00001")
        temp_sl1 = os.path.join(td, "temp.sl1")
        output_ctb = os.path.join(td, "test.ctb")
        params = {"layer_height_mm": 0.05, "bottom_layers": 2, "normal_exposure_s": 2.5, "bottom_exposure_s": 20.0}
        if not _create_sl1_from_pngs(str(layers), params, temp_sl1):
            return (1, "Failed to create temp SL1")
        cmd = f"uvtools-cli convert {shlex.quote(temp_sl1)} ctb {shlex.quote(output_ctb)} --version 4"
        rc, logtxt = sh(cmd)
        if Path(output_ctb).exists() and Path(output_ctb).stat().st_size > 0 and "Done" in (logtxt or ""):
            return (0, f"synthetic convert OK → {Path(output_ctb).name} ({Path(output_ctb).stat().st_size} bytes)")
        return (rc, f"synthetic convert failed rc={rc}\n{(logtxt or '')[-2000:]}")

@app.get("/healthz")
def healthz():
    return {"ok": True, "msg": "slicer-worker alive"}

@app.get("/ready")
def ready():
    forced = _resolve_datadir_opt_in()
    return {
        "ok": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY and WORKER_TOKEN and _SUPABASE_IMPORT_OK),
        "has_SUPABASE_URL": bool(SUPABASE_URL),
        "has_SERVICE_ROLE": bool(SUPABASE_SERVICE_ROLE_KEY),
        "has_WORKER_TOKEN": bool(WORKER_TOKEN),
        "supabase_import_ok": _SUPABASE_IMPORT_OK,
        "supabase_import_err": _SUPABASE_IMPORT_ERR if not _SUPABASE_IMPORT_OK else "",
        "storage_bucket": STORAGE_BUCKET,
        "datadir": forced or "(none; using default user config under $HOME)",
    }

@app.get("/")
def root():
    return {"ok": True, "service": "slicer-worker"}

@app.get("/diag/uvtools")
def diag_uvtools():
    rc_v, log_v = uvtools_version()
    rc_p, log_p = uvtools_synthetic_pack_test()
    return {
        "has_uvtools": _has_uvtools(),
        "version_rc": rc_v,
        "version": (log_v or "").strip().splitlines()[:2],
        "synthetic_pack_rc": rc_p,
        "synthetic_pack_result": (log_p or "")[-1200:],
    }

@app.get("/diag/uvtools-help")
def diag_uvtools_help():
    rc, output = sh("uvtools-cli --help", timeout=30)
    return {
        "rc": rc,
        "help_output": output
    }
    
@app.get("/diag/encoders")
def diag_encoders():
    rc, output = sh("uvtools-cli convert --help", timeout=30)
    return {
        "rc": rc,
        "help_output": output,
        "has_uvtools": _has_uvtools()
    }

def _validate_uvtools_params(params: Dict[str, Any], target_format: str) -> Optional[str]:
    must_have_common = ["layer_height_mm"]
    for k in must_have_common:
        if k not in params:
            return f"params missing required key: {k}"
    if target_format in ("ctb","ctb7","ctb2"):
        needed = ["display_pixels_x", "display_pixels_y", "bottom_layers", "bottom_exposure_s", "normal_exposure_s", "lift_height_mm", "lift_speed_mm_s", "retract_speed_mm_s"]
        if not any(k in params for k in ("pixel_size_um","pixel_size_x_um")):
            return "params missing pixel size"
        for k in needed:
            if k not in params:
                return f"params missing required key for {target_format}: {k}"
    if target_format in ("sl1","sl1s"):
        for k in ("bottom_exposure_s","normal_exposure_s"):
            if k not in params:
                return f"params missing required key for {target_format}: {k}"
    return None

@app.post("/jobs")
def start_job(payload: Dict[str, Any], authorization: str = Header(None)):
    try:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            return JSONResponse({"ok": False, "error": "missing_env"}, status_code=200)
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
            datadir = _resolve_datadir_opt_in()
            in_spec = job["input_path"]
            if ":" not in in_spec:
                update_job(job_id, status="failed", error=f"bad_input_path_format: {in_spec}")
                return {"ok": False, "error": "bad_input_path_format"}
            bucket, path_in_bucket = in_spec.split(":", 1)
            base_name = os.path.basename(path_in_bucket.split("?")[0])
            _, ext = os.path.splitext(base_name)
            ext = ext.lower()
            if ext not in ALLOWED_MODEL_EXTS:
                update_job(job_id, status="failed", error=f"unsupported_model_extension: {ext}")
                return {"ok": False, "error": "unsupported_model_extension"}
            input_model = os.path.join(wd, base_name)
            signed_download(bucket, path_in_bucket, input_model)
            
            original_stem = Path(base_name).stem
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            file_prefix = f"{original_stem}_{job_id}_{timestamp}"
            log.info(f"File prefix for outputs: {file_prefix}")
            
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
            rc0, log0, cmd0 = run_prusaslicer_headless(_maybe_with_datadir(["--help"], datadir), timeout=30)
            if ("PrusaSlicer" not in (log0 or "")) and (rc0 != 0):
                update_job(job_id, status="failed", error=f"prusaslicer_boot_failed rc={rc0}")
                return {"ok": False, "error": "prusaslicer_boot_failed"}
            out_dir = os.path.join(wd, "out")
            os.makedirs(out_dir, exist_ok=True)
            printer_name = row["printer_profile_name"]
            print_profile = job.get("print_profile") or row["print_profile_name"]
            material_profile = job.get("material_profile") or row["material_profile_name"]
            try:
                merged_cli_ini = materialize_cli_config(bundle_local, printer_name, print_profile, material_profile, dest_dir=wd)
            except HTTPException as he:
                update_job(job_id, status="failed", error=f"bundle_section_missing: {he.detail}")
                return {"ok": False, "error":"bundle_section_missing"}
            
            input_basename = Path(input_model).stem
            potential_output_dirs = [
                out_dir,
                os.path.join(out_dir, input_basename),
                wd
            ]

            attempts: List[List[str]] = []
            attempts.append(_maybe_with_datadir(["--export-sla","--loglevel","3","--output",out_dir + "/","--load",str(merged_cli_ini),input_model], datadir))
            attempts.append(_maybe_with_datadir(["--slice","--loglevel","3","--output",out_dir + "/","--load",str(merged_cli_ini),input_model], datadir))
            project_out = os.path.join(out_dir, "project.3mf")
            attempts.append(_maybe_with_datadir(["--export-3mf","--loglevel","3","--output",project_out,"--load",str(merged_cli_ini),input_model], datadir))
            
            success = False
            cmd1, log1 = "", ""
            for i, args in enumerate(attempts):
                log.info(f"PrusaSlicer attempt {i+1}/{len(attempts)}: {' '.join(args[:3])}...")
                log.info(f"Starting PrusaSlicer at {time.strftime('%Y-%m-%d %H:%M:%S')}")
                start_time = time.time()
                rc_try, log_try, cmd_try = run_prusaslicer_headless(args, timeout=900)
                elapsed = time.time() - start_time
                log.info(f"PrusaSlicer attempt {i+1} completed in {elapsed:.1f}s with rc={rc_try}")
                
                if rc_try == 124:
                    log.error(f"PrusaSlicer timed out after 900s on attempt {i+1}")
                    cmd1, log1 = cmd_try, log_try
                    continue
                
                if rc_try == 0:
                    success = True
                    cmd1, log1 = cmd_try, log_try
                    log.info(f"PrusaSlicer succeeded on attempt {i+1}")
                    break
                else:
                    cmd1, log1 = cmd_try, log_try
                    log.warning(f"PrusaSlicer attempt {i+1} failed with rc={rc_try}, trying next...")
            
            if not success and os.path.exists(project_out):
                success = True
            
            if not success:
                if "TIMEOUT" in log1:
                    update_job(job_id, status="failed", error=f"prusaslicer_timeout: Model too complex or large. Try simplifying the mesh. Last log: {log1[-500:]}")
                    return {"ok": False, "error": "prusaslicer_timeout"}
                
                log.error(f"PrusaSlicer attempts all failed or returned non-zero")
                log.error(f"Last attempt log output (first 2000 chars): {log1[:2000] if log1 else 'no log'}")
                log.error(f"Last attempt log output (last 2000 chars): {log1[-2000:] if log1 else 'no log'}")
                update_job(job_id, status="failed", error=f"prusaslicer_failed. Last log: {log1[-500:] if log1 else 'no output'}")
                return {"ok": False, "error": "prusaslicer_failed"}
            
            log.info(f"=== COMPLETE WORKSPACE STRUCTURE AFTER PRUSASLICER ===")
            log.info(f"Workspace root: {wd}")
            all_files = []
            for root, dirs, files in os.walk(wd):
                rel_path = os.path.relpath(root, wd)
                log.info(f"DIR: {rel_path}/")
                for d in dirs:
                    log.info(f"  SUBDIR: {d}/")
                for f in files:
                    full_path = os.path.join(root, f)
                    size = os.path.getsize(full_path)
                    log.info(f"  FILE: {f} ({size} bytes)")
                    all_files.append(full_path)
            log.info(f"=== END WORKSPACE STRUCTURE (Total files: {len(all_files)}) ===")
            
            if success:
                log.info(f"PrusaSlicer succeeded, searching for output in multiple locations...")
                for search_dir in potential_output_dirs:
                    if os.path.exists(search_dir):
                        log.info(f"Checking potential output dir: {search_dir}")
                        if os.path.isdir(search_dir):
                            contents = os.listdir(search_dir)
                            log.info(f"  Contains {len(contents)} items: {contents[:20]}")
                        else:
                            log.info(f"  NOT A DIRECTORY (is a file)")
            
            native_found = find_native_artifact(out_dir)
            if native_found:
                found_ext = Path(native_found).suffix.lstrip(".").lower()
                expected_native = str(row.get("native_format", "")).lower()
                
                if found_ext in ("sl1", "sl1s") and found_ext != expected_native:
                    log.info(f"Found {found_ext} file, extracting PNGs to convert to {expected_native}")
                    extract_dir = os.path.join(out_dir, "extracted_sl1")
                    os.makedirs(extract_dir, exist_ok=True)
                    try:
                        with zipfile.ZipFile(native_found, 'r') as z:
                            z.extractall(extract_dir)
                        log.info(f"Extracted .sl1 contents to {extract_dir}")
                    except Exception as e:
                        log.error(f"Failed to extract .sl1: {e}")
                        update_job(job_id, status="failed", error=f"sl1_extraction_failed: {e}")
                        return {"ok": False, "error": "sl1_extraction_failed"}
                elif found_ext == expected_native and found_ext:
                    log.info(f"Native format matches expected: {found_ext}")
                    slices_dir_opt = find_layers(out_dir)
                    zip_path = None
                    if slices_dir_opt:
                        zip_path = os.path.join(out_dir, f"{file_prefix}_layers.zip")
                        log.info(f"Creating layers ZIP at {zip_path}")
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                            for fn in sorted(glob.glob(os.path.join(slices_dir_opt, "*.png"))):
                                z.write(fn, arcname=os.path.basename(fn))
                    native_ext = found_ext
                    
                    log.info("Starting file uploads...")
                    upload_start = time.time()
                    
                    log.info(f"Uploading native file: {file_prefix}.{native_ext}")
                    upload_file("native", f"{job_id}/{file_prefix}.{native_ext}", native_found, "application/octet-stream")
                    
                    proj = next((f for f in os.listdir(out_dir) if f.endswith(".3mf")), None)
                    if proj:
                        log.info(f"Uploading 3MF project file: {file_prefix}.3mf")
                        upload_file("projects", f"{job_id}/{file_prefix}.3mf", os.path.join(out_dir, proj), "model/3mf")
                    
                    if zip_path:
                        log.info(f"Uploading layers ZIP: {file_prefix}_layers.zip")
                        upload_file("slices", f"{job_id}/{file_prefix}_layers.zip", zip_path, "application/zip")
                    
                    upload_elapsed = time.time() - upload_start
                    log.info(f"All uploads completed in {upload_elapsed:.1f}s")
                    
                    report = {"native_ext": native_ext, "native_source": "prusaslicer", "layers": len(glob.glob(os.path.join(slices_dir_opt, "*.png"))) if slices_dir_opt else 0}
                    
                    log.info("Updating job status to succeeded")
                    update_job(
                        job_id, 
                        status="succeeded", 
                        report=report, 
                        output_native_path=f"native:{job_id}/{file_prefix}.{native_ext}", 
                        output_project_path=(f"projects:{job_id}/{file_prefix}.3mf" if proj else None), 
                        output_slices_zip_path=(f"slices:{job_id}/{file_prefix}_layers.zip" if zip_path else None), 
                        error=None
                    )
                    log.info("Job status update completed")
                    return {"ok": True}
            
            slices_dir = None
            for search_dir in potential_output_dirs:
                if os.path.exists(search_dir):
                    slices_dir = find_layers(search_dir)
                    if slices_dir:
                        log.info(f"Found slices in {slices_dir}")
                        break
            
            if not slices_dir:
                log.error(f"No slices found. Workspace structure:")
                for root, dirs, files in os.walk(wd):
                    log.error(f"DIR: {root}")
                    for f in files:
                        log.error(f"  FILE: {os.path.join(root, f)}")
                update_job(job_id, status="failed", error=f"no_slices_found in any output location. Searched: {', '.join(potential_output_dirs)}")
                return {"ok": False, "error": "no_slices_found"}
            
            merged_params_path = merge_overrides(Path(params_local), job.get("overrides"))
            params_obj = _read_json(Path(merged_params_path))
            
            target_format = str(params_obj.get("target_format") or row.get("native_format") or "").lower().strip()
            
            ctb_version = params_obj.get("ctb_version")
            
            if not target_format:
                update_job(job_id, status="failed", error="uvtools_target_format_missing")
                return {"ok": False, "error": "uvtools_target_format_missing"}
            
            native_ext = target_format if target_format != "ctb" else "ctb"
            native_path = os.path.join(out_dir, f"{file_prefix}.{native_ext}")
            
            params_err = _validate_uvtools_params(params_obj, target_format)
            if params_err:
                update_job(job_id, status="failed", error=f"uvtools_params_invalid: {params_err}")
                return {"ok": False, "error": "uvtools_params_invalid"}
            
            temp_sl1 = os.path.join(out_dir, "temp_for_conversion.sl1")
            if not _create_sl1_from_pngs(slices_dir, params_obj, temp_sl1):
                update_job(job_id, status="failed", error="failed to create temp SL1")
                return {"ok": False, "error": "sl1_creation_failed"}
            
            if target_format == "ctb" and ctb_version:
                cmd2 = f"uvtools-cli convert {shlex.quote(temp_sl1)} Chitubox {shlex.quote(native_path)} --version {ctb_version}"
                log.info(f"Using Chitubox encoder with explicit version: {ctb_version}")
            else:
                encoder_map = {
                    "ctb": "Chitubox",
                    "cbddlp": "Chitubox",
                    "photon": "Chitubox",
                    "phz": "PHZ",
                    "pws": "Anycubic",
                    "pwmx": "Anycubic",
                    "sl1": "SL1",
                    "sl1s": "SL1",
                }
                encoder_name = encoder_map.get(target_format, "ctb")
                cmd2 = f"uvtools-cli convert {shlex.quote(temp_sl1)} {encoder_name} {shlex.quote(native_path)}"
                log.info(f"Using encoder: {encoder_name} for format: {target_format}")

            log.info(f"UVtools convert command: {cmd2}")
            log.info(f"Starting UVtools conversion at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            uvtools_start = time.time()
            rc2, log2 = sh(cmd2, timeout=1800)
            uvtools_elapsed = time.time() - uvtools_start
            log.info(f"UVtools conversion completed in {uvtools_elapsed:.1f}s with rc={rc2}")

            conversion_succeeded = (
            Path(native_path).exists() and
            Path(native_path).stat().st_size > 0 and
            "Done" in (log2 or "")
            )

            if not conversion_succeeded:
                update_job(job_id, status="failed", error=f"uvtools_convert_failed rc={rc2}\n{(log2 or '')[-4000:]}")
                return {"ok": False, "error": "uvtools_convert_failed"}

            log.info(f"UVtools conversion succeeded, now applying printer-specific parameters")

            # Apply printer-specific parameters via property modification
            machine_name = params_obj.get('machine_name', 'ELEGOO Saturn 4 Ultra')
            lift_height = params_obj.get('lift_height_mm', 0.1)
            lift_speed = params_obj.get('lift_speed_mm_s', 0.05)
            retract_speed = params_obj.get('retract_speed_mm_s', 0.05)
            bottom_lift_height = params_obj.get('bottom_lift_height_mm', 0.1)
            bottom_lift_speed = params_obj.get('bottom_lift_speed_mm_s', 0.05)
            bottom_layers = params_obj.get('bottom_layers', 6)

            # Build property modification command
            # UVtools CLI syntax: uvtools-cli set-property <file> <property> <value>
            prop_cmds = [
                f"uvtools-cli set-property {shlex.quote(native_path)} MachineName {shlex.quote(machine_name)}",
                f"uvtools-cli set-property {shlex.quote(native_path)} LiftHeight {lift_height}",
                f"uvtools-cli set-property {shlex.quote(native_path)} LiftSpeed {lift_speed}",
                f"uvtools-cli set-property {shlex.quote(native_path)} RetractSpeed {retract_speed}",
                f"uvtools-cli set-property {shlex.quote(native_path)} BottomLiftHeight {bottom_lift_height}",
                f"uvtools-cli set-property {shlex.quote(native_path)} BottomLiftSpeed {bottom_lift_speed}",
                f"uvtools-cli set-property {shlex.quote(native_path)} BottomLayersCount {bottom_layers}",
            ]

            log.info(f"Applying {len(prop_cmds)} property modifications to CTB")
            for prop_cmd in prop_cmds:
                rc_prop, log_prop = sh(prop_cmd, timeout=60)
                if rc_prop != 0:
                    log.warning(f"Property modification failed (rc={rc_prop}): {prop_cmd}")
                    log.warning(f"Output: {log_prop[:500]}")

            log.info(f"CTB parameter application complete")
            
            if not conversion_succeeded:
                update_job(job_id, status="failed", error=f"uvtools_convert_failed rc={rc2}\n{(log2 or '')[-4000:]}")
                return {"ok": False, "error": "uvtools_convert_failed"}
            log.info(f"UVtools conversion succeeded: {native_path} ({Path(native_path).stat().st_size} bytes)")
            
            log.info("Cleaning up intermediate files to free memory before uploads")
            if os.path.exists(temp_sl1):
                temp_sl1_size = os.path.getsize(temp_sl1) / (1024 * 1024)
                os.remove(temp_sl1)
                log.info(f"Deleted temp SL1 file ({temp_sl1_size:.1f} MB)")
            
            zip_path = os.path.join(out_dir, f"{file_prefix}_layers.zip")
            log.info(f"Creating layers ZIP at {zip_path}")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                for fn in sorted(glob.glob(os.path.join(slices_dir, "*.png"))):
                    z.write(fn, arcname=os.path.basename(fn))
            
            log.info("Starting file uploads...")
            upload_start = time.time()
            
            log.info(f"Uploading native CTB file: {file_prefix}.{native_ext}")
            upload_file("native", f"{job_id}/{file_prefix}.{native_ext}", native_path, "application/octet-stream")
            
            proj = next((f for f in os.listdir(out_dir) if f.endswith(".3mf")), None)
            if proj:
                log.info(f"Uploading 3MF project file: {file_prefix}.3mf")
                upload_file("projects", f"{job_id}/{file_prefix}.3mf", os.path.join(out_dir, proj), "model/3mf")
            
            log.info(f"Uploading layers ZIP: {file_prefix}_layers.zip")
            upload_file("slices", f"{job_id}/{file_prefix}_layers.zip", zip_path, "application/zip")
            
            upload_elapsed = time.time() - upload_start
            log.info(f"All uploads completed in {upload_elapsed:.1f}s")
            
            report = {"native_ext": native_ext, "native_source": "uvtools", "layers": len(glob.glob(os.path.join(slices_dir, "*.png")))}
            
            log.info("Updating job status to succeeded")
            update_job(
                job_id, 
                status="succeeded", 
                report=report, 
                output_native_path=f"native:{job_id}/{file_prefix}.{native_ext}", 
                output_project_path=(f"projects:{job_id}/{file_prefix}.3mf" if proj else None), 
                output_slices_zip_path=f"slices:{job_id}/{file_prefix}_layers.zip", 
                error=None
            )
            log.info("Job status update completed")
            return {"ok": True}
        except Exception as e:
            log.exception("Exception during job processing")
            update_job(job_id, status="failed", error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": str(e)}
        finally:
            shutil.rmtree(wd, ignore_errors=True)
    except Exception as e:
        log.exception("Fatal handler failure at /jobs")
        return JSONResponse({"ok": False, "error": f"fatal: {e}"}, status_code=200)
