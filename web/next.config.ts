import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard reads the bot's sqlite DB through node:sqlite and spawns the
  // Python CLI, so every route that touches those lives on the Node runtime.
  // Nothing here is bundled for the edge.
  outputFileTracingRoot: __dirname,
  // Ship the build-time report snapshot with every serverless function so the
  // hosted (Vercel) dashboard can read it without the Python project.
  outputFileTracingIncludes: {
    "/**": ["./snapshot/**"],
  },
  experimental: {
    // Keep the dev overlay out of the way of the log panel.
    optimizePackageImports: [],
  },
};

export default nextConfig;
