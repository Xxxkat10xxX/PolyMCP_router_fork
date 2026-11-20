# Собираем официальный GitHub MCP-сервер из исходников (версия, на которой прогонялся бенчмарк).
FROM golang:1.24-alpine AS github-mcp
ENV GOTOOLCHAIN=auto CGO_ENABLED=0
RUN go install github.com/github/github-mcp-server/cmd/github-mcp-server@v1.14.0

FROM python:3.12-slim
# procps нужен shell-серверу (ps)
RUN apt-get update && apt-get install -y --no-install-recommends procps && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=github-mcp /go/bin/github-mcp-server /usr/local/bin/github-mcp-server
COPY . .
ENV GITHUB_MCP_BIN=/usr/local/bin/github-mcp-server \
    SHELL_SANDBOX_ROOT=/app/sandbox \
    PYTHONUNBUFFERED=1
EXPOSE 8501
CMD ["streamlit", "run", "app_ui.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
