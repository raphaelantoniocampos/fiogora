# Use python 3.13-slim as base
FROM python:3.13-slim

# Install system dependencies for Selenium / Firefox
RUN apt-get update && apt-get install -y --no-install-recommends \
    firefox-esr \
    wget \
    gnupg \
    libdbus-glib-1-2 \
    libgtk-3-0 \
    libx11-xcb1 \
    xvfb \
    libxt6 \
    libxrender1 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and group
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" appgroup && \
    useradd -l -u "${UID}" -g "${GID}" -m -s /bin/bash appuser

# Set working directory
WORKDIR /app

# Create necessary directories and set ownership
RUN mkdir -p /app/data /app/downloads && \
    chown -R appuser:appgroup /app

# Install uv for dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Switch to non-root user
USER appuser

# Copy dependency files
COPY --chown=appuser:appgroup pyproject.toml uv.lock ./

# Install dependencies (only production)
RUN uv sync --frozen --no-dev

# Copy the rest of the application
COPY --chown=appuser:appgroup . .

# Expose port 8000
EXPOSE 8000

# Command to run the application
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
