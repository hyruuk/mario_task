# Releasing a new version

How to ship a new `mario_task-windows-vX.Y.Z.zip` to operators.

## The flow

1. **Make your code changes** on `main` and commit normally.
2. **Bump the version** in `pyproject.toml` (`version = "0.2.0"` → `"0.3.0"`).
3. **Commit the version bump**:
   ```bash
   git commit -am "Release 0.3.0"
   ```
4. **Tag the commit** with `v` + the same version:
   ```bash
   git tag v0.3.0
   ```
5. **Push both the commit and the tag**:
   ```bash
   git push origin main --tags
   ```

GitHub Actions takes over from there:
- Workflow `release.yml` triggers on the `v0.3.0` tag.
- The packaging script runs (`scripts/package-windows-release.sh`).
- A new GitHub Release is created at `https://github.com/<you>/mario_task/releases/tag/v0.3.0` with `mario_task-windows-v0.3.0.zip` attached.

Roughly 15 seconds from `git push` to the ZIP being live.

## Versioning

We follow SemVer-ish for now:
- **Patch** (0.3.0 → 0.3.1): bug fixes, docs, no operator-facing changes.
- **Minor** (0.3.0 → 0.4.0): new feature operators need to know about (added a level, changed a default, etc.).
- **Major** (0.x → 1.0): breaking change to config.json or BIDS output format.

## Stable operator URL

Once released, operators always download from:

```
https://github.com/<your-github-user>/mario_task/releases/latest
```

GitHub redirects "latest" to the most recent non-prerelease tag. That URL is what goes in any lab documentation — lab managers don't need to update it across releases.

## Gotcha: the "same-push" GitHub quirk (one-time)

When `release.yml` is added in the *same push* as the version tag that should trigger it, GitHub Actions doesn't fire — the workflow file wasn't registered yet at the moment the tag event was processed.

**This only affects the very first release.** After `release.yml` exists on `main`, every subsequent tag push triggers normally.

**Fix if you hit it**: re-push the tag.
```bash
git push origin :refs/tags/v0.3.0       # delete remote tag
git push origin v0.3.0                  # re-push it
```
Same commit, but now Actions sees the workflow and runs.

You can confirm a run is happening with:
```bash
gh run list --limit 5
```

## Pre-release versions

To cut a prerelease (e.g. `v0.3.0-rc1`), tag with the suffix:
```bash
git tag v0.3.0-rc1
git push origin v0.3.0-rc1
```

GitHub treats anything with `-alpha` / `-beta` / `-rc` as a prerelease automatically. The `/releases/latest` URL skips prereleases, so operators still see the most recent stable. Useful for in-lab pilot testing before a wider rollout.

## What's in the ZIP

`scripts/package-windows-release.sh` includes every tracked-or-untracked-not-gitignored file under the repo root, minus:
- `setup_env.sh`, `run.sh` (Linux-only)
- `data/`, `output/`, `dist/`, `.local-libs/` (data + outputs)
- `config.json` (per-deployment)

If you add a new top-level file you want shipped to operators, just `git add` it — the packaging script picks it up automatically. If you add one you *don't* want shipped, add it to the grep filters at the top of the script.

## Verifying a release

```bash
gh release view v0.3.0 --json assets --jq '.assets[].name'
# → mario_task-windows-v0.3.0.zip

# Download and inspect locally:
gh release download v0.3.0 --dir /tmp/check
unzip -l /tmp/check/mario_task-windows-v0.3.0.zip | head
```

You can also trigger a *dry-run* of the workflow without making a release — go to **Actions → Build Windows release ZIP → Run workflow**, or:
```bash
gh workflow run release.yml
```
This produces a ZIP as a workflow artifact (no GitHub Release, no public URL). Useful for testing packaging changes.
