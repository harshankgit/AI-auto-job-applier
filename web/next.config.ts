import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The dashboard reads the bot's sqlite DB through node:sqlite and spawns the
  // Python CLI, so every route that touches those lives on the Node runtime.
  // Nothing here is bundled for the edge.
  outputFileTracingRoot: __dirname,
  experimental: {
    // Keep the dev overlay out of the way of the log panel.
    optimizePackageImports: [],
  },
};

export default nextConfig;
