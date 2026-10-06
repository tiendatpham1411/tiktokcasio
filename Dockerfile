FROM python:3.10-slim

# Install system dependencies including ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Setup non-root user
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR $HOME/app

# Install python dependencies
COPY --chown=user:user requirements.txt $HOME/app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY --chown=user:user . $HOME/app/

# Expose ports
EXPOSE 7860 10000

# Run FastAPI / Uvicorn server with dynamic port support (Render, HF, or Local)
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-7860}"]

