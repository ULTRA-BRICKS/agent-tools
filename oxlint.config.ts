// Managed by Rig. Source of truth: global Rig config + repository rig.yaml.
// Do not edit directly: `rig apply` reconciles this file. Temporary diagnostic edits are allowed locally,
// but move the final change into Rig policy before commit.

import { defineConfig } from "oxlint";

export default defineConfig({
  options: { typeAware: true },
  jsPlugins: [
    { name: "anti-slop", specifier: "./tools/oxlint/anti-slop/index.ts" },
  ],
  categories: { correctness: "error", suspicious: "error", perf: "error" },
  rules: {
    "anti-slop/no-chained-type-assertions": "error",
    "anti-slop/no-conditional-empty-object-spread": "off",
    "anti-slop/no-known-value-widening": "error",
    "anti-slop/no-module-mocking": "warn",
    "anti-slop/no-multiple-function-params": "off",
    "anti-slop/no-object-parameters": "error",
    "anti-slop/no-optional-function-parameters": "off",
    "anti-slop/no-reflect-apply": "warn",
    "anti-slop/no-reflect-get": "warn",
    "anti-slop/no-runtime-typeof": "off",
    "anti-slop/no-shape-in-symbol-names": "off",
    "anti-slop/no-unknown-parameters": "warn",
    "anti-slop/no-unknown-returns": "error",
    "anti-slop/no-unknown-type-aliases": "error",
    "anti-slop/no-unsafe-dictionary-type": "error",
    "anti-slop/no-widen-then-assert": "error",
    "anti-slop/require-safety-comment-for-type-assertion": "error",
    "typescript/ban-ts-comment": [
      "error",
      {
        "ts-ignore": true,
        "ts-nocheck": true,
        "ts-expect-error": "allow-with-description",
        minimumDescriptionLength: 8,
      },
    ],
    "typescript/no-non-null-assertion": "error",
    "typescript/no-unnecessary-type-assertion": "error",
    "typescript/no-unsafe-type-assertion": "error",
  },
});
