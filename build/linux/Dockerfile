# syntax=docker/dockerfile:1
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04
WORKDIR /app
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libegl1 libxrandr2 libxss1 libxcursor1 libxcomposite1 \
    libasound2 libxi6 libxtst6 curl ffmpeg git nano gnupg2 libsm6 wget unzip \
    libxcb-icccm4 libxkbcommon-x11-0 libxcb-keysyms1 libxcb-render0 \
    libxcb-render-util0 libxcb-image0 libxcb-cursor0 \
    libxcb-xinerama0 libxcb-xinput0 libxcb-xfixes0 libxcb-shape0 \
    libxcb-shm0 libxcb-sync1 libxcb-xkb1 libxcb-util1 \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

COPY build/linux/requirements-gpu.txt /tmp/requirements-gpu.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip && \
    python -m pip install -r /tmp/requirements-gpu.txt

COPY . /app/DeepFaceLive
WORKDIR /app/DeepFaceLive

COPY build/linux/example.sh /app/DeepFaceLive/example.sh
RUN chmod +x /app/DeepFaceLive/example.sh

CMD ["./example.sh"]
