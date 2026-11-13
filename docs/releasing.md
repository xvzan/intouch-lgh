# Publishing intouch-lgh

This repository intentionally uses **manual** release runs. The workflow builds distributions from a `vX.Y.Z` tag pointing to the current `main` HEAD, then publishes **the same files** to PyPI and GitHub Release. It does not run on every push or tag creation.

## One-time setup

1. In the GitHub repository, go to **Settings → Environments** and create the `pypi` environment. Configure protection rules appropriate to your access model; ideally require a separate reviewer for release approval. Confirm the workflow can access that environment.
2. At <https://pypi.org/manage/project/intouch-lgh/settings/publishing/> (log in as a project owner), register a GitHub Actions Trusted Publisher with:
   - Owner: `xvzan`
   - Repository: `intouch-lgh`
   - Workflow filename: `release.yml`
   - Environment: `pypi`
3. Never put a PyPI password or long-lived token in the workflow. Ensure the PyPI account controls the existing `intouch-lgh` project.

## Release procedure

1. Review pending changes, dependency/third-party licensing, private-data exclusions, and `git log` / commit metadata; ensure CI on `main` is green.
2. Set `pyproject.toml` and `intouch_lgh/__init__.py` to the desired version. Update `CHANGELOG.md` and add `release-notes/vX.Y.Z.md`. Commit and push these changes to `main`; wait for CI to pass.
3. Create an annotated tag at the same commit and push it:

   ```bash
   git switch main
   git pull --ff-only
   git tag -a v1.0.2 -m "intouch-lgh 1.0.2"
   git push origin v1.0.2
   ```

4. In **GitHub → Actions → Publish release → Run workflow**, select branch `main`, enter `1.0.2` (without `v`), and start the run. The workflow refuses tags that do not match the latest `main` commit or the declared package version.
5. Approve the `pypi` environment deployment if configured. Once PyPI succeeds, GitHub Release is created automatically with precisely the same wheel and sdist.
6. Verify both <https://pypi.org/project/intouch-lgh/> and <https://github.com/xvzan/intouch-lgh/releases>, and inspect their distribution hashes.

### Failure and recovery

- A build/test failure occurs **before** PyPI upload. Fix the code in a new commit, rerun CI, and create a fresh tag pointing at the new `main` commit (do not reuse a public version tag).
- If PyPI upload succeeds but GitHub Release creation fails, **do not rerun the full publishing workflow**: PyPI does not permit overwriting an existing distribution filename. Create the GitHub Release separately, attaching the already built release artifacts (downloaded from the original workflow run) and checking their hashes against PyPI.
- If `1.0.2` already exists on PyPI when publishing, use a new version such as `1.0.3`; never assume PyPI can overwrite published files.

## Security boundaries

- Only the `pypi` job has `id-token: write`; only the subsequent GitHub Release job has `contents: write`.
- Never include private industrial corpora, review reports, or local scan patterns in release assets.
- `twine check` checks distribution metadata rendering but is **not** a complete privacy, license or security audit.
