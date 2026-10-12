---
name: expben-v8-evolve-build
description: Build ExploitBench V8 images locally from the official test-set wrapper images — first the pre-evolve (baseline) image, then one evolved image per evolution direction. Use when asked to rebuild, reproduce, verify or re-derive a cybergen_v8_cr_* image (especially a t2t1-*-compiled-v1 evolved image) on this host, when a build context has to be regenerated from build-inputs, or when explaining why a rebuilt V8 image does or does not match the published one. Start at "Fast path" for the command sequence that builds N directions with the fewest surprises.
---

# ExploitBench V8 evolve build

Two stages, one mechanism. Everything runs in the local docker daemon; no
cluster, no kaniko, no buildx.

```
ExploitBench test-set image   (…:v3-official-16cap)
  ├─[A] baseline   rebuild the UNMODIFIED vulnerable d8  → <reg>/<problem>:t2t1-baseline-v1
  └─[B] evolved    apply the phase-two patch, rebuild   → <reg>/<problem>:t2t1-….compiled-v1
```

Implementation lives in `../../expben-v8-evolve-pipeline/` (sibling of this
skill's parent). Drive everything through its `scripts/run-pipeline.sh`.

## Fast path — build N directions

**Scripts in `scripts/` are the fast path; the prose below is the reasoning.**

| script | what |
|---|---|
| `stage-phase2-inputs.py` | a dataset bundle (`tasks/<task_dir>/inputs/`) → the per-task `$PHASE2_INPUTS_ROOT/<task_dir>/` layout, deriving the one file the bundle lacks |
| `run-evolved-batch.sh` | build every unbuilt (or unverified) row of a `problems.tsv`, one at a time, verifying each — idempotent, so re-running resumes |
| `verify-evolved.sh` | one image against its inputs and its source image; 15 s; the check that catches a silently wrong side |

`references/pitfalls-and-smoke.md` has the rest of what the 2026-10 run taught:
the worked 2-problem/10-direction example with timings and image ids, the `d8`
probe gotcha, the host smoke recipe, the host assumptions that differ from the
pipeline's, and the GHCR publishing convention.

```bash
cd ../../expben-v8-evolve-pipeline
SKILL=../skills/expben-v8-evolve-build                     # this skill

# 0. stage the phase-two inputs, one directory per direction.
#    (Bundles arrive as a dataset: <dataset>/tasks/<task_dir>/inputs/.)
python3 "$SKILL/scripts/stage-phase2-inputs.py" \
  --bundles <dataset-with-tasks-dir> --selection <dataset>/selection.json \
  --output /data/temporal/phase2-inputs-local --overwrite
export PHASE2_INPUTS_ROOT=/data/temporal/phase2-inputs-local

# 1. seconds: does each direction's patch apply to its pristine vuln tree?
mkdir -p /data/temporal/patchcheck
while read -r prob task _; do
  [ "$prob" = problem_id ] && continue
  tree=/data/temporal/patchcheck/$prob
  [ -d "$tree/src" ] || { mkdir -p "$tree"; git -C /data/temporal/v8 archive \
      "$(python3 -c "import json;print(json.load(open('config/problem-commits.json'))['$prob']['vulnerable_commit'])")" \
      | tar -x -C "$tree"; }
  patch -d "$tree" -p1 --dry-run --forward \
    < "$PHASE2_INPUTS_ROOT/$task/phase2.patch.diff" >/dev/null && echo "OK  $task" || echo "BAD $task"
done < <(tail -n +2 <problems.tsv>)

# 2. seconds: is the row, tag, context dir and input dir what you think?
scripts/run-pipeline.sh --stage evolved --only <problem> --task <task_dir> \
  --problems-file <problems.tsv> --configs release,release-asan,coverage \
  --jobs 16 --dry-run

# 3. the long part: ~40 min per direction, sequential, verify after each
"$SKILL/scripts/run-evolved-batch.sh" --problems-file <problems.tsv> --jobs 16
```

**Prerequisites, all of which fail fast if missing:** per-problem
`build-inputs` (`scripts/bootstrap.sh` reports them), the source image for the
problem in the local daemon (`--stage source` builds it, ~90 min, and it compiles
*both* sides × 3 configs), `PHASE2_INPUTS_ROOT` staged, and the V8 checkout
holding each problem's `vulnerable_commit` object.

**Measured on this host** (`--configs release,release-asan,coverage --jobs 16`,
one direction = 3 configs of one side):

| stage | V8 14.2 problem | V8 12.8 problem |
|---|---|---|
| `--stage source` (both sides) | 89m38s | 76m10s |
| one evolved direction | ~44 min | ~36 min |
| context generation | ~2–4 min (3.6 GiB tar) | same |
| verify one image | 15 s | 15 s |

Shipped 10 directions (2 problems × 5) in ~6 h of wall clock, unattended.

**Do not run two builds at once.** 16 cores / 61 GiB: `--jobs 16` already uses
the whole machine, and the asan link steps are the peaks. `BUILD_JOBS=64` (the
`pipeline.env` default) is 64 concurrent clang processes ≈ 64 GiB — it does not
fit here, which is why the batch script clamps to 16.

## Read this before touching anything

A build is a Dockerfile context whose builder stage **deletes the image's own
build toolchain and replaces it wholesale**:

```dockerfile
RUN rm -rf /rlenv/source/v8/{out,build,buildtools,third_party/libc++,
                            third_party/llvm-build,gni,test,tools/clang,…}
ADD build-inputs/source-deps.tar /rlenv/source/v8/
```

**Do not "improve" this into copying dependencies on top of the image.** The
test-set image ships a build toolchain that does not match its own V8 revision;
merging new deps over it leaves the stale libc++ winning and the compile dies in
`third_party/libc++/src/include/cctype` on `__has_include(<ctype.h>)`. This is
the single most expensive mistake available here — it costs a full context
upload before failing.

`scripts/rjob_compile_d8.sh` then does the three compatibility passes that make
an old V8 revision build with the newer injected toolchain:

1. adds the missing `#include <vector>` to ~129 headers under `src/` and
   `include/` that relied on a transitive include libc++ no longer provides;
2. appends `use_siso=false`, `treat_warnings_as_errors=false`,
   `use_llvm_libatomic=false`, `use_libcxx_modules=false`, `use_cxx23=false`,
   `v8_use_perfetto_json_export=false`;
3. patches `gni/v8.gni` and `.gn` (`exec_script_whitelist` → `_allowlist`,
   `expand_directory_allowlist`).

Baseline mode is the *same* context with an **empty** phase-two patch, so the
pristine vulnerable source is compiled. That is the whole difference between the
two stages.

### Revisions older than 2025 also need a `third_party/llvm-libc/BUILD.gn`

The injected libc++ is a 2025 one, and `buildtools/third_party/libc++/BUILD.gn`
depends on `//third_party/llvm-libc:llvm-libc-shared` (LLVM 22's `charconv`
reaches into llvm-libc's shared headers). `third_party/llvm-libc` is a **V8 2025
fork** — a V8 12.8 (2024-06) checkout has no such directory, and `gn gen` dies
before compiling anything:

```
ERROR at //buildtools/third_party/libc++/BUILD.gn:228:14:
Unable to load "…/third_party/llvm-libc/BUILD.gn".
```

`prepare_problem_build_inputs.py::write_llvm_libc_build_gn()` writes the two-line
group from `chromium-compat-a6fc5852/third_party/llvm-libc/BUILD.gn.2025` —
**but only when `git cat-file -e <vulnerable>:third_party/llvm-libc/BUILD.gn`
says the revision has none.** The 2025 revisions ship the real fork inside their
own tarball, and `ADD source-deps.tar` lands on top of that tree, so a generic
file must never overwrite it. Any problem whose V8 predates 2025 needs this;
there is no way to see it from the revision number alone, and `--stage source`
hits it first.

## Bootstrap a clean host (cache, else fetch)

Nothing in the pipeline downloads on its own.  Every external dependency is
looked up in a **cache root** first and only fetched when it is absent:

```bash
cd ../../expben-v8-evolve-pipeline
scripts/bootstrap.sh                                   # report only; exit 1 if anything is missing
scripts/bootstrap.sh --ensure                          # fetch what is missing
scripts/bootstrap.sh --ensure --problem cybergen_v8_cr_489887978dfb
scripts/bootstrap.sh --ensure --with-system-deps       # also run install-build-deps.sh (needs sudo)
```

`CACHE_ROOT` (default `/data/temporal`) holds everything below.  On a warm
cache the script is a pure read and prints `[cached]` for every item.

| cache item | where | authoritative source | pin |
|---|---|---|---|
| host tools | — | the host's package manager | `docker python3 git curl unzip` |
| depot_tools | `$CACHE_ROOT/depot_tools` | `chromium.googlesource.com/chromium/tools/depot_tools.git` | `fa754cb8cf5e2f181b070e9eb4f93083cc367696` |
| V8 checkout | `$CACHE_ROOT/v8` | `chromium.googlesource.com/v8/v8.git` via `gclient sync -D` | `ea34886326b8178ac039de5960968fae4662ee9f` |
| GN | `$CACHE_ROOT/gn-28df2d93/gn` | CIPD `gn/gn/linux-amd64` | `git_revision:28df2d93df29e5f773dd51ed2f19c112987626ed` |
| GN args | `$CACHE_ROOT/gn-args-vuln` | **vendored** in `assets/gn-args-vuln` | — |
| pkg-config shim | `$CACHE_ROOT/pkg-config-wrapper.sh` | **vendored** in `assets/` | — |
| test-set image | docker daemon | `$REGISTRY/<problem>:v3-official-16cap` | tag — pass `--base-image …@sha256:…` to pin by digest |
| build inputs | `$CACHE_ROOT/expben_v8_build/problems/<problem>/build-inputs` | generated by `scripts/prepare_problem_build_inputs.py` | per-problem fix commit |

Pins live in `config/cache-manifest.json`; the per-problem commits live in
`config/problem-commits.json`.  The V8/depot_tools half of this mirrors
`temporal/cybergen/references/linux-v8-workspace.md`, which is **authoritative**
for it — read that before changing the clone/sync steps here.

Two things bootstrap deliberately does not do:

- it never runs `sudo` on its own; system packages are reported, and
  `install-build-deps.sh` only runs behind `--with-system-deps`;
- it never invents a fix commit.  Adding a new problem means adding its
  `fix_commit` to `config/problem-commits.json` first — the generator can derive
  the vulnerable revision from it (`<fix>^`) without any phase-two dataset.

### The V8 checkout must reach each problem's revision

A fresh `gclient sync` only has what the pinned `ea348863` branch reaches, but
`prepare_problem_build_inputs.py` runs `git archive <vulnerable>` and resolves
`<fix>^` — both of which need those exact objects.  `bootstrap.sh --ensure
--problem <id>` therefore **fetches them itself** before generating:

```text
revision present: 6294f47ea49c
fetching e51fdffd126d into /data/temporal/v8
```

It tries `git fetch origin <sha>` first (chromium.googlesource.com serves
reachable SHAs) and falls back to a full `git fetch origin`.  If a revision is
still missing afterwards it says so and stops — it does not guess.  This needs
network access to `chromium.googlesource.com`; there is no offline path.

### Growing this skill

The intended workflow is that this file is edited as gaps are found: any step
that was needed on a fresh host but is missing here is a bug in the skill, not
in the operator.  When a fetch is added, record it in
`config/cache-manifest.json` **and** the table above so the two cannot drift.

## `--stage source` — build the source image, no cybergen

The per-problem source image (`<registry>/cybergen_v8_cr_<sha12>:v3-official-16cap`)
is what an evolve direction starts from.  **cybergen is not required to make
one** — this pipeline builds it from a V8 checkout:

```bash
scripts/run-pipeline.sh --stage source \
  --only cybergen_v8_cr_6294f47ea49c \
  --v8-git /data/temporal/v8 \
  --donor <any sec-bmk image>          # supplies /rlenv layout, users, MCP, wrapper
```

It writes `pipeline.env`'s `CONTEXT_ROOT/<problem>-source` and builds it to
`<REGISTRY>/<problem>:v3-official-16cap`.  `--stage source` iterates
`config/problem-commits.json` (per **problem**), not `problems.tsv` (per task) —
otherwise the 5 problems × 2 directions of a task list would rebuild each source
image twice.

### What it has to do that the other stages do not

1. **Build both sides.** Every sec-bmk image carries `vuln` *and* `fixed` d8, and
   the grader's `diff` capability is the difference between them.  The
   baseline/evolved stages only ever refine the vulnerable side, taking `fixed`
   from the image they started from — impossible for a brand-new problem.
   `rjob_compile_d8.sh` now takes `PHASE2_BINARY_SIDE` (default `vuln`) and is
   run once per side against the matching tree.
2. **Reconstruct the problem metadata.** `prepare_source_context.py` derives it
   and it is verified byte-for-byte against a real source image — see below.

### The recipe, and how it was verified

```text
problem_id      = f"cybergen_v8_cr_{fix[:12]}"
vulnerable      = <fix>^
bug_description = f"V8 vulnerability task generated from CR {fix[:12]}: {subject}"
prompt          = assets/v8-prompt.template + those two + the patch
patch.diff      = git diff <vuln> <fix>
patch_locations = patch_locations_derive.py (the vendored ExploitBench parser)
id/types/eval_flags = constants (16 `1day_*` capability names)
```

Regenerating those six files for `cybergen_v8_cr_6294f47ea49c` and diffing
against the published `v3-official-16cap` image gave **6/6 byte-identical**.

**Use `git -c core.abbrev=40 diff`.** Plain `git diff` abbreviates blob hashes.
Note that `git diff --no-abbrev` is accepted but does **nothing** for diff — the
flag exists for `show`/`log`. The shipped patches carry all 40 characters, and
the prompt embeds the patch verbatim, so an abbreviated hash silently changes
two of the six files.

### Why the Dockerfile is multi-stage

Each side needs the ~3.5 GiB toolchain injected.  A `rm -rf` in a later layer
does not remove it from the image, so a single-stage two-pass build would ship
~70 GiB.  `source.Dockerfile.template` uses one builder per side and copies only
the finished `/rlenv/binaries/*` and the pruned source tree into the final
stage.

## Run it

```bash
cd ../../expben-v8-evolve-pipeline        # relative to this SKILL.md

scripts/run-pipeline.sh --stage baseline --only cybergen_v8_cr_489887978dfb
scripts/run-pipeline.sh --stage evolved  --only cybergen_v8_cr_489887978dfb
scripts/run-pipeline.sh --stage both --dry-run          # plan only, all problems
```

Flags that matter: `--task SUBSTRING` (run one evolution direction),
`--problems-file PATH` (keep one-off lists out of `config/problems.tsv`),
`--configs release` (fast single-config smoke), `--jobs N`, `--overwrite`
(replace an existing context), `--dry-run` (print the resolved row, tag,
context and input directory), `--push`, `--registry <ns>`.

**Rows are per direction, but a context used to be per problem.** A problem has
one row per evolution direction (d01, d02, …) with a different phase-two patch
each, and they all wrote `$CONTEXT_ROOT/<problem>-evolved` — whichever ran last
won. Both are per task now: `$CONTEXT_ROOT/<task_dir>-evolved`, and
`--task` is what makes one row at a time expressible (`--only` matches the
problem only). Combine them: `--only <problem> --task <task_dir>` runs exactly
one row.

Inspect a finished image without building again:

```bash
docker run --rm -it --entrypoint /bin/bash <image>
/rlenv/binaries/vuln/release/d8 -e 'print(version())'
```

## The official-patch dry-run belongs in the builder stage

The upstream template checks that the official vuln→fix patch still applies:

```dockerfile
patch -d /testbed/source -p1 --dry-run < /testbed/patch.diff
```

It puts that check in the **final** stage.  That is wrong for any problem whose
patch touches a directory the base image strips — and `test/` is stripped in the
test-set images.  The final stage starts from the pristine base image and only
copies back the rebuilt binaries, so its `/rlenv/source/v8` has no `test/`:

```
checking file src/heap/remembered-set.h
checking file test/mjsunit/sandbox/regress-329345899.js
can't find file to patch at input line 438
|+++ b/test/unittests/heap/base/basic-slot-set-unittest.cc
Skipping patch.  7 out of 7 hunks ignored
```

It passes only for patches that stay inside `src/`.  This pipeline runs the
dry-run in the **builder** stage instead, right after `ADD source-deps.tar` and
**before** `rjob_compile_d8.sh` — only the builder has the full tree, and the
tree is still pristine there (the compatibility pass inserts `#include <vector>`
into ~129 headers and would otherwise invalidate the check).

**Both stages are affected, and the evolved stage fails more often.**  An evolve
direction frequently *adds* a regression test, so the evolved patch carries a
`+++ b/test/mjsunit/...` hunk by construction.  The evolved stage's dry-run has
therefore also been removed; its `grep -Fq -- '+++ b/<path>'` checks still assert
the shipped patch's content, which is the part that actually matters.

### `rm -f` on a directory

The same final stage ends with

```dockerfile
rm -f /tmp/expected-problem.list /tmp/pristine-testbed.list /tmp/expben-skill
```

`/tmp/expben-skill` is a **directory** (the skill scripts are COPIed there), and
`rm -f` refuses directories — so this returns non-zero and fails the build with
`rm: cannot remove '/tmp/expben-skill': Is a directory`.  Inherited from the
upstream template; this pipeline uses `rm -rf`.

Both of these are only reachable in the **evolved** stage, which is why the
baseline smoke passes while the evolved one fails.

## Reproducibility — measured

Rebuilding `cybergen_v8_cr_489887978dfb` direction `066a7856` with
`--configs release` produced a `release` d8 **bit-identical** to the published
`t2t1-0928-066a7856-compiled-v1`:

```
release d8   rebuilt 64c0b31cea2de110   published 64c0b31cea2de110   ← identical
debug        rebuilt d1487f507fb3ca6e   published 015f0fa8d7a12f26
release-asan rebuilt d3e69ff36398f839   published 825ad5fa71178092
```

The other configurations differ because the smoke builds only `release`; the
rest stay as the base image shipped them.  Structure (testbed layout, problem
files, symlinks, modes) matched exactly.

## Local only — never push

This workflow produces **local docker images**; the machine that builds them is
not the machine that consumes them.  Do not pass `--push`, and do not read a
pipeline run that omitted `--push` as an incomplete one.

`test/**` also matters for a second reason: a fix commit often **adds** new test
files, which are absent from the vulnerable revision by construction.  The
patch carries them as new-file hunks, which `patch` creates, so their absence is
correct — but the dry-run must still run somewhere the surrounding tree exists.

## What silently goes wrong

- **`--configs` decides what is actually rebuilt.** The final stage only
  `rm -rf` + `COPY --from=builder`s the configurations listed. A
  `--configs release` run leaves `debug`, `debug-asan`, `release-asan` and
  `coverage` present but still the *test-set image's originals*. All five
  directories exist either way — check timestamps or the image manifest before
  claiming a full baseline.
- **Phase-two inputs resolve per task first, then per problem, then by search.**
  `PHASE2_INPUTS_ROOT/<task_dir>` wins, then `PHASE2_INPUTS_ROOT/<problem>`
  (the historical layout, one bundle per problem — which can only describe one
  of its five directions), and only if neither exists does it grep
  `$JOBS_ROOT/*/*/kaniko-contexts/<problem>/` for the destination tag. **This
  host has no `$JOBS_ROOT`** (`/mnt/ws-jfs/...` is not mounted), so
  `PHASE2_INPUTS_ROOT` must be set explicitly or the evolved stage finds
  nothing. Whichever path it takes, confirm the resolved `inputs=` line in
  `--dry-run`: a per-problem directory silently supplies the wrong direction's
  patch, and the build succeeds.
- **A bundle is one file short.** `patch_locations.json` is absent from the
  per-task bundles; `prepare_context.py` requires it, so `stage-phase2-inputs.py`
  derives it with the vendored parser. Derive it from
  `evolved-vuln-to-fixed.patch.diff` (= the shipped `/testbed/patch.diff`), not
  from the phase-two patch: measured byte-identical to what cybergen itself
  produced for the one direction whose bundle did carry the file.
- **Legacy builder, not BuildKit.** This host has no `buildx`; the contexts'
  `# syntax=docker/dockerfile:1` line is stripped into `Dockerfile.legacy` and
  `DOCKER_BUILDKIT=0` is set. Dockerfiles must therefore stay within the legacy
  builder's feature set — no `RUN --mount`, no heredocs.
- **`task.json` is deleted from the produced image.** The test-set images *do*
  ship `/testbed/problem/task.json` (1980 bytes of build provenance: fix commit,
  canonical-patch hash, patch-locations parser pin, upstream ExploitBench
  commit).  Both stages therefore `rm -f` it in the final stage, and the
  generator neither requires nor copies it.  The provenance is not lost — the
  same facts live in the per-problem `build-inputs/manifest.json` outside the
  image.
- **`/testbed/README.md` is always re-rendered**, never inherited. Both stages
  copy a README rendered from `templates/task-image-README.md`; the test-set
  image's own copy is stale and still documents the old nine-capability sec-bmk
  score, while the grader in use reports the full sixteen-flag bitmap. The
  rendered text drops the template's `problem/task.json` tree entry for the
  reason above.

## Cost, honestly

The per-direction numbers are in the Fast path table (**~40 min for three
configs of one side**, ~90 min for a source image's two sides). Each generated
context is ~3.6 GiB and each image ~16 GiB on disk, against 493 GiB total here —
ten directions is ~160 GiB, so watch headroom and delete a context once its
image is tagged and verified. Ten problems × two stages × five configurations is
an overnight run, not an interactive one; go one direction at a time.

Two things make an attempt cheaper to abandon:

- a patch that does not apply fails **~2 minutes** in (`patch --forward` under
  `set -Eeuo pipefail`), not after 45 minutes — so a bad bundle costs minutes,
  which is why step 1 of the Fast path is worth its seconds;
- the compile is a `builder` stage and the prompt/README are only `COPY`ed in
  the final stage, so re-shipping prose is a re-tag, not a rebuild:
  `scripts/build-context.sh --context <ctx> --destination <tag> --keep-context`
  takes 30 seconds. Do **not** re-drive `run-pipeline.sh` for that — it
  regenerates the context, re-tars 3.6 GiB, and throws the compile cache away.

## Keeping this honest

`prepare_context.py` and `rjob_compile_d8.sh` here are ports of the authoritative
files under
`github/securitybenchmarks/skills/cyber-security/exploitbench-v8-t2t1-problem-evolve/`.
The only intended divergences are `--mode baseline`, the `[ -s "$PHASE2_PATCH" ]`
guard, and the dropped `task.json`. When upstream changes, re-copy rather than
editing in place.
