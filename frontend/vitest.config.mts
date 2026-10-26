import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // The interaction machine is pure, so no DOM environment is needed. Tests
    // that render components will add their own.
    environment: "node",
    include: ["lib/**/*.test.ts", "app/**/*.test.ts"],
  },
});
