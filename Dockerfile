FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Base deps (+ xvfb, GTK/OpenGL libs), Flatpak, DBus
RUN apt-get update && apt-get install -y \
    wget curl unzip ca-certificates \
    python3 python3-pip git \
    xvfb libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    flatpak dbus dbus-x11 \
 && rm -rf /var/lib/apt/lists/*

# Add Flathub and install PrusaSlicer (Flatpak)
RUN flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo \
 && flatpak install -y flathub com.prusa3d.PrusaSlicer

# UVtools CLI (v5.2.0)
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

# (Optional) prime Flatpak metadata (non-fatal)
RUN flatpak info com.prusa3d.PrusaSlicer || true

# Serve the FastAPI app
ENV PORT=8080
CMD ["python3","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
