FROM node:22.13.0-slim AS dependencies

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22.13.0-slim AS builder

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 \
    GCP_CLOUD_RUN_BUILD=1 \
    NEXT_PUBLIC_DEPLOYMENT_MODE=gcp-iap

COPY --from=dependencies /app/node_modules ./node_modules
COPY . .
RUN npm run build:gcp

FROM node:22.13.0-slim AS runner

WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=8080 \
    HOSTNAME=0.0.0.0

RUN groupadd --system --gid 1001 nodejs \
    && useradd --system --uid 1001 --gid nodejs nextjs

COPY --from=builder --chown=nextjs:nodejs /app/.next-gcp/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next-gcp/static ./.next-gcp/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

USER nextjs

CMD ["node", "server.js"]
