FROM node:22-bookworm-slim AS deps
WORKDIR /app
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml web/.npmrc ./
# `sharp` is an optional next/image dep we don't build (images are unoptimized).
RUN pnpm install --frozen-lockfile --config.strictDepBuilds=false

FROM node:22-bookworm-slim AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY web/ .
RUN node_modules/.bin/next build

FROM node:22-trixie-slim AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    GARMIN_COACH_REPO_ROOT=/opt/garmin-coach \
    GARMIN_COACH_PYTHON=/opt/venv/bin/python \
    PYTHONPATH=/opt/garmin-coach
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
COPY requirements.txt /opt/garmin-coach/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /opt/garmin-coach/requirements.txt
COPY coach/ /opt/garmin-coach/coach/
COPY collector/ /opt/garmin-coach/collector/
RUN addgroup --system nodejs && adduser --system --ingroup nodejs nextjs
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
