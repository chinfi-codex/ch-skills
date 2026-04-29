#!/usr/bin/env node

import { readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const rootDir = resolve(__dirname, "..");
const fragmentsDir = join(rootDir, "shared", "fragments");

const fragmentMap: Record<string, string> = {
  PREAMBLE: "preamble.md",
  MODE_GUARD: "mode-guard.md",
  ASK_FORMAT: "ask-format.md",
  COMPLETION_STATUS: "completion-status.md",
  ARTIFACT_PATHS: "artifact-paths.md",
  DOC_WRITING_RULES: "doc-writing-rules.md",
  REVIEW_METHOD: "review-method.md",
};

const generatedNotice = [
  "<!-- AUTO-GENERATED from SKILL.md.tmpl -->",
  "<!-- do not edit directly -->",
].join("\n");

function loadFragments(): Record<string, string> {
  return Object.fromEntries(
    Object.entries(fragmentMap).map(([key, filename]) => [
      key,
      readFileSync(join(fragmentsDir, filename), "utf8").trim(),
    ]),
  );
}

function renderTemplate(template: string, fragments: Record<string, string>): string {
  return template.replace(/\{\{([A-Z_]+)\}\}/g, (fullMatch, key: string) => {
    if (!(key in fragments)) {
      throw new Error(`Unknown placeholder: ${fullMatch}`);
    }
    return fragments[key];
  });
}

function prependNotice(rendered: string): string {
  const normalized = rendered.trimStart();

  const frontmatterMatch = normalized.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/);

  if (!frontmatterMatch) {
    return `${generatedNotice}\n\n${rendered.trimEnd()}\n`;
  }

  const frontmatter = frontmatterMatch[0].trimEnd();
  const body = normalized.slice(frontmatterMatch[0].length).trimStart();

  return `${frontmatter}\n${generatedNotice}\n\n${body.trimEnd()}\n`;
}

function findSkillDirs(): string[] {
  return readdirSync(rootDir)
    .map((name) => join(rootDir, name))
    .filter((fullPath) => statSync(fullPath).isDirectory())
    .filter((fullPath) => {
      const base = fullPath.split(/[\\/]/).pop() ?? "";
      return !["shared", "scripts", "examples"].includes(base);
    })
    .filter((fullPath) => {
      try {
        statSync(join(fullPath, "SKILL.md.tmpl"));
        return true;
      } catch {
        return false;
      }
    });
}

function main(): void {
  const fragments = loadFragments();
  const skillDirs = findSkillDirs();

  if (skillDirs.length === 0) {
    throw new Error(`No skill directories found under ${rootDir}`);
  }

  for (const skillDir of skillDirs) {
    const templatePath = join(skillDir, "SKILL.md.tmpl");
    const outputPath = join(skillDir, "SKILL.md");
    const template = readFileSync(templatePath, "utf8");
    const rendered = renderTemplate(template, fragments);
    writeFileSync(outputPath, prependNotice(rendered), "utf8");
    console.log(`Generated ${outputPath}`);
  }
}

main();
