// The smoke test exercises runtime approval binding, not TypeBox validation.
export async function resolve(specifier, context, nextResolve) {
  if (specifier === "typebox") return {
    url: "data:text/javascript," + encodeURIComponent(
      "export const Type = new Proxy({}, {get: (_, name) => (...args) => ({name, args})});"),
    shortCircuit: true,
  };
  return nextResolve(specifier, context);
}
