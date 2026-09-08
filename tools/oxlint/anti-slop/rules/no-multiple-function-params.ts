import { defineRule } from "@oxlint/plugins";
import type { ESTree } from "@oxlint/plugins";

type ParameterOwner =
  | ESTree.ArrowFunctionExpression
  | ESTree.Function
  | ESTree.TSCallSignatureDeclaration
  | ESTree.TSConstructSignatureDeclaration
  | ESTree.TSConstructorType
  | ESTree.TSFunctionType
  | ESTree.TSMethodSignature;

type LooseNode = { type?: string; name?: string; typeAnnotation?: LooseNode; elementTypes?: LooseNode[] };

function tupleRestArity(parameter: ESTree.ParamPattern): number | null {
  const node = parameter as unknown as LooseNode;
  if (node.type !== "RestElement") return null;
  const annotation = node.typeAnnotation?.typeAnnotation;
  if (annotation?.type !== "TSTupleType" || !Array.isArray(annotation.elementTypes)) return null;
  return annotation.elementTypes.length;
}

function positionalArity(params: readonly ESTree.ParamPattern[]): number {
  let count = 0;
  for (const parameter of params) {
    const node = parameter as unknown as LooseNode;
    if (node.type === "Identifier" && node.name === "this") continue;
    count += tupleRestArity(parameter) ?? 1;
  }
  return count;
}

/** Encourage named request/command objects instead of long positional function signatures. */
export const noMultipleFunctionParamsRule = defineRule({
  meta: {
    type: "suggestion",
    docs: {
      description:
        "Disallow functions with more than one caller-visible positional parameter; use a named parameter object for multi-value input contracts.",
    },
    messages: {
      multiple:
        "This function has {{count}} positional parameters. Prefer one named request/options object so call sites carry field names and the contract can evolve without parameter-order coupling. See: docs/rules/no-multiple-function-params.md",
    },
  },
  create(context) {
    const check = (node: ParameterOwner) => {
      const count = positionalArity(node.params);
      if (count <= 1) return;
      context.report({ node, messageId: "multiple", data: { count: String(count) } });
    };
    return {
      ArrowFunctionExpression: check,
      FunctionDeclaration: check,
      FunctionExpression: check,
      TSCallSignatureDeclaration: check,
      TSConstructSignatureDeclaration: check,
      TSConstructorType: check,
      TSDeclareFunction: check,
      TSEmptyBodyFunctionExpression: check,
      TSFunctionType: check,
      TSMethodSignature: check,
    };
  },
});
