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

# Ensure /usr/local/bin precedes others so our UVtools wrapper is found by PATH checks
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# .NET & globalization hardening for UVtools CLI (avoids rc=255 on some images)
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

# =========================
# System deps
# =========================
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip ca-certificates \
    python3 python3-pip git \
    xvfb xauth \
    libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    libwebkit2gtk-4.0-37 \
    libgdiplus libunwind8 libicu70 libfontconfig1 \
    libx11-6 libx11-xcb1 libxcb1 \
  && rm -rf /var/lib/apt/lists/*

# Make sure our writable HOME exists
RUN mkdir -p "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" "${CACHE_DIR}" && chmod -R 777 "${PS_HOME}"

# =========================
# PrusaSlicer AppImage
# =========================
RUN wget -O /opt/PrusaSlicer.AppImage \
      https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1+linux-x64-older_
