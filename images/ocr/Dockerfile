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

# Each version was at least two weeks old when chosen.
# pdfplumber's own dependencies are pinned too, so a rebuild cannot move them.
RUN pip install --no-cache-dir "pdfplumber==0.11.10" "pdfminer.six==20260107" \
      "pypdfium2==5.14.0" "pytesseract==0.3.13" "pillow==12.3.0"
