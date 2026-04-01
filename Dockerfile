# Max Brain Server — Railway Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install server dependencies only (fast build, no pipecat)
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

# Copy the full project (server.py needs max/persona.py + max_avatar.html)
COPY max/ ./max/
COPY max_avatar.html .

# Railway injects PORT env var — uvicorn binds to it
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn max.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
