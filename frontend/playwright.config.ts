import { defineConfig } from "@playwright/test";

if (!process.env.E2E_POSTGRES_URL) throw new Error("Set E2E_POSTGRES_URL to an independent disposable PostgreSQL _test database.");
const frontendPort = Number(process.env.E2E_FRONTEND_PORT ?? "4173");
const apiPort = Number(process.env.E2E_API_PORT ?? "18081");
const origin = `http://127.0.0.1:${frontendPort}`;
const python = process.env.E2E_PYTHON ?? "python3";

export default defineConfig({
  testDir: "./tests", fullyParallel: false, workers: 1, timeout: 30000,
  reporter: "list", outputDir: "test-results",
  use: { baseURL: origin, browserName: "chromium", channel: process.env.E2E_BROWSER_CHANNEL,
    trace: "off", screenshot: "only-on-failure" },
  webServer: [
    { command: `${python} ../backend/tests/browser_fixture.py`, url: `http://127.0.0.1:${apiPort}/api/v1/health`,
      reuseExistingServer: false, timeout: 45000,
      env: { PULSEPAY_E2E_ALLOWED: "1", E2E_POSTGRES_URL: process.env.E2E_POSTGRES_URL,
        E2E_FRONTEND_ORIGIN: origin, E2E_API_PORT: String(apiPort) } },
    { command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`, url: origin,
      reuseExistingServer: false, timeout: 30000, env: { API_PROXY_TARGET: `http://127.0.0.1:${apiPort}` } },
  ],
});
