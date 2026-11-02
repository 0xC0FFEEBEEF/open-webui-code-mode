#!/usr/bin/env node
// tscheck.cjs - the JavaScript half of `check` (installed as /opt/code-check/tscheck.cjs).
// Reads {programs: [{id, kind: "browser"|"node"|"any", root, files: [{path, text?}]}]} on stdin and checks each program
// as plain JavaScript with the TypeScript compiler (allowJs + checkJs). A file with `text` is virtual: that is how the
// inline <script> blocks of an HTML page are checked without writing anything into the project. Scripts of one
// program share their globals, the way classic scripts on one page do.
// Prints {programs: [{id, files: [...project files the program read], diagnostics: [{file, line, col, code, message}]}]}.
"use strict";
const HOME = process.env.CODE_CHECK_HOME || "/opt/code-check";
const ts = require(HOME + "/node_modules/typescript");
const path = require("path");
const fs = require("fs");

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const LIBDIR = path.dirname(ts.getDefaultLibFilePath({}));
const libCache = new Map(); // lib .d.ts files are parsed once and shared by every program

function check(p) {
  const virtual = new Map();
  for (const f of p.files) if (typeof f.text === "string") virtual.set(path.resolve(f.path), f.text);
  const options = {
    allowJs: true, checkJs: true, noEmit: true, skipLibCheck: true, target: ts.ScriptTarget.ES2022,
    allowUnreachableCode: false, allowUnusedLabels: false, strict: false, noImplicitAny: false,
    resolveJsonModule: true, allowImportingTsExtensions: true, types: [], jsx: ts.JsxEmit.Preserve,
  };
  const dom = ["lib.es2022.d.ts", "lib.dom.d.ts", "lib.dom.iterable.d.ts", "lib.dom.asynciterable.d.ts"];
  if (p.kind === "browser") {
    options.lib = dom;
    options.module = ts.ModuleKind.ESNext;
    options.moduleResolution = ts.ModuleResolutionKind.Bundler;
  } else {
    options.lib = p.kind === "node" ? ["lib.es2022.d.ts"] : dom;
    options.module = ts.ModuleKind.NodeNext;
    options.moduleResolution = ts.ModuleResolutionKind.NodeNext;
    options.typeRoots = [HOME + "/node_modules/@types"];
    options.types = ["node"];
  }
  const host = ts.createCompilerHost(options, true);
  const readSource = host.getSourceFile.bind(host);
  host.getSourceFile = (fileName, languageVersion, onError, shouldCreate) => {
    const abs = path.resolve(fileName);
    if (virtual.has(abs)) return ts.createSourceFile(fileName, virtual.get(abs), languageVersion, true, ts.ScriptKind.JS);
    if (abs.startsWith(LIBDIR + path.sep) || abs.startsWith(HOME + path.sep)) {
      if (!libCache.has(abs)) libCache.set(abs, readSource(fileName, languageVersion, onError, shouldCreate));
      return libCache.get(abs);
    }
    return readSource(fileName, languageVersion, onError, shouldCreate);
  };
  host.fileExists = (f) => virtual.has(path.resolve(f)) || ts.sys.fileExists(f);
  host.readFile = (f) => (virtual.has(path.resolve(f)) ? virtual.get(path.resolve(f)) : ts.sys.readFile(f));
  host.getCurrentDirectory = () => p.root;

  const roots = p.files.map((f) => path.resolve(f.path));
  const program = ts.createProgram(roots, options, host);
  const inProject = (name) => {
    const abs = path.resolve(name);
    return abs.startsWith(path.resolve(p.root) + path.sep) && !abs.includes(path.sep + "node_modules" + path.sep) &&
      !abs.endsWith(".d.ts");
  };
  const diagnostics = [];
  for (const d of ts.getPreEmitDiagnostics(program)) {
    if (!d.file || !inProject(d.file.fileName)) continue;
    const pos = d.file.getLineAndCharacterOfPosition(d.start || 0);
    // "also declared here" pointing only at the browser's own definitions (const name, let status, ...) is not a
    // clash between two project scripts; `own` says whether another project file is involved.
    const related = (d.relatedInformation || []).filter((r) => r.file);
    diagnostics.push({
      file: path.resolve(d.file.fileName), line: pos.line + 1, col: pos.character + 1, code: d.code,
      message: ts.flattenDiagnosticMessageText(d.messageText, " "),
      own: related.some((r) => inProject(r.file.fileName)),
    });
  }
  const files = [];
  for (const sf of program.getSourceFiles()) {
    if (!inProject(sf.fileName)) continue;
    files.push(path.resolve(sf.fileName));
    // Node refuses `import`/`export` in a file it treats as CommonJS; TypeScript would quietly accept it.
    if (p.kind === "node" && sf.externalModuleIndicator && sf.impliedNodeFormat === ts.ModuleKind.CommonJS &&
        /\.(js|cjs)$/.test(sf.fileName)) {
      const node = sf.externalModuleIndicator;
      if (node.kind === ts.SyntaxKind.ImportDeclaration || node.kind === ts.SyntaxKind.ExportDeclaration ||
          node.kind === ts.SyntaxKind.ExportAssignment || ts.canHaveModifiers(node)) {
        const pos = sf.getLineAndCharacterOfPosition(node.getStart(sf));
        diagnostics.push({
          file: path.resolve(sf.fileName), line: pos.line + 1, col: pos.character + 1, code: 90001,
          message: "import/export in a file Node runs as CommonJS: it stops with \"Cannot use import statement outside a " +
            "module\". Name the file .mjs, or add \"type\": \"module\" to package.json, or use require().",
        });
      }
    }
  }
  return { id: p.id, files, diagnostics };
}

const out = [];
for (const p of input.programs) {
  try {
    out.push(check(p));
  } catch (e) {
    out.push({ id: p.id, files: [], diagnostics: [], failed: String((e && e.stack) || e).slice(0, 600) });
  }
}
process.stdout.write(JSON.stringify({ programs: out }));
