# Intake API (Stage 2, the api channel): the dip package served by uvicorn. Build context
# is the repo root. Base and packages pinned; every version was at least two weeks old
# when it was chosen.
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN pip install --no-cache-dir "fastapi==0.142.4" "starlette==1.7.0" "uvicorn==0.54.0" \
      "boto3==1.43.109" "pyyaml==6.0.3" "jsonschema==4.26.0" \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin api

# --no-deps: the dependencies above are the ones the API needs (openpyxl is not: the API
# never parses a file).
COPY pyproject.toml /tmp/dip/pyproject.toml
COPY dip/ /tmp/dip/dip/
RUN pip install --no-cache-dir --no-deps /tmp/dip && rm -rf /tmp/dip
COPY packs/ /app/packs/
ENV DIP_CONTRACT_DIRS=/app/packs/healthcare/contracts

USER 10001
EXPOSE 8000
CMD ["uvicorn", "dip.intake_api:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
