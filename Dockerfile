# Max Brain Server — Railway Dockerfile (Pipecat streaming architecture)
FROM python:3.11-slim

WORKDIR /app

# System dependencies for Pipecat + Silero VAD + protobuf
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (includes Pipecat + Deepgram + Anthropic + Silero)
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# Copy the server package
COPY max/ ./max/

# Railway injects PORT env var — uvicorn binds to it
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn max.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
