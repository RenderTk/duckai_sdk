import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts"],
  format: ["esm", "cjs"],
  dts: true,
  clean: true,
  sourcemap: true,
  esbuildOptions(options, context) {
    // The evaluator is serialized into Duck.ai's iframe; it never evaluates upstream code in Node.
    options.logOverride = { ...options.logOverride, "direct-eval": "silent" };
    // CommonJS supplies __filename; ESM uses import.meta.url for package-local requires.
    if (context.format === "cjs")
      options.define = { ...options.define, "import.meta.url": "__filename" };
  },
});
