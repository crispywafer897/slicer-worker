# ... (everything above unchanged)

            # 3) Slice with PrusaSlicer (Flatpak + Xvfb + DBus). Try explicit SLA output dir, then fallback.
            slices_target = os.path.join(out_dir, "slices")
            os.makedirs(slices_target, exist_ok=True)

            cmd1_options = [
                # Preferred: explicit SLA output directory (supported on many builds)
                (
                    'dbus-run-session -- '
                    'xvfb-run -a -s "-screen 0 1024x768x24" '
                    'flatpak run --command=PrusaSlicer com.prusa3d.PrusaSlicer '
                    f'--no-gui --export-sla --export-3mf --load "{prusa_ini}" '
                    f'--output "{out_dir}" --sla-output "{slices_target}" "{input_model}"'
                ),
                # Fallback: let PrusaSlicer decide, we’ll auto-discover slice folder
                (
                    'dbus-run-session -- '
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
                update_job(job_id, status="failed", error=f"prusaslicer_failed rc={rc1}\n{log1[-4000:]}")
                return {"ok": False, "error": "prusaslicer_failed"}

            # 3b) Find where PNG layers actually went (unchanged)
            slices_dir = find_layers(out_dir)
            if not slices_dir:
                update_job(job_id, status="failed",
                           error=f"no_slices_found in {out_dir}. Prusa log tail:\n{log1[-4000:]}")
                return {"ok": False, "error": "no_slices_found"}

# ... (everything below unchanged)
