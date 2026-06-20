---
name: js-conventions
description: "JavaScript/TypeScript style conventions: formatting (prettier), imports, error handling, async. Use when the target repo is JS/TS (package.json present)."
license: MIT
---

# Skill: js-conventions

Apply only when the target repo is JS/TS. Override with repo-local config
(`.eslintrc`, `tsconfig.json`, `package.json` "type").

## Module system
- Prefer ESM (`"type": "module"` in package.json) for new packages.
- `import` over `require`. No mixed CJS/ESM in the same file.

## TypeScript
- `strict: true` always. No `any` (use `unknown` + narrow).
- Prefer `type` aliases for unions/intersections, `interface` for object shapes
  the consumer may extend.
- No non-null assertion `!` except in tests.
- Use `as const` for literal-typed config.

## Style
- `prettier --write` is the source of truth.
- ESLint with project config; add `// eslint-disable-next-line` ONLY with a
  comment explaining why.
- Arrow functions for callbacks, `function` declarations for top-level.

## Async
- `async/await` over `.then()` chains.
- Always `await` or explicitly `void promise` to satisfy `no-floating-promises`.
- Promises in parallel: `Promise.all([...])` (or `Promise.allSettled` if some
  may fail).

## React (if applicable)
- Functional components + hooks only.
- One component per file. PascalCase filename.
- Props typed, no `React.FC` (verbose).
- Side-effects in `useEffect` with explicit deps; lint rule must be on.

## Errors
- `try { ... } catch (e) { ... }` — narrow `e` (`if (e instanceof Error) ...`).
- Never swallow errors silently; log or rethrow.

## Node-specific
- `fetch` is built-in (Node ≥ 18); don't add `axios` for new code.
- `fs/promises`, never callback-style fs.
- Env vars validated at startup (`zod` or hand-rolled), not deep inside handlers.
