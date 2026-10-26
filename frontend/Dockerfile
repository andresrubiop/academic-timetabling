# Next.js, built to a standalone bundle.
#
# Three stages so that node_modules -- the largest thing in a JS project and
# the one full of build-time-only packages -- never reaches the runtime
# image. On this host that is not tidiness: `/` is at 96%.

# ---------------------------------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
# `npm ci` when a lockfile exists: it installs exactly what was locked, which
# is what makes the build reproducible and what stops a compromised upstream
# version from being pulled in silently. Falls back for the initial build,
# before a lockfile is committed.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

# ---------------------------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Baked at build time, because Next.js inlines NEXT_PUBLIC_* into the client
# bundle. Only ever put non-secret values in a NEXT_PUBLIC_ variable: they
# are shipped to every browser.
ARG NEXT_PUBLIC_API_BASE_URL=/api/v1
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
RUN npm run build

# ---------------------------------------------------------------------------
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup --system --gid 10002 nodeapp \
 && adduser --system --uid 10002 --ingroup nodeapp nextjs

# `standalone` contains a minimal server plus only the traced dependencies.
COPY --from=builder --chown=10002:10002 /app/.next/standalone ./
COPY --from=builder --chown=10002:10002 /app/.next/static ./.next/static

USER 10002:10002

EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=10s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
