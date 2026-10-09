# Contributing to TrackSeerr

Bug reports, fixes, and documentation changes are welcome. For a large feature, open an issue first so we can agree on the approach.

## Layout

| Path | Contents |
|---|---|
| `trackseerr/` | Python backend (FastAPI). Run with `python -m trackseerr`. |
| `frontend/` | Web app (React, TypeScript, Vite). |
| `tests/` | pytest suite. |
| `unraid/` | Unraid templates and icons. |
| `docs/` | User guides. `docs/design/` holds design notes for maintainers. |

## Set up

Backend, Python 3.12:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m trackseerr
```

Frontend, in a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

The Vite dev server proxies API calls to the backend on `localhost:5250`.

## Run the checks

Run all four before you open a pull request. CI runs the same.

```bash
# 1. Frontend type check and build
cd frontend && npx tsc --noEmit && npm run build && cd ..

# 2. Python syntax
python3 -m py_compile trackseerr/**/*.py tests/**/*.py

# 3. Test suite, in the test image
docker build -f Dockerfile.test -t trackseerr:test .
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD":/app -w /app -e PYTHONPATH=/app trackseerr:test \
  pytest -q -n 2 -p no:cacheprovider

# 4. Image build
docker build -t trackseerr:dev .
```

The full suite takes about 15 minutes with two workers. While you work, run only the tests you touch, for example `pytest tests/test_api.py -q`.

Two optional suites are not part of the normal run:

- [Integration tests](docs/INTEGRATION_TESTS.md) against real Lidarr, Navidrome, and Jellyfin containers.
- [Local media tests](docs/LOCAL_MEDIA_TESTS.md) against a real music folder.

## API types

`frontend/src/types/api.ts` is generated from the backend's OpenAPI schema. Do not edit it by hand. After you change a backend route or a Pydantic model, run from `frontend/`:

```bash
npm run gen:api     # regenerate src/types/api.ts
npm run check:api   # fail if the file is out of date (CI runs this)
```

The generator runs `python3` and needs the backend dependencies. To use another interpreter, such as the test image, set `OPENAPI_PYTHON` to a command that reads the repo at `/app`. The script exports `REPO`, the repo root:

```bash
OPENAPI_PYTHON='docker run --rm -i --user 1000:1000 -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v "$REPO:/app" -w /app -e PYTHONPATH=/app trackseerr:test python' npm run gen:api
```

In frontend code, take backend types from the contract with `Schema<'Name'>` (from `src/types`) instead of writing your own interfaces.

## Rules

- **No silent failures.** No bare `except:`. Catch specific exceptions. If you must catch `Exception`, log the cause.
- **SQL.** Parameterized queries only. Never build SQL from strings.
- **No shell.** No `subprocess`, `os.system`, or shell calls.
- **Outbound URLs.** Validate every address before connecting. Use the helpers in `trackseerr/security.py`.
- **Secrets.** Never commit tokens or keys. New secrets come from environment variables or the database, are never returned by the API, and are redacted from logs. If a secret must not reach the gateway, add it to the list in `trackseerr/role_guard.py`.
- **Types.** Type hints on all new Python functions. No `any` in TypeScript.
- **Tests.** Every fix and feature comes with tests that exercise it, with outside APIs mocked.
- **Docs.** If you add an environment variable, add it to [docs/CONFIGURATION.md](docs/CONFIGURATION.md). If you change how a feature is used, update its guide.

## Pull requests

1. Fork the repository and create a branch from `main`, for example `fix/playlist-match` or `feat/new-client`.
2. Use [Conventional Commits](https://www.conventionalcommits.org/) for commit messages: `feat:`, `fix:`, `docs:`, and so on.
3. Push your branch and open a pull request against `main` in `RonFBurgundy/trackseerr`.
4. Fill in the pull request template.

CI runs the checks above and a CodeQL scan.

## Conduct

Be respectful and constructive.
