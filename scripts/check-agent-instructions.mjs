import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = execFileSync(
  "git",
  ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
  { cwd: repositoryRoot },
);
const files = output
  .toString("utf8")
  .split("\0")
  .filter((file) => file && existsSync(resolve(repositoryRoot, file)));
const fileSet = new Set(files);
const agentFiles = files.filter((file) => basename(file) === "AGENTS.md").sort();
const claudeFiles = files.filter((file) => basename(file) === "CLAUDE.md").sort();
const problems = [];

for (const agentFile of agentFiles) {
  const claudeFile = `${agentFile.slice(0, -"AGENTS.md".length)}CLAUDE.md`;

  if (!fileSet.has(claudeFile)) {
    problems.push(`Missing ${claudeFile}`);
    continue;
  }

  const agentContent = readFileSync(resolve(repositoryRoot, agentFile));
  const claudeContent = readFileSync(resolve(repositoryRoot, claudeFile));

  if (!agentContent.equals(claudeContent)) {
    problems.push(`${claudeFile} differs from ${agentFile}`);
  }
}

for (const claudeFile of claudeFiles) {
  const agentFile = `${claudeFile.slice(0, -"CLAUDE.md".length)}AGENTS.md`;
  if (!fileSet.has(agentFile)) {
    problems.push(`Orphan ${claudeFile}`);
  }
}

if (problems.length > 0) {
  console.error(problems.join("\n"));
  process.exit(1);
}

console.log(`Agent instruction mirrors match: ${agentFiles.length} pairs.`);
