# Word Party image for Render (or any Docker host)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api ./api
COPY public ./public

# Render gives the port in $PORT. ONE worker only: rooms live in memory.
ENV PORT=10000
EXPOSE 10000
CMD uvicorn api.index:app --host 0.0.0.0 --port ${PORT}
