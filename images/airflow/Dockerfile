# Platform Airflow image: the official image plus our DAGs, the `dip` package, the domain
# packs (contracts) and the dbt project. Build context is the repo root.
FROM apache/airflow:3.2.2-python3.12

# --no-deps: pyyaml, jsonschema and boto3 already ship in the official image at versions
# Airflow's constraints pin, and we must not move them.
COPY --chown=airflow:root pyproject.toml /tmp/dip/pyproject.toml
COPY --chown=airflow:root dip/ /tmp/dip/dip/
RUN pip install --no-cache-dir --no-deps /tmp/dip
# The agents (Stage 9, S9.1: the agent_chain DAG) in their own venv, like dbt below: the
# Anthropic SDK, the MCP client and LangGraph never touch Airflow's dependencies. The
# same pins as pyproject.toml's dev extra and images/mcp.
RUN python -m venv /opt/airflow/agent-venv  && /opt/airflow/agent-venv/bin/pip install --no-cache-dir /tmp/dip "anthropic==1.12.0"       "mcp==2.3.0" "langgraph==1.2.14" "langchain-core==1.6.9" "langgraph-checkpoint==4.2.0"       "langgraph-prebuilt==1.1.0" "langgraph-sdk==0.4.6" "psycopg[binary]==3.3.6"       "httpx==0.28.1"  && rm -rf /tmp/dip
COPY --chown=airflow:root agents/prompts/ /opt/airflow/agents/prompts/
COPY --chown=airflow:root config/llm.toml /opt/airflow/config/llm.toml
ENV DIP_PROMPT_DIR=/opt/airflow/agents/prompts     DIP_LLM_CONFIG=/opt/airflow/config/llm.toml
# Excel contracts (Stage 2). Not in the official image; pure Python, one dependency (et-xmlfile).
RUN pip install --no-cache-dir "openpyxl==3.1.5" "et-xmlfile==2.0.0"

COPY --chown=airflow:root packs/ /opt/airflow/packs/
ENV DIP_CONTRACT_DIRS=/opt/airflow/packs/healthcare/contracts:/opt/airflow/packs/finance/contracts

# dbt in its own venv so its dependencies never touch Airflow's.
RUN python -m venv /opt/airflow/dbt-venv \
 && /opt/airflow/dbt-venv/bin/pip install --no-cache-dir "dbt-core==1.12.6" "dbt-postgres==1.11.0"
COPY --chown=airflow:root dbt/ /opt/airflow/dbt/
ENV DIP_DBT_DIR=/opt/airflow/dbt \
    DIP_DBT_BIN=/opt/airflow/dbt-venv/bin/dbt \
    DIP_CACHE_DIR=/tmp/dip-cache \
    DBT_LOG_PATH=/tmp/dbt-logs \
    DBT_TARGET_PATH=/tmp/dbt-target

COPY --chown=airflow:root dags/ /opt/airflow/dags/
