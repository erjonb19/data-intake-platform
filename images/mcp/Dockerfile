# The MCP server (Stage 3, S3.4: the Read tier, deployed): the dip package and the official
# MCP SDK, served by uvicorn (python -m dip mcp-serve). Build context is the repo root. Base
# and packages pinned; every version was at least two weeks old when it was chosen. No
# Anthropic SDK: the server never calls a model, agents do. The same image serves the
# approvals page (Stage 9, S9.2: python -m dip approvals-serve, FastAPI, its own login).
FROM python:3.12.14-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN pip install --no-cache-dir "mcp==2.3.0" "fastapi==0.142.4" "mcp-types==2.3.0" "starlette==1.7.0" \
      "uvicorn==0.54.0" "httpx==0.28.1" "pydantic==2.14.1" "anyio==4.15.1" \
      "sse-starlette==3.5.0" "psycopg[binary]==3.3.6" "boto3==1.43.116" "pyyaml==6.0.3" \
      "jsonschema==4.26.0" "openpyxl==3.1.5" "et-xmlfile==2.0.0" \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp

COPY pyproject.toml /tmp/dip/pyproject.toml
COPY dip/ /tmp/dip/dip/
RUN pip install --no-cache-dir --no-deps /tmp/dip && rm -rf /tmp/dip
COPY packs/ /app/packs/
COPY agents/policies/ /app/agents/policies/
COPY docs/runbooks/ /app/docs/runbooks/
ENV DIP_CONTRACT_DIRS=/app/packs/healthcare/contracts:/app/packs/finance/contracts \
    DIP_POLICY_DIR=/app/agents/policies \
    DIP_RUNBOOK_DIR=/app/docs/runbooks

USER 10001
EXPOSE 8765
CMD ["python", "-m", "dip", "mcp-serve", "--host", "0.0.0.0", "--port", "8765"]
