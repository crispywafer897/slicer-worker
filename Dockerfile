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

# Ensure a stable, lowercase datadir path exists and verify it.
# Some builds ship .../share/PrusaSlicer (capital P/S). If that's the case,
# create a lowercase alias so PRUSA_DATADIR can be consistent.
RUN set -eux; \
    if [ -d /opt/prusaslicer/usr/share/prusa-slicer ]; then \
      echo "Found datadir: /opt/prusaslicer/usr/share/prusa-slicer"; \
    elif [ -d /opt/prusaslicer/usr/share/PrusaSlicer ]; then \
      echo "Found datadir: /opt/prusaslicer/usr/share/PrusaSlicer (capitalized)"; \
      ln -s /opt/prusaslicer/usr/share/PrusaSlicer /opt/prusaslicer/usr/share/prusa-slicer; \
      echo "Created symlink → /opt/prusaslicer/usr/share/prusa-slicer"; \
    else \
      echo "ERROR: Could not locate PrusaSlicer datadir under /opt/prusaslicer/usr/share"; \
      (ls -laR /opt/prusaslicer/usr/share || true); \
      exit 1; \
    fi

# Tell the app exactly where PrusaSlicer’s resources live
ENV PRUSA_DATADIR=/opt/prusaslicer/usr/share/prusa-slicer

# Build-time sanity check to fail early if the folder disappears
RUN test -d "${PRUSA_DATADIR}" || (ls -laR /opt/prusaslicer/usr/share && exit 1)

# --------------------------------------------------------------------
# UVtools CLI
# - Using the official prebuilt zip. Symlink uvtools-cli into PATH.
# - If you prefer the .NET global tool, swap this block accordingly.
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
