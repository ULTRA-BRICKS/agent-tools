import { defineRule } from "@oxlint/plugins";
import type { ESTree } from "@oxlint/plugins";

type Parameter = ESTree.ParamPattern;
type ParameterOwner =
  | ESTree.ArrowFunctionExpression
  | ESTree.Function
  | ESTree.TSCallSignatureDeclaration
  | ESTree.TSConstructSignatureDeclaration
  | ESTree.TSConstructorType
  | ESTree.TSFunctionType
  | ESTree.TSMethodSignature;

type LooseNode = { type?: string; optional?: boolean; typeAnnotation?: LooseNode; elementTypes?: LooseNode[] };

function tupleRestContainsOptional(parameter: Parameter): boolean {
  const node = parameter as unknown as LooseNode;
  if (node.type !== "RestElement") return false;
  const annotation = node.typeAnnotation?.typeAnnotation;
  if (annotation?.type !== "TSTupleType" || !Array.isArray(annotation.elementTypes)) return false;
  return annotation.elementTypes.some((element) => element.type === "TSOptionalType" || element.optional === true);
}

function optionalParameter(parameter: Parameter): ESTree.Node | null {
  if (parameter.type === "TSParameterProperty") return optionalParameter(parameter.parameter);
  if (parameter.type === "AssignmentPattern") return null;
  if (parameter.type === "RestElement") {
    return parameter.optional || tupleRestContainsOptional(parameter) ? parameter : null;
  }
  return parameter.optional ? parameter : null;
}

/** Prefer explicit input-object optionality over positional `?` parameters. */
export const noOptionalFunctionParametersRule = defineRule({
  meta: {
    type: "suggestion",
    docs: {
      description:
        "Disallow TypeScript optional positional parameters; model optional input explicitly in a named request/options contract.",
    },
    messages: {
      optional:
        "This positional parameter is optional. Prefer a named request/options object with an explicit optional field (or a required `T | undefined` when positional semantics are essential). See: docs/rules/no-optional-function-parameters.md",
    },
  },
  create(context) {
    const check = (node: ParameterOwner) => {
      for (const parameter of node.params) {
        const target = optionalParameter(parameter);
        if (target !== null) context.report({ node: target, messageId: "optional" });
      }
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
