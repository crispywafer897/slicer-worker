# Base runtime
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Prevent GTK from probing the accessibility D-Bus (keeps headless runs clean)
ENV NO_AT_BRIDGE=1
# Where the worker expects to fetch bundles/params at runtime
ENV STORAGE_BUCKET=slicer-presets
# Optional: where we cache bundles/params inside the container between requests
ENV CACHE_DIR=/tmp/preset_cache

# --------------------------------------------------------------------
# System deps:
#  - xvfb/xauth: headless X server for PrusaSlicer GUI runtime
#  - GTK/OpenGL + WebKitGTK 4.0: PrusaSlicer 2.8.x older-distros build
#  - curl/wget/unzip: fetch release artifacts
#  - python3/pip: run FastAPI worker
# --------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip ca-certificates \
    python3 python3-pip git \
    xvfb xauth \
    libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    libwebkit2gtk-4.0-37 \
  && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------
# PrusaSlicer AppImage: download and EXTRACT at build time
# (avoids needing FUSE in Cloud Run). We symlink AppRun to PATH.
# Pin the exact build you tested so CLI flags stay stable.
# --------------------------------------------------------------------
RUN wget -O /opt/PrusaSlicer.AppImage \
      https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1+linux-x64-older-distros-GTK3-202409181354.AppImage \
  && chmod +x /opt/PrusaSlicer.AppImage \
  && /opt/PrusaSlicer.AppImage --appimage-extract \
  && mv squashfs-root /opt/prusaslicer \
  && ln -sf /opt/prusaslicer/AppRun /usr/local/bin/prusaslicer \
  && rm -f /opt/PrusaSlicer.AppImage

# --------------------------------------------------------------------
# Normalize + auto-detect PrusaSlicer datadir and expose a stable path.
# We choose the *actual* resources root (prefer /opt/prusaslicer/resources),
# verify hallmark subfolders, then create a stable alias at:
#   /opt/prusaslicer/usr/share/prusa-slicer  →  <actual resources>
# --------------------------------------------------------------------
RUN set -eux; \
  ROOT="/opt/prusaslicer"; \
  mkdir -p "$ROOT/usr/share"; \
  CANDIDATES="\
    $ROOT/resources \
    $ROOT/Resources \
    $ROOT/usr/share/prusa-slicer \
    $ROOT/usr/share/PrusaSlicer \
    $ROOT/share/prusa-slicer \
    $ROOT/share/PrusaSlicer \
    $ROOT/usr/lib/PrusaSlicer/resources \
    $ROOT/usr/lib64/PrusaSlicer/resources \
  "; \
  DATADIR=""; \
  for d in $CANDIDATES; do \
    if [ -d "$d" ]; then \
      markers=0; \
      [ -d "$d/profiles" ] && markers=$((markers+1)); \
      [ -d "$d/vendor" ]   && markers=$((markers+1)); \
      [ -d "$d/shaders" ]  && markers=$((markers+1)); \
      if [ $markers -ge 2 ]; then \
        DATADIR="$d"; \
        echo "Chosen datadir (marker-validated): $d"; \
        break; \
      fi; \
    fi; \
  done; \
  if [ -z "$DATADIR" ]; then \
    echo "Scanning for resources under $ROOT ..."; \
    # Find a directory named resources/Resources/prusa-slicer/PrusaSlicer that contains at least 2 markers
    while IFS= read -r d; do \
      markers=0; \
      [ -d "$d/profiles" ] && markers=$((markers+1)); \
      [ -d "$d/vendor" ]   && markers=$((markers+1)); \
      [ -d "$d/shaders" ]  && markers=$((markers+1)); \
      if [ $markers -ge 2 ]; then DATADIR="$d"; echo "Chosen datadir (scanned): $d"; break; fi; \
    done <<EOF
$(find "$ROOT" -maxdepth 5 -type d \( -name resources -o -name Resources -o -name prusa-slicer -o -name PrusaSlicer \))
EOF \
  ; fi; \
  if [ -z "$DATADIR" ]; then \
    echo "ERROR: Could not locate PrusaSlicer datadir under $ROOT"; \
    find "$ROOT" -maxdepth 4 -type d -print || true; \
    exit 1; \
  fi; \
  ln -sfn "$DATADIR" "$ROOT/usr/share/prusa-slicer"; \
  echo "Stable alias → $ROOT/usr/share/prusa-slicer → $(readlink -f $ROOT/usr/share/prusa-slicer)"; \
  ls -la "$ROOT/usr/share"; \
  # Light runtime check to ensure this datadir boots
  xvfb-run -a -s '-screen 0 1024x768x24' /usr/local/bin/prusaslicer --datadir "$ROOT/usr/share/prusa-slicer" --help >/dev/null

# Tell the app exactly where PrusaSlicer’s resources live (stable alias)
ENV PRUSA_DATADIR=/opt/prusaslicer/usr/share/prusa-slicer

# Build-time sanity check to fail early if the folder disappears
RUN test -d "${PRUSA_DATADIR}" || (ls -laR /opt/prusaslicer/usr/share && exit 1)

# --------------------------------------------------------------------
# UVtools CLI
# - Using the official prebuilt zip. Symlink uvtools-cli into PATH.
# --------------------------------------------------------------------
RUN wget -O /tmp/uvtools.zip \
      https://github.com/sn4k3/UVtools/releases/download/v5.2.0/UVtools_linux-x64_v5.2.0.zip \
  && unzip /tmp/uvtools.zip -d /opt/uvtools \
  && ln -s /opt/uvtools/uvtools-cli /usr/local/bin/uvtools-cli \
  && rm -f /tmp/uvtools.zip

# --------------------------------------------------------------------
# Python deps & app
# --------------------------------------------------------------------
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy only application code (no local profiles — presets are fetched at runtime)
COPY app ./app

# Create the cache dir for preset bundles/params (ephemeral on Cloud Run instances)
RUN mkdir -p ${CACHE_DIR}

# Cloud Run port
ENV PORT=8080

# Start API (FastAPI service invokes PrusaSlicer via xvfb-run)
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
