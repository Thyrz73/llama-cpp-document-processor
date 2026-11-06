# Contributing

Bug reports, fixes and improvements are welcome. This guide describes the workflow; the [architecture guide](docs/architecture.md) explains how the code is organized and where to make common changes.

## Workflow

1. For a large change, open an [issue](https://github.com/Thyrz73/llama-cpp-document-processor/issues) first to agree on the approach.
2. Branch from `develop`, and open the pull request against `develop`.
3. Set up the tools as described in the README's [Development](README.md#development) section, then run every check before pushing:

   ```bash
   .venv/bin/ruff format --check .
   .venv/bin/ruff check .
   .venv/bin/mypy
   .venv/bin/python -m unittest discover -s tests
   node --test tests/test_polling.js
   ```

   Frontend tests use Node 22’s built-in `node:test` runner and `node:assert/strict`; no dependency installation is needed. The suites run sequentially because real interface modules share state and browser globals. Each scenario gets a fresh simulated DOM, explicitly settled requests, manually controlled timers and automatic cleanup of globals. Filter scenarios with `node --test --test-name-pattern="visibility" tests/test_polling.js`.

   Check JS, HTML and CSS formatting with the locked development tool:

   ```bash
   npm ci --ignore-scripts
   npm run format:check
   ```

   Apply formatting with `npm run format`; `npm test` is an alias for the Node test command above. Prettier 3.6.2 is the only npm dependency, pinned exactly in `package.json` and integrity-locked in `package-lock.json`. It provides one formatter for JS, HTML and CSS without a build step or browser dependency. CI uses `npm ci`; no command downloads an unpinned formatter. The scripts target source JS, CSS and HTML plus the test helpers and tooling configuration; generated assets and translation catalogs are outside their scope.

   `.prettierrc.json` uses strict HTML whitespace sensitivity to preserve spacing around inline elements. Review template changes carefully: keep Jinja expressions and literal textarea/pre content intact, and run the Python template/report tests after formatting templates. To update the formatter deliberately, run `npm install --save-dev --save-exact prettier@<version>` and commit both npm manifests with the formatting changes and checks.

   Apply Python formatting locally with `.venv/bin/ruff format .`. Ruff checks import sorting (`I`), bug-prone constructs (`B`), Python 3.12 modernization (`UP`) and simplifications (`SIM`), in addition to the core rules. Keep any rule exceptions local and explain why they preserve existing behavior.

   Use Python 3.12 and install the locked tools with `.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt` (Ruff 0.17.3 and MyPy 2.4.1).

   MyPy applies stronger checks only to the modules listed in `pyproject.toml`. See the [dictionary contracts](docs/type-contracts.md) for required/optional fields and the remaining dynamic boundaries. After the normal `.venv/bin/mypy` check, verify the selected modules in full strict mode:

   ```bash
   .venv/bin/mypy --strict --follow-imports=silent \
     -m document_processor.contracts -m document_processor.config \
     -m document_processor.prompts -m document_processor.environment \
     -m document_processor.settings
   ```

   CI runs the same checks, and also checks syntax, an offline benchmark plan, Compose and Docker packaging.
4. Add or update tests for the behavior you change, and update the documentation in the same pull request.

## Commits

- Make one logical change per commit; every commit should pass the checks.
- Write the subject in the [Conventional Commits](https://www.conventionalcommits.org/) style: `feat:`, `fix:`, `perf:`, `refactor:`, `test:`, `docs:`, `build:`, `ci:` or `chore:`, then a short summary. Use the body to explain what changed and why.
- Keep formatting separate from content: reformat in a commit of its own, then list that commit in [.git-blame-ignore-revs](.git-blame-ignore-revs) so that `git blame` skips it.
- Write code, comments and commit messages in English. Interface text goes in both languages of `static/translations.json`.

## Updating dependencies

Dependencies are declared with version ranges in `requirements.in` (runtime) and `requirements-dev.in` (tools). The installed `requirements.txt` and `requirements-dev.txt` files are hash-locked for Linux and Python 3.12; never edit them by hand. After changing a `.in` file, regenerate both locks with pip-tools, runtime first:

```bash
.venv/bin/python -m pip install pip-tools
.venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url -o requirements.txt requirements.in
.venv/bin/pip-compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url -o requirements-dev.txt requirements-dev.in
```

`tests/test_dependencies.py` checks that both locks agree. The Docker base image is pinned by digest. [Dependabot](.github/dependabot.yml) proposes weekly updates for Python packages, the base image and GitHub Actions on `develop`.

## Private data

Never commit secrets, private configuration, PDFs, reports or job data. Keep personal notes and test documents in `local/`, which Git ignores. Bug reports should include the revision and the relevant logs, but no secrets or document content.
