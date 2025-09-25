import os, subprocess, shutil, json, tempfile, zipfile, urllib.request, glob, shlex
from typing import Dict, Any, Optional

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from supabase import create_client, Client

# ---------- Env (no crash on import) ----------
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
WORKER_TOKEN = os.getenv("WORKER_TOKEN", "")

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

# ---------- PrusaSlicer runner helpers (Flatpak in headless container) ----------
PRUSA_FLATPAK_APP = "com.prusa3d.PrusaSlicer"

def run_prusaslicer_headless(args: list[str]) -> tuple[int, str]:
    """
    Start a private D-Bus session + Xvfb, run PrusaSlicer Flatpak with full FS access,
    then tear everything down. Returns (rc, combined_output).
    This avoids the 'Could not connect: No such file or directory' error in containers.
    """
    # Ensure an XDG runtime dir; D-Bus often expects it
    env = os.environ.copy()
    xdg_runtime = env.get("XDG_RUNTIME_DIR", "/tmp/xdg-runtime")
    os.makedirs(xdg_runtime, exist_ok=True)
    try:
        os.chmod(xdg_runtime, 0o700)
    except Exception:
        pass
    env["XDG_RUNTIME_DIR"] = xdg_runtime

    # Start a dedicated session bus
    # We capture both the bus address and PID so we can clean up.
    dbus_cmd = ["dbus-daemon", "--session", "--print-address=1", "--print-pid=1", "--fork"]
    try:
        # Run dbus-daemon and capture stdout (two lines: address, pid)
        out = subprocess.check_output(dbus_cmd, env=env, text=True)
    except Exception as e:
        return 1, f"Failed to start dbus-daemon: {e}\n(Check that the 'dbus' package is installed.)"

    lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return 1, f"dbus-daemon did not print address and pid; got: {out!r}"
    dbus_addr, dbus_pid = lines[0], lines[1]
    env["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr

    # Compose PrusaSlicer command
    # - xvfb-run provides a virtual X for GTK/OpenGL init (even with --no-gui)
    # - --filesystem=host lets Flatpak see /profiles, /tmp, and our working dir
    base = [
        "xvfb-run", "-a", "-s", "-screen 0 1024x768x24",
        "flatpak", "run", "--branch=stable", "--filesystem=host", PRUSA_FLATPAK_APP
    ]
    # Join into a single shell-safe command for logging and our sh() helper
    cmd = " ".join(shlex.quote(x) for x in base + args)

    try:
        rc, log = sh(cmd, env=env)
    finally:
        # Tear down the session bus we started
        try:
            subprocess.run(["kill", "-9", dbus_pid], check=False)
        except Exception:
            pass

    return rc, log

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
                raise ValueError(f"input_path must be 'bucket:path', got: {in_spec}")
            bucket, path = in_spec.split(":", 1)
            # Keep name generic; Prusa can import STL/3MF
            input_model = os.path.join(wd, "model.input")
            signed_download(bucket, path, input_model)

            # 2) Profiles
            printer_id     = job["printer_id"]
            print_profile  = job["print_profile"]
            printer_json   = f"/profiles/printers/{printer_id}.json"
            prusa_ini      = f"/profiles/resin_presets/{'0.05mm_standard.ini' if print_profile == '0.05mm_standard' else print_profile + '.ini'}"
            uvtools_params = f"/profiles/uvtools_params/{printer_id}_std_resin.json"

            for req_path, label in [(printer_json, "printer profile"),
                                    (prusa_ini, "resin preset"),
                                    (uvtools_params, "uvtools params")]:
                if not os.path.exists(req_path):
                    raise FileNotFoundError(f"missing {label}: {req_path}")

            out_dir = os.path.join(wd, "out")
            os.makedirs(out_dir, exist_ok=True)

            # 3) Slice with PrusaSlicer (Flatpak + Xvfb + private D-Bus session), with full FS access
            slices_target = os.path.join(out_dir, "slices")
            os.makedirs(slices_target, exist_ok=True)

            # Build base Prusa CLI (headless export)
            base_args = [
                "--no-gui", "--export-sla", "--export-3mf",
                "--load", prusa_ini,
                "--output", out_dir,
            ]

            # We try explicit SLA dir first, then fallback
            cmd_variants = [
                base_args + ["--sla-output", slices_target, input_model],
                base_args + [input_model],
            ]

            rc1, log1 = -1, ""
            for argv in cmd_variants:
                rc1, log1 = run_prusaslicer_headless(argv)
                if rc1 == 0:
                    break

            if rc1 != 0:
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
                    _supabase().storage.from_(bucket).upload(path, f, {"content-type": ctype, "upsert": True})

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
