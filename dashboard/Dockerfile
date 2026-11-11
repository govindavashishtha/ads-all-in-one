# Next.js dashboard (BUILD_PLAN §18). basePath and API URL are BUILD-TIME values:
# changing them requires rebuilding this image.
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:22-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_BASE_PATH=/ads-autopilot
ARG NEXT_PUBLIC_API_URL=/ads-autopilot/api/v1
# true = demo build with clearly-labelled example data and no backend calls.
ARG NEXT_PUBLIC_MOCK_API=false
ARG NEXT_PUBLIC_CSRF_COOKIE=aa_csrf
ENV NEXT_PUBLIC_BASE_PATH=$NEXT_PUBLIC_BASE_PATH \
    NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_MOCK_API=$NEXT_PUBLIC_MOCK_API \
    NEXT_PUBLIC_CSRF_COOKIE=$NEXT_PUBLIC_CSRF_COOKIE \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
