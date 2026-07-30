# MyFitnessPal MCP Server
#
# Transport is selected at runtime via MFP_TRANSPORT (unset/"stdio" default,
# "streamable-http" for a hosted deployment -- see README "Cloud Deployment").
#
# Local stdio (mounts a browser's cookie database for auth):
#   docker build -t mfp-mcp .
#   docker run -it --rm -v ~/.config/google-chrome:/root/.config/google-chrome:ro mfp-mcp
#
# Hosted streamable-http (auth via MFP_COOKIES_JSON instead -- no browser):
#   docker run -p 8000:8000 \
#     -e MFP_TRANSPORT=streamable-http -e MFP_AUTH_TOKEN=... -e MFP_PUBLIC_HOST=... \
#     -e MFP_COOKIES_JSON="$(cat ~/.mfp_mcp/cookies.json)" mfp-mcp

FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install the package
RUN pip install --no-cache-dir -e .

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash mcp
USER mcp

# Port used when MFP_TRANSPORT=streamable-http (Railway overrides via $PORT)
EXPOSE 8000

# main() picks stdio vs streamable-http from MFP_TRANSPORT at runtime
ENTRYPOINT ["python", "-m", "mfp_mcp.server"]
