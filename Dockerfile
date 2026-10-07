# =============================================================================
# Stage 1: Build React frontend (Vite)
# =============================================================================
FROM node:20-slim AS frontend-build

# Declare build-time proxy arguments (passed dynamically from Cloud Build)
ARG HTTP_PROXY="http://internet.gcp.ford.com:83"
ARG HTTPS_PROXY="http://internet.gcp.ford.com:83"
ARG NO_PROXY="127.0.0.1,0.0.0.0,::1,localhost,169.254.169.254,metadata,metadata.google.internal,.ford.com,.local,.testing,.internal,.googleapis.com,.google.internal,19.0.0.0/8,136.1.0.0/16,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

# Set environment variables for Stage 1 build
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    no_proxy=${NO_PROXY}

WORKDIR /app/frontend

# Copy package files first — cached unless dependencies change
COPY frontend/package*.json ./

# Exclude node_modules in .dockerignore to prevent local environment leaks
RUN npm install

# Copy source and build (outputs to build/ in your setup)
COPY frontend/ ./
RUN npm run build


# =============================================================================
# Stage 2: Production (Python + Chrome + SeleniumBase)
# =============================================================================
FROM python:3.12-slim

WORKDIR /app

# Re-declare build-time proxy arguments for Stage 2
ARG HTTP_PROXY="http://internet.gcp.ford.com:83"
ARG HTTPS_PROXY="http://internet.gcp.ford.com:83"
ARG NO_PROXY="127.0.0.1,0.0.0.0,::1,localhost,169.254.169.254,metadata,metadata.google.internal,.ford.com,.local,.testing,.internal,.googleapis.com,.google.internal,19.0.0.0/8,136.1.0.0/16,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

# Dynamically apply the proxy settings so they don't override Cloud Build values
ENV HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY} \
    no_proxy=${NO_PROXY}

# -----------------------------------------------------------------------------
# System dependencies + Xvfb (virtual framebuffer) for headless Chrome
# -----------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    curl \
    unzip \
    xvfb \
    xauth \
    dbus-x11 \
    python3-tk \
    python3-dev \
    libx11-xcb1 \
    x11-utils \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    fonts-liberation \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Install official Google Chrome Stable
# -----------------------------------------------------------------------------
RUN curl -fsSL https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/googlechrome-keyring.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/googlechrome-keyring.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && \
    apt-get install -y google-chrome-stable --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

# -----------------------------------------------------------------------------
# Selenium / display environment
# -----------------------------------------------------------------------------
ENV DISPLAY=:99 \
    SE_OFFLINE=true \
    SE_AVOID_BROWSER_DOWNLOAD=true

# -----------------------------------------------------------------------------
# Python dependencies
# -----------------------------------------------------------------------------
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt gunicorn

# Pre-fetch matching chromedriver (must run after pip install so seleniumbase is available)
RUN seleniumbase get chromedriver --path || true

# -----------------------------------------------------------------------------
# Application code
# -----------------------------------------------------------------------------
COPY backend/ ./backend/

# Copy built frontend from stage 1
COPY --from=frontend-build /app/frontend/build /app/frontend/dist

# Runtime directories for uploads / output
RUN mkdir -p /tmp/uploads /tmp/output

# -----------------------------------------------------------------------------
# Runtime environment
# -----------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    UPLOAD_FOLDER=/tmp/uploads \
    OUTPUT_DIR=/tmp/output

EXPOSE 8080

WORKDIR /app/backend

# Fallback command if run outside Cloud Run
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "900", "-k", "uvicorn.workers.UvicornWorker", "na_parts_main:app"]
