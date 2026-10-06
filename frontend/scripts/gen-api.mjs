// Regenerates src/types/api.ts from the backend OpenAPI schema.
//   node scripts/gen-api.mjs            -> write src/types/api.ts
//   node scripts/gen-api.mjs --check    -> regenerate to a temp file and fail if it differs
// REPO (the repo root) is exported to the child so an OPENAPI_PYTHON docker override can mount `-v "$REPO:/app"`.
// Python is `python3` unless OPENAPI_PYTHON is set (a shell command prefix, e.g. a `docker run ... python`).
import { execSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const repo = resolve(frontend, '..');
const target = join(frontend, 'src', 'types', 'api.ts');
const check = process.argv.includes('--check');
const py = process.env.OPENAPI_PYTHON || 'python3';
const tmp = mkdtempSync(join(tmpdir(), 'openapi-'));
const json = join(tmp, 'openapi.json');
const out = check ? join(tmp, 'api.ts') : target;

try {
  // Export script path is repo-relative: the repo root is the cwd (and /app in the docker override).
  // Schema goes over stdout so an OPENAPI_PYTHON container override needs no shared temp path.
  const schema = execSync(`${py} scripts/export_openapi.py`, {
    cwd: repo,
    maxBuffer: 256 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'inherit'],
    env: { ...process.env, REPO: repo, PYTHONPATH: repo, PYTHONDONTWRITEBYTECODE: '1' },
  });
  writeFileSync(json, schema);
  execSync(`npx openapi-typescript "${json}" -o "${out}"`, { cwd: frontend, stdio: ['ignore', 'ignore', 'inherit'] });
  if (check) {
    const fresh = readFileSync(out, 'utf8');
    const committed = readFileSync(target, 'utf8');
    if (fresh !== committed) {
      console.error('\nsrc/types/api.ts is stale: it no longer matches the backend OpenAPI schema.');
      console.error('Run `npm run gen:api` (from frontend/) and commit the result.');
      process.exitCode = 1;
    } else {
      console.log('src/types/api.ts is up to date.');
    }
  }
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
