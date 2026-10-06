const typeboxStub = `
export const Type = {
	Array(items, options = {}) {
		return { type: "array", items, ...options };
	},
	Integer(options = {}) {
		return { type: "integer", ...options };
	},
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
			required: Object.keys(properties).filter((key) => properties[key]?.optional !== true),
      ...options,
    };
  },
  String(options = {}) {
    return { type: "string", ...options };
  },
	Union(items) {
		return { anyOf: items };
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
