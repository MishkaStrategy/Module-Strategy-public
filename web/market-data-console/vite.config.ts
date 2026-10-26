import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

function loopbackApiTarget(
  portValue: string | undefined,
  environmentName: string,
  defaultPort: string,
): string {
  const normalized = portValue?.trim() || defaultPort;
  if (!/^\d{1,5}$/.test(normalized)) {
    throw new Error(`${environmentName} must be a decimal TCP port`);
  }
  const port = Number(normalized);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error(`${environmentName} must be between 1 and 65535`);
  }
  return `http://127.0.0.1:${port}`;
}

export default defineConfig(({ mode }) => {
  const mdiEnvironment = loadEnv(mode, ".", "MDI_");
  const controlEnvironment = loadEnv(mode, ".", "MODULAR_CONTROL_");
  const proxy = {
    "/api/control": {
      target: loopbackApiTarget(
        controlEnvironment.MODULAR_CONTROL_PORT,
        "MODULAR_CONTROL_PORT",
        "8765",
      ),
      changeOrigin: false,
    },
    "/api": {
      target: loopbackApiTarget(mdiEnvironment.MDI_E2E_API_PORT, "MDI_E2E_API_PORT", "8000"),
      changeOrigin: false,
    },
  };

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy,
    },
    preview: {
      host: "127.0.0.1",
      port: 4173,
      strictPort: true,
      proxy,
    },
    test: {
      include: ["src/**/*.test.ts"],
    },
  };
});
