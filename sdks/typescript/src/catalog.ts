import snapshot from "./catalog-data.json" with { type: "json" };
import type { ModelInfo } from "./types.js";
export { Model } from "./model-constants.js";

const models: readonly ModelInfo[] = snapshot.map((m) =>
  Object.freeze({
    id: m.id,
    name: m.name,
    provider: m.provider,
    description: m.description,
    accessTier: m.access_tier as ModelInfo["accessTier"],
    reasoningEfforts: Object.freeze([...m.reasoning_efforts]),
    supportsImages: m.supports_images,
    supportsPDF: m.supports_pdf,
    supportsTools: m.supports_tools,
    beta: m.beta,
    defaultReasoningEffort: m.reasoning_efforts[0]!,
  }),
);

export function listModels(
  options: { includeSubscriber?: boolean } = {},
): readonly ModelInfo[] {
  return Object.freeze(
    models.filter(
      (m) => options.includeSubscriber !== false || m.accessTier === "free",
    ),
  );
}
export function getModel(id: string): ModelInfo | undefined {
  return models.find((m) => m.id === id);
}
/** @internal */
export function nativeProfile(id: string) {
  return snapshot.find((m) => m.id === id)?.attachment_profile;
}
