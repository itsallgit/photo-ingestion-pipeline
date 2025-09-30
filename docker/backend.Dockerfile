FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# Install exiftool and basic dependencies
RUN apt-get update && apt-get install -y --no-install-recommends libimage-exiftool-perl ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY backend /app
RUN pip install --no-cache-dir -r requirements.txt
# Create data directories
RUN mkdir -p /app/data/sessions /app/data/logs
VOLUME ["/app/data"]
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
