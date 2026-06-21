FROM python:3.11-slim

# git: required by app/retrieval/workspace.py (worktree management)
# build-essential: tree-sitter has a native extension build step
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
