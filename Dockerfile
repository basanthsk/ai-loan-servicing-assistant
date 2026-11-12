# One image for every component; docker-compose.yml picks the command.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1

RUN pip install --no-cache-dir "poetry>=2.0,<3"

WORKDIR /app
COPY pyproject.toml poetry.lock poetry.toml ./
RUN poetry install --only main --no-root

COPY . .
ENV PATH="/app/.venv/bin:$PATH"

# Bake the embedding model and the policy search index into the image, so the first
# policy question does not wait for a download.
RUN python -c "from core.knowledge import get_knowledge_base as g; g()"
