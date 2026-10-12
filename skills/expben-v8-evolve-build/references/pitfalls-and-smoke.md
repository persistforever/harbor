# Pitfalls, smoke harness, publishing — notes from the 2026-10 run

Everything here was hit or measured by hand; the Fast path section in `SKILL.md`
is the short version. Read this when something behaves differently from what the
pipeline says it does.

## Worked example: 2 problems × 5 directions (2026-10-11/12)

| | `cybergen_v8_cr_15a72141745c` | `cybergen_v8_cr_1b85c84da74a` |
|---|---|---|
| V8 | ~14.2 (2025-09), DEPS pins LLVM 22 | 12.8 (2024-06), DEPS pins LLVM 19 |
| source image | 15.1 GB, id `2d00c26e0c79`, 89m38s | 14.5 GB, id `89476bb35169`, 76m10s |
| evolved image | 16.2 GB, ~44 min each | 15.4 GB, ~36 min each |
| directions | d01, d02, d03, d04, d06 | d01, d06, d08, d09, d10 |

All 10 evolved images built and verified in ~6 h of unattended wall clock, one
direction at a time. Each image's tag is the `evolved_ref` column of
`problems.tsv` (`<reg>/<problem>:t2t1-0915-<hash>-compiled-v1-clean`, i.e. the
name the dataset's `selection.json` records as `evolved_image`), so the
evaluation side can reference it directly.

Input bundles came from the dataset's `tasks/<task_dir>/inputs/` — 8 files, no
`patch_locations.json`, no `patch.json`. `stage-phase2-inputs.py` supplies both.

## `d8` probe: `--version` does not exist and `-e` prints no banner

`d8 --version` prints `Warning: unknown flag --version.` and continues; an
interactive `d8` start prints the `V8 version …` banner, but `d8 -e '…'` does
**not**. A verifier that greps the output for `V8 version` therefore fails both
sides of a perfectly good image — this happened, and because the batch policy
stops on a verification failure it wasted a 45-minute build before anyone looked
at *why*.

```sh
d8 -e 'print("V8", version(), "PROBE-OK", 1n << 70n)'
# → V8 14.2.0 (candidate) PROBE-OK 1180591620717411303424
```

Requiring the exact BigInt result is the point: it proves the binary executes JS,
which a startup banner does not. `verify-evolved.sh` does this.

## Host smoke before a 90-minute source build

```bash
cd /data/temporal/smoke
bash refresh.sh <problem-id> release      # regenerates build-inputs, rebuilds the
                                          # tree, replays rjob_compile_d8.sh's
                                          # compat heredoc, runs gn gen, then
                                          # prints "missing inputs: N path group(s)"
```

`refresh.sh` lifts the compatibility pass **out of `rjob_compile_d8.sh`** rather
than reimplementing it, so a green smoke cannot diverge from the container. It
found the missing `third_party/llvm-libc/BUILD.gn` in ~2 minutes for the V8 12.8
problem, which is what made that problem's fix cheap.

**`ninja` is not on `PATH`** — not on the host, not in the image (the image uses
the tree's own copy via `rjob_compile_d8.sh`).

```bash
/data/temporal/smoke/v8/third_party/ninja/ninja -C /data/temporal/smoke/out/x64.release -j16 d8
```

## Pipeline assumptions this host violates

- `$JOBS_ROOT` (`/mnt/ws-jfs/...`) is **not mounted** → `PHASE2_INPUTS_ROOT` must
  be set explicitly. Its absence looks like "no phase-two inputs found", not like
  a missing mount.
- `BUILD_JOBS` defaults to 64; 16 cores / 61 GiB here → use `--jobs 16`.
- No `buildx`: `build-context.sh` strips the BuildKit syntax line into
  `Dockerfile.legacy` and sets `DOCKER_BUILDKIT=0`. Dockerfiles must stay inside
  the legacy builder's feature set.
- nginx-free network: `github.com` is SNI-blocked, GCS only answers on
  `storage.googleapis.com` path-style URLs. See
  `../../expben-glm53-t3plus-10-2img/SOURCE-IMAGES-BLOCKED.md` for the toolchain
  vendoring that resulted (LLVM 22 toolchain + libc++/libc++abi/libunwind/
  llvm-libc, chromium's `__config_site`, and the 7 textual substitutions in
  build/ that `gn gen` enforces).

## Publishing is a separate, explicit step

Builds stay local: **never pass `--push`** to `run-pipeline.sh` /
`build-context.sh`. That flag targets the internal
`hub.i.basemind.com/security` registry, which is not what ships.

When the user asks for GHCR publication, the agreed naming is

```
ghcr.io/persistforever/v8src-<sha12>:r<N>              # source image
ghcr.io/persistforever/v8evo-<sha12>-d<NN>:r<N>        # evolved image, per direction
```

`<sha12>` = the problem id's commit prefix; **the direction number must be in the
repository name, not only in the tag** — five directions sharing
`v8evo-<sha12>` are indistinguishable in a registry listing. The credential is
already in `~/.docker/config.json` (`auths.ghcr.io`); confirm the path is unused
with `docker manifest inspect` before pushing, and retag locally first rather
than rebuilding.
