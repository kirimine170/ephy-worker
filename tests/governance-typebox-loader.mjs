const typeboxStub = `
export const Type = {
  Literal(value) {
    return { const: value };
  },
  Optional(schema) {
    return { ...schema, optional: true };
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
  Union(items, options = {}) {
    return { anyOf: items, ...options };
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
