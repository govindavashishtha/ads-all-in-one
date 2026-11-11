import type { NextConfig } from "next";

/**
 * BUILD_PLAN §18: basePath and API URL are build-time values (Docker build args).
 * Changing them requires rebuilding the image.
 */
const rawBase = process.env.NEXT_PUBLIC_BASE_PATH ?? "/ads-autopilot";
const basePath = rawBase === "/" ? "" : rawBase.replace(/\/+$/, "");

const nextConfig: NextConfig = {
  basePath,
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  env: {
    // Re-exported so client code can read the normalised value.
    NEXT_PUBLIC_BASE_PATH: basePath,
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "same-origin" },
        ],
      },
    ];
  },
};

export default nextConfig;
