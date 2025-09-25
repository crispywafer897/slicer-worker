FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
# Prevent GTK from touching the accessibility (AT-SPI) D-Bus (pairs with --no-session-bus)
ENV NO_AT_BRIDGE=1

# Base deps (+ xvfb for headless, GTK/OpenGL libs), Flatpak, D-Bus, and xauth for xvfb-run
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip ca-certificates \
    python3 python3-pip git \
    xvfb xauth \
    libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    flatpak dbus dbus-x11 \
  && rm -rf /var/lib/apt/lists/*

# Add Flathub (SYSTEM scope) and install PrusaSlicer (SYSTEM scope)
RUN flatpak --system remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo \
  && flatpak --system install -y flathub com.prusa3d.PrusaSlicer

# (Optional) prime Flatpak metadata (use --system here too)
RUN flatpak --system info com.prusa3d.PrusaSlicer || true

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
