#!/bin/sh
# Rebuilds the ESP32-C3 modem firmware in this folder from esp32c3_modem/, with ESP-IDF 5.4.1 in Docker.
# Run it after a change to esp32c3_modem/ and commit the result: the dashboard flashes these files.
set -e
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
cp -R esp32c3_modem/CMakeLists.txt esp32c3_modem/main esp32c3_modem/sdkconfig.defaults "$tmp/"
docker run --rm -v "$tmp:/fw" -w /fw espressif/idf:v5.4.1 sh -c "idf.py set-target esp32c3 && idf.py build"
cp "$tmp/build/pictochat_modem.bin" firmware/
cp "$tmp/build/bootloader/bootloader.bin" firmware/bootloader/
cp "$tmp/build/partition_table/partition-table.bin" firmware/partition_table/
git rev-parse --short HEAD > firmware/BUILT_FROM 2>/dev/null || true
echo "firmware/ updated"
