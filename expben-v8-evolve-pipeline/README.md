# ExploitBench V8 evolve pipeline (self-contained)

> Operational skill for this flow: [`../skills/expben-v8-evolve-build/SKILL.md`](../skills/expben-v8-evolve-build/SKILL.md)
> — read it first if you are driving a build rather than changing the scripts.

Reproduces — inside this repository — the build chain that turns an
**ExploitBench test-set wrapper image** into a rebuilt image, in two stages:

```
ExploitBench test-set image   (…:v3-official-16cap)
  │
  ├─[A] baseline   rebuild the UNMODIFIED vulnerable d8 with real GN+ninja
  │                →  <registry>/<problem>:t2t1-baseline-v1
  │
  └─[B] evolved    apply the phase-two evolution patch to the vulnerable source,
                   rebuild, then ship the evolved image
                   →  <registry>/<problem>:t2t1-….compiled-v1
```

## How a build actually works

Both stages use one mechanism: generate a **self-contained Docker build
context** and build it with the local docker daemon.

The test-set image ships the vulnerable V8 *source* but not a build toolchain
that matches its own revision, so the context carries the fixes:

- the builder stage **deletes** the image's own `build/`, `buildtools/`,
  `third_party/libc++`, `third_party/llvm-build`, … and `ADD`s a single
  `build-inputs/source-deps.tar` in their place.  Merging deps *on top of* the
  image instead of replacing them does **not** work — the image's stale libc++
  wins and the build dies in `third_party/libc++/src/include/cctype`.
- `scripts/rjob_compile_d8.sh` then: applies the phase-two patch, patches
  `gni/v8.gni` and `.gn`, adds the missing `#include <vector>` to the ~129
  headers that relied on a transitive include the newer libc++ dropped, and
  appends compatibility GN args (`use_llvm_libatomic=false`,
  `use_llvm_libcxx_modules=false`, `use_siso=false`, …).
- the final stage copies back **only** the rebuilt d8 sets (and, for evolved,
  the changed source file plus the evolved patch/prompt), then diffs `/testbed`
  against a pristine snapshot so nothing extra leaks into the image.

### Two things the shipped image does not inherit

- **`/testbed/README.md` is re-rendered** from `templates/task-image-README.md`
  in *both* stages.  The test-set image's own README is stale — it still
  describes the old nine-capability sec-bmk score, while the grader in use
  reports the full sixteen-flag bitmap.  The rendered text drops the template's
  `problem/task.json` tree entry, which the images do not ship.
- **`/testbed/problem/task.json` is deleted from the produced image.**  The
  test-set images *do* ship it (1980 bytes of build provenance: fix commit,
  canonical-patch hash, patch-locations parser pin, upstream ExploitBench
  commit), so both stages `rm -f` it in the final stage.  The same facts remain
  in the per-problem `build-inputs/manifest.json` outside the image.

Baseline mode is the same context with an **empty** phase-two patch: the
vulnerable source is compiled exactly as the test-set image ships it, and the
final stage ships only the rebuilt binaries.

## Layout

```
expben-v8-evolve-pipeline/
├── config/
│   ├── pipeline.env        site config (registry, cache root, paths, parallelism)
│   ├── problems.tsv        the problems to process, one row each
│   ├── problem-commits.json  fix/vulnerable commit + source image per problem
│   └── cache-manifest.json   pins for every fetched dependency
├── assets/                 vendored small inputs (gn-args-vuln/, pkg-config-wrapper.sh)
├── scripts/
│   ├── bootstrap.sh              cache-then-fetch for every external dependency
│   ├── prepare_problem_build_inputs.py  regenerates a problem's build inputs
│   ├── prepare_context.py        context generator (ported; adds --mode baseline)
│   ├── rjob_compile_d8.sh        in-container GN/ninja driver (ported; empty-patch guard)
│   ├── build-context.sh          local `docker build` of a context (legacy builder)
│   ├── run-pipeline.sh           driver over problems.tsv
│   ├── check_task_files.py, task_prompt.py, patch_locations_derive.py   (ported)
├── templates/task-image-README.md   canonical /testbed/README.md source (ported)
├── vendor/bench-v8-env-builder/patch_locations.py                        (ported)
└── out/                    logs + build evidence (git-ignored)
```

Generated contexts go to `$CONTEXT_ROOT` (default `/data/temporal/expben-evolve-contexts`),
**not** into this repository — each is ~3.4 GiB.

## Why the legacy builder

The contexts' Dockerfiles start with `# syntax=docker/dockerfile:1`, which
forces BuildKit; this host has no `buildx` plugin.  `build-context.sh` strips
that directive into `Dockerfile.legacy` and sets `DOCKER_BUILDKIT=0`.  The
Dockerfiles use only multi-stage `FROM` / `ADD <tar>` / `COPY --from`, all of
which the legacy builder supports.  Install buildx instead if you prefer.

## Usage

```bash
cd temporal/harbor/expben-v8-evolve-pipeline

# See the plan without building (pass a phase-two input root if you have one).
scripts/run-pipeline.sh --stage both --limit 1 --dry-run

# Build the pre-evolve image for one problem.
scripts/run-pipeline.sh --stage baseline --only cybergen_v8_cr_489887978dfb

# Build the evolved image (phase-two inputs are auto-discovered under JOBS_ROOT
# by matching the destination tag; override with PHASE2_INPUTS_ROOT).
scripts/run-pipeline.sh --stage evolved --only cybergen_v8_cr_489887978dfb

# Whole set, both stages, pushing into your own registry.
scripts/run-pipeline.sh --stage both --push --registry 100.96.192.165:8080/security
```

Useful flags: `--configs release` (fast single-config smoke),
`--jobs N`, `--overwrite` (replace an existing generated context).

## Provenance / keeping in sync

`prepare_context.py`, `rjob_compile_d8.sh`, `check_task_files.py`,
`task_prompt.py`, `patch_locations_derive.py`,
`templates/task-image-README.md` and
`vendor/bench-v8-env-builder/patch_locations.py` are ports of the authoritative
files under
`github/cyber-collections/securitybenchmarks/skills/cyber-security/exploitbench-v8-t2t1-problem-evolve/`.
The intended divergences are exactly four:

1. `--mode baseline` (empty patch, trimmed final stage);
2. an `[ -s "$PHASE2_PATCH" ]` guard so both modes share one compile script;
3. `task.json` dropped from the required inputs;
4. the rendered README drops the template's `problem/task.json` tree entry
   (see `README_PROBLEM_TAIL`) — the template should probably be corrected
   upstream, after which that transform is a no-op.

Re-copy the upstream files when they change rather than editing here.

## Cost

Measured on this box for one problem, `release` only, `--jobs 48`: **~40 min**
(≈20 min of that is context upload + tar unpack).  Three configurations and ten
problems is therefore an overnight job — run it serially, and watch `/data`
(each context is 3.4 GiB and each image ~10 GiB).

## Known failure modes

- `debug` / `debug-asan` are OOM-prone component builds that need their
  `lib*.so` sidecars; they are not in the default config set.
- A bare `Killed` with no `error:` in the log means the OOM killer, not a code
  problem — re-run.
- `source-deps/third_party/rust` must come from the image overlay; regenerate
  the build inputs rather than substituting a stub.
