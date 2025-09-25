FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# base deps
RUN apt-get update && apt-get install -y \
    wget curl unzip ca-certificates xvfb libgtk-3-0 libgl1 libglu1-mesa libxrender1 \
    python3 python3-pip git && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y flatpak && rm -rf /var/lib/apt/lists/*
# add flathub remote
RUN flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
# install PrusaSlicer (non-interactive)
RUN flatpak install -y flathub com.prusa3d.PrusaSlicer

# Run it via flatpak-spawn (needs dbus/workarounds) – CLI flags can differ:
# e.g. flatpak run com.prusa3d.PrusaSlicer --help


# UVtools CLI — replace "vA.B.C" with the latest release
RUN wget -O /tmp/uvtools.zip https://github.com/sn4k3/UVtools/releases/download/v5.2.0/UVtools_linux-x64_v5.2.0.zip \
 && unzip /tmp/uvtools.zip -d /opt/uvtools \
 && ln -s /opt/uvtools/uvtools-cli /usr/local/bin/uvtools-cli
# UVtools supports packing to many ChiTu-based formats (.ctb v2/v3, .cbddlp, .photon). :contentReference[oaicite:2]{index=2}

# app
WORKDIR /app
COPY requirements.txt ./
RUN pip3 install -r requirements.txt
COPY app ./app
COPY profiles /profiles

# Serve the FastAPI app
ENV PORT=8080
CMD ["python3","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
