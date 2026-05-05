FROM python:3.12-slim

# System deps
#   xvfb            : virtual X server for headless Chromium
#   curl            : uv installer
#   fonts-noto-cjk  : Chinese rendering inside Playwright pages
#   nodejs+npm      : build the Vue frontend at image-build time
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        curl \
        nodejs \
        npm \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

WORKDIR /app

# ---- Python deps first (cache layer) ----
COPY pyproject.toml ./
RUN uv sync

# ---- Playwright Chromium ----
# This is the step that's often forgotten — without it `Flow.start()` will
# crash with: "BrowserType.launch: Executable doesn't exist at ..."
RUN uv run playwright install chromium && \
    uv run playwright install-deps chromium

# ---- Frontend build (cache layer) ----
COPY web/ web/
RUN cd web && npm install && npm run build

# ---- Source ----
COPY src/ src/

# Persistence
VOLUME ["/app/data"]
RUN mkdir -p /app/data /app/data/auths /app/data/runs /app/data/logs

ENV DISPLAY=:99
EXPOSE 8788

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["api", "--host", "0.0.0.0", "--port", "8788"]
