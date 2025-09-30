# =========================
# Base runtime
# =========================
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Prevent GTK from probing the accessibility D-Bus (keeps headless runs clean)
ENV NO_AT_BRIDGE=1

# Where the worker expects to fetch bundles/params at runtime
ENV STORAGE_BUCKET=slicer-presets
# Where we cache bundles/params inside the container between requests
ENV CACHE_DIR=/tmp/preset_cache

# A dedicated, writable HOME for headless PrusaSlicer/User config
ENV PS_HOME=/tmp/pshome
ENV HOME=${PS_HOME}
ENV XDG_CONFIG_HOME=${PS_HOME}/.config
ENV XDG_CACHE_HOME=${PS_HOME}/.cache

# =========================
# System deps
#  - xvfb/xauth: headless X server for PrusaSlicer GUI runtime
#  - GTK/OpenGL + WebKitGTK 4.0: PrusaSlicer 2.8.x older-distros build
#  - curl/wget/unzip: fetch release artifacts
#  - python3/pip: run FastAPI worker
# =========================
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip ca-certificates \
    python3 python3-pip git \
    xvfb xauth \
    libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    libwebkit2gtk-4.0-37 \
  && rm -rf /var/lib/apt/lists/*

# Make sure our writable HOME exists
RUN mkdir -p "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" "${CACHE_DIR}" && chmod -R 777 "${PS_HOME}"

# =========================
# PrusaSlicer AppImage
# - Download and EXTRACT at build time (no FUSE on Cloud Run)
# - We run the extracted AppDir's AppRun via /usr/local/bin/prusaslicer
# - IMPORTANT: we DO NOT set or pass --datadir (we let PS use ${HOME})
# =========================
RUN wget -O /opt/PrusaSlicer.AppImage \
      https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1+linux-x64-older-distros-GTK3-202409181354.AppImage \
  && chmod +x /opt/PrusaSlicer.AppImage \
  && /opt/PrusaSlicer.AppImage --appimage-extract \
  && mv squashfs-root /opt/prusaslicer \
  && ln -sf /opt/prusaslicer/AppRun /usr/local/bin/prusaslicer \
  && rm -f /opt/PrusaSlicer.AppImage

# Optional: tiny smoke test that does NOT fixate a datadir.
# Uses a temp throwaway HOME so we don't bake configs into the image layer.
RUN PS_TMP=/tmp/ps_check && mkdir -p "$PS_TMP/.config" "$PS_TMP/.cache" \
  && env HOME="$PS_TMP" XDG_CONFIG_HOME="$PS_TMP/.config" XDG_CACHE_HOME="$PS_TMP/.cache" \
     xvfb-run -a -s '-screen 0 1024x768x24' prusaslicer --help-sla >/dev/null \
  && rm -rf "$PS_TMP"

# =========================
# UVtools CLI (prebuilt)
# =========================
RUN wget -O /tmp/uvtools.zip \
      https://github.com/sn4k3/UVtools/releases/download/v5.2.0/UVtools_linux-x64_v5.2.0.zip \
  && unzip /tmp/uvtools.zip -d /opt/uvtools \
  && ln -s /opt/uvtools/uvtools-cli /usr/local/bin/uvtools-cli \
  && rm -f /tmp/uvtools.zip

# =========================
# Python deps & app
# =========================
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy only application code (presets are fetched at runtime)
COPY app ./app

# Cloud Run port
ENV PORT=8080

# =========================
# Start API (FastAPI service invokes PrusaSlicer via xvfb-run)
# NOTE: Do NOT pass --datadir in your worker; rely on ${HOME} (PS_HOME)
# =========================
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
