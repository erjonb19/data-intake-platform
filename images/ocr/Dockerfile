# OCR task image (Stage 3, S3.1): the platform Airflow image plus Tesseract and the PDF
# readers. Only the PDF feed's intake task runs in it (a pod override in
# dags/feed_intake.py); everything else keeps the smaller Airflow image. Nothing here is
# installed on the laptop. Build context: the repo root; up.sh passes BASE.
ARG BASE=dip-airflow:build
FROM ${BASE}

USER root
# Debian's Tesseract 5 with the English model; no other language packs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*
USER airflow

# Each version was at least two weeks old when chosen. Pillow 13.0.0 (2026-10-16) fixes
# the CRITICAL findings the 12.9 Trivy scan reported in 12.3.0 (CVE-2026-112393 and others).
# 24.4: a hashed lock of the whole tree (requirements/ocr.in), resolved against Airflow's
# constraints so it never moves a package Airflow ships.
COPY --chown=airflow:root requirements/ocr.txt /tmp/ocr.txt
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/ocr.txt && rm /tmp/ocr.txt
