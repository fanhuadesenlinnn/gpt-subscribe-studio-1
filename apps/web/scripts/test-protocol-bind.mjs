import { build } from "esbuild";
import Module from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const filename = resolve(root, ".protocol-bind-tests.cjs");
const result = await build({
  absWorkingDir: root,
  entryPoints: ["tests/protocol-bind.test.tsx"],
  outfile: filename,
  bundle: true,
  packages: "external",
  platform: "node",
  format: "cjs",
  jsx: "automatic",
  write: false,
});
const compiled = new Module(filename);
compiled.filename = filename;
compiled.paths = Module._nodeModulePaths(root);
compiled._compile(result.outputFiles[0].text, filename);
