FROM python:3.12-alpine

WORKDIR /app
COPY pikpak_nas_worker.py /app/pikpak_nas_worker.py

ENV PIKPAK_NAS_BIND=0.0.0.0
ENV PIKPAK_NAS_PORT=18082
ENV PIKPAK_NAS_DOWNLOAD_ROOT=/downloads
ENV PIKPAK_NAS_STATE_DIR=/state

EXPOSE 18082
CMD ["python", "/app/pikpak_nas_worker.py"]
