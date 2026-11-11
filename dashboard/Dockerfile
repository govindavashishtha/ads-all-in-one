# PLACEHOLDER image — the dashboard agent replaces this with the Next.js build.
FROM node:22-alpine
ARG NEXT_PUBLIC_BASE_PATH=/ads-autopilot
ARG NEXT_PUBLIC_API_URL=/ads-autopilot/api/v1
ENV NEXT_PUBLIC_BASE_PATH=$NEXT_PUBLIC_BASE_PATH NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
WORKDIR /app
COPY server.js ./
USER node
EXPOSE 3000
CMD ["node", "server.js"]
