FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Keep GTK from probing accessibility D-Bus in headless mode
ENV NO_AT_BRIDGE=1

# Base deps (+ xvfb/xauth for headless X), GTK/OpenGL libs, WebKitGTK 4.0, and libfuse2
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip ca-certificates \
    python3 python3-pip git \
    xvfb xauth \
    libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    libwebkit2gtk-4.0-37 \
    libfuse2 \
 && rm -rf /var/lib/apt/lists/*

# --- Download PrusaSlicer AppImage (2.8.1, older-distros / WebKit 4.0) and EXTRACT it ---
# Extracting avoids the FUSE requirement at runtime.
RUN wget -O /opt/PrusaSlicer.AppImage \
      https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1+linux-x64-older-distros-GTK3-202409181354.AppImage \
  && chmod +x /opt/PrusaSlicer.AppImage \
  && /opt/PrusaSlicer.AppImage --appimage-extract -q -C /opt \
  && ln -s /opt/squashfs-root/AppRun /usr/local/bin/prusaslicer

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

# Start API
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
