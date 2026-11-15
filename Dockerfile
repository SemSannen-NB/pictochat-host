# PictoChat dashboard + host + ESP32-C3 flasher in one container. See docker-compose.yml.
FROM python:3.13-alpine
RUN pip install --no-cache-dir pyserial==3.5 esptool==5.6.0
WORKDIR /app
COPY pictochat.py dashboard.py dashboard.html ./
COPY firmware ./firmware
ENV PYTHONUNBUFFERED=1
EXPOSE 8080 7777
CMD ["python", "dashboard.py"]
