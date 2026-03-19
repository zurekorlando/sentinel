FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for argon2-cffi and zstandard
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "sentinel.cli"]
CMD ["--help"]
