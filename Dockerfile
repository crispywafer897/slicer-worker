FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Prevent GTK from probing the accessibility D-Bus (keeps headless runs clean)
ENV NO_AT_BRIDGE=1

# Base deps (+ xvfb/xauth for headless X), GTK/OpenGL libs, and WebKitGTK 4.0 runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip ca-certificates \
    python3 python3-pip git \
    xvfb xauth \
    libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    libwebkit2gtk-4.0-37 \
  && rm -rf /var/lib/apt/lists/*

# --- Download & EXTRACT PrusaSlicer AppImage (bypass FUSE at runtime) ---
# URL you provided (older-distros / WebKit 4.0 build for Ubuntu 22.04)
RUN wget -O /opt/PrusaSlicer.AppImage \
      https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1+linux-x64-older-distros-GTK3-202409181354.AppImage \
  && chmod +x /opt/PrusaSlicer.AppImage \
  # Extract contents of the AppImage into /opt/prusaslicer (creates squashfs-root/)
  && /opt/PrusaSlicer.AppImage --appimage-extract \
  && mv squashfs-root /opt/prusaslicer \
  # Symlink the extracted launcher so we can call 'prusaslicer' directly
  && ln -sf /opt/prusaslicer/AppRun /usr/local/bin/prusaslicer \
  # Optional: delete the original AppImage to save space
  && rm -f /opt/PrusaSlicer.AppImage

# UVtools CLI (pin v5.2.0; bump as needed)
RUN wget -O /tmp/uvtools.zip \
      https://github.com/sn4k3/UVtools/releases/download/v5.2.0/UVtools_linux-x64_v5.2.0.zip \
  && unzip /tmp/uvtools.zip -d /opt/uvtools \
  && ln -s /opt/uvtools/uvtools-cli /usr/local/bin/uvtools-cli

# App
WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app ./app
COPY profiles /profiles

# Cloud Run port
ENV PORT=8080

# Start API (your FastAPI service will invoke the extracted PrusaSlicer via xvfb-run)
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
