FROM python:3.12-slim

# VAAPI drivers let FFmpeg decode/deinterlace/encode on the Intel iGPU instead
# of burning a CPU core per stream. intel-media-va-driver covers Broadwell and
# newer, i965-va-driver the older parts; libva picks whichever matches the GPU.
# Both live in Debian's non-free component, hence the extra source line.
RUN echo "deb http://deb.debian.org/debian bookworm main non-free non-free-firmware" \
        > /etc/apt/sources.list.d/non-free.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        git ffmpeg intel-media-va-driver i965-va-driver vainfo \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
