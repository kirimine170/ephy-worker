const typeboxStub = `
export const Type = {
  Literal(value) {
    return { const: value };
  },
  Object(properties, options = {}) {
    return {
      type: "object",
      properties,
      required: Object.keys(properties),
      ...options,
    };
  },
  String(options = {}) {
    return { type: "string", ...options };
  },
};
`;

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "typebox") {
    return {
      url: `data:text/javascript,${encodeURIComponent(typeboxStub)}`,
      shortCircuit: true,
    };
  }
  return nextResolve(specifier, context);
}
