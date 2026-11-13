# Intake API (Stage 2, the api channel): the dip package served by uvicorn. Build context
# is the repo root. Base and packages pinned; every version was at least two weeks old
# when it was chosen.
FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

# 24.4: a hashed lock (requirements/intake-api.in, compiled by scripts/lock.py), installed
# with --require-hashes --no-deps --no-build-isolation after the hashed build lock
# (setuptools, packaging), so pip never fetches an unhashed build backend for an sdist.
COPY requirements/build.txt requirements/intake-api.txt /tmp/
RUN pip install --no-cache-dir --require-hashes --no-deps -r /tmp/build.txt \
 && pip install --no-cache-dir --require-hashes --no-deps --no-build-isolation -r /tmp/intake-api.txt \
 && rm /tmp/build.txt /tmp/intake-api.txt \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin api

# --no-deps: the dependencies above are the ones the API needs (openpyxl is not: the API
# never parses a file). --no-build-isolation: the locked setuptools builds it.
COPY pyproject.toml /tmp/dip/pyproject.toml
COPY dip/ /tmp/dip/dip/
RUN pip install --no-cache-dir --no-deps --no-build-isolation /tmp/dip && rm -rf /tmp/dip
COPY packs/ /app/packs/
ENV DIP_CONTRACT_DIRS=/app/packs/healthcare/contracts:/app/packs/finance/contracts

USER 10001
EXPOSE 8000
CMD ["uvicorn", "dip.intake_api:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
