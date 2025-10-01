# =========================
# Base runtime
# =========================
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV NO_AT_BRIDGE=1

ENV STORAGE_BUCKET=slicer-presets
ENV CACHE_DIR=/tmp/preset_cache

ENV PS_HOME=/tmp/pshome
ENV HOME=${PS_HOME}
ENV XDG_CONFIG_HOME=${PS_HOME}/.config
ENV XDG_CACHE_HOME=${PS_HOME}/.cache

ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=0
ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

# =========================
# System deps + .NET Runtime
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

# Install .NET 8 Runtime (required for UVtools)
RUN wget https://dot.net/v1/dotnet-install.sh -O /tmp/dotnet-install.sh \
  && chmod +x /tmp/dotnet-install.sh \
  && /tmp/dotnet-install.sh --channel 8.0 --runtime dotnet --install-dir /usr/share/dotnet \
  && ln -s /usr/share/dotnet/dotnet /usr/bin/dotnet \
  && rm /tmp/dotnet-install.sh

RUN mkdir -p "${XDG_CONFIG_HOME}" "${XDG_CACHE_HOME}" "${CACHE_DIR}" && chmod -R 777 "${PS_HOME}"

# =========================
# PrusaSlicer AppImage
# =========================
RUN wget -O /opt/PrusaSlicer.AppImage \
      https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1+linux-x64-older-distros-GTK3-202409181354.AppImage \
  && chmod +x /opt/PrusaSlicer.AppImage \
  && /opt/PrusaSlicer.AppImage --appimage-extract \
  && mv squashfs-root /opt/prusaslicer \
  && ln -sf /opt/prusaslicer/AppRun /usr/local/bin/prusaslicer \
  && rm -f /opt/PrusaSlicer.AppImage

RUN PS_TMP=/tmp/ps_check && mkdir -p "$PS_TMP/.config" "$PS_TMP/.cache" \
  && env HOME="$PS_TMP" XDG_CONFIG_HOME="$PS_TMP/.config" XDG_CACHE_HOME="$PS_TMP/.cache" \
     xvfb-run -a -s '-screen 0 1024x768x24' prusaslicer --help-sla >/dev/null \
  && rm -rf "$PS_TMP"

# =========================
# UVtools CLI
# =========================
ARG UVTOOLS_VERSION=v5.2.1
ARG UVTOOLS_ZIP_URL=https://github.com/sn4k3/UVtools/releases/download/v5.2.1/UVtools_linux-x64_v5.2.1.zip

RUN set -eux; \
    wget -O /tmp/uvtools.zip "${UVTOOLS_ZIP_URL}"; \
    mkdir -p /opt/uvtools; \
    unzip /tmp/uvtools.zip -d /opt/uvtools; \
    rm -f /tmp/uvtools.zip; \
    chmod +x /opt/uvtools/UVtools 2>/dev/null || true; \
    chmod +x /opt/uvtools/uvtools 2>/dev/null || true; \
    chmod +x /opt/uvtools/UVtools.CLI 2>/dev/null || true; \
    echo "=== UVtools contents ===" && ls -lah /opt/uvtools/ && echo "======================="

# Create wrapper that uses the CLI tool (UVtoolsCmd)
RUN printf '%s\n' \
      '#!/bin/bash' \
      'set -e' \
      'UVTOOLS_DIR="/opt/uvtools"' \
      '# Try the CLI DLL first (most reliable)' \
      'if [ -f "$UVTOOLS_DIR/UVtoolsCmd.dll" ]; then' \
      '  exec dotnet "$UVTOOLS_DIR/UVtoolsCmd.dll" "$@"' \
      'fi' \
      '# Fall back to CLI executable' \
      'if [ -x "$UVTOOLS_DIR/UVtoolsCmd" ]; then' \
      '  exec "$UVTOOLS_DIR/UVtoolsCmd" "$@"' \
      'fi' \
      '# Last resort: try UVtools.dll' \
      'if [ -f "$UVTOOLS_DIR/UVtools.dll" ]; then' \
      '  exec dotnet "$UVTOOLS_DIR/UVtools.dll" "$@"' \
      'fi' \
      'echo "ERROR: Could not find UVtoolsCmd.dll or executable" >&2' \
      'ls -la "$UVTOOLS_DIR" | grep -i uvtools >&2' \
      'exit 127' \
      > /usr/local/bin/uvtools-cli && \
    chmod +x /usr/local/bin/uvtools-cli && \
    ln -sf /usr/local/bin/uvtools-cli /usr/local/bin/uvtools

# Test UVtools installation
RUN uvtools-cli --version || echo "WARNING: uvtools test failed, will debug at runtime"

# =========================
# Python deps & app
# =========================
WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PORT=8080

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
