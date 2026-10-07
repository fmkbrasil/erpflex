FROM golang:1.23-bookworm AS bridgebuild
WORKDIR /src
COPY go_bridge/erpflex_bridge.go .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /erpflex_v78_bridge erpflex_bridge.go

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ERPFLEX_ENGINE=go_v78
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=bridgebuild /erpflex_v78_bridge /app/bin/erpflex_v78_bridge
RUN chmod +x /app/bin/erpflex_v78_bridge && mkdir -p /data
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
