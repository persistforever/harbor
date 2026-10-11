---
name: expben-v8-upstream-convert
description: Convert an official ExploitBench bench-v8 image (ghcr.io/exploitbench/v8-r1) into the sec-bmk / cybergen runtime format used by hub.i.basemind.com/security images. Use when reproducing that conversion on another machine, when a task needs a sec-bmk-shaped image but only the upstream image is available, or when explaining how the two image families differ.
---

# Convert an upstream ExploitBench image into the sec-bmk format

Takes the *official* image for a bench-v8 problem and produces the same problem
in the shape the sec-bmk / cybergen images have: a `/testbed` task surface, a
grading layer under `/rlenv/cybergen`, the vulnerable V8 source without its
build toolchain, and no `/rlenv/source/depot_tools`.

**The problem does not change.** The output is still the same bug — e.g.
converting `cve-2024-0517` yields a sec-bmk-shaped CVE-2024-0517 image, which
belongs to the `exploitbench_v8_cve_2024_0517_*` family. It does **not** become
a cybergen synthetic problem. Those (`cybergen_v8_cr_<sha>`) have no upstream
image at all.

```
ghcr.io/exploitbench/v8-r1:<cve/crbug>            ~20 GiB compressed, 132 GiB unpacked
   │   scripts/convert.sh
   ▼
<registry>/exploitbench_v8_<bug>_<idx>:<tag>      sec-bmk format, ~10-17 GiB
```

## Where this fits: the three-step training-data pipeline

This conversion is **step 1** of three. The other two live in the sibling skill
`../expben-v8-evolve-build/`; it drives them.

| step | what | produced by | output |
|---|---|---|---|
| 1 | **baseline** — a test-set image turned into the target format | **this skill** (`convert.sh`) | `<reg>/exploitbench_v8_<bug>_<idx>:secbmk-converted` |
| 2 | **original images** — the pre-evolve image for each gerrit problem | `expben-v8-evolve-pipeline`, `--stage baseline` | `<reg>/<problem>:t2t1-baseline-v1` |
| 3 | **final images** — evolve the original, which is the training data | same pipeline, `--stage evolved` | `<reg>/<problem>:t2t1-<hash>-compiled-v1` |

Step 1 exists because steps 2 and 3 need a **format authority**: something that
says exactly what a sec-bmk image contains, checkable and reproducible. The
converted image is that authority — the sibling skill's `Dockerfile.template` is
derived from the same runtime contract.

Steps 2 and 3 are **builds**: they compile d8 × 5 configurations × 2 sides, so
budget hours per problem, not minutes. Step 1 is a conversion and costs one
build of the payload only.

### Worked smoke

Problem `cybergen_v8_cr_47c50e8013d9` (V8 CR `47c50e8013d9`, the offset-70 seed
in `score_2_from_exploitbench_trainsets/dataset.jsonl`, score 2/9):

```bash
# 1. format authority (any test-set image will do)
scripts/convert.sh --source ghcr.io/exploitbench/v8-r1:cve-2024-0517 \
  --problem-id CVE-2024-0517 --destination <reg>/exploitbench_v8_cve_2024_0517_0002:secbmk-converted

# 2. the original (pre-evolve) image for the gerrit problem
../expben-v8-evolve-pipeline/scripts/run-pipeline.sh --stage baseline \
  --problems-file <file with the problem row> --only cybergen_v8_cr_47c50e8013d9

# 3. the evolved image = training data
../expben-v8-evolve-pipeline/scripts/run-pipeline.sh --stage evolved \
  --problems-file <same file> --only cybergen_v8_cr_47c50e8013d9
```

The problem list is a TSV with
`problem_id  task_dir  source_ref  baseline_ref  evolved_ref  evolved_image`.
Keep ad-hoc lists in their own file — `run-pipeline.sh --problems-file` exists so
one-off smokes do not pollute the main `config/problems.tsv`.

## The two families, side by side

| | upstream `v8-r1` | sec-bmk (`phase3-v4`, `*-compiled-v1`) |
|---|---|---|
| `/testbed` | **absent** | present: README, patch.diff, problem/, source→symlink, grader/, .eval-fallback.sh |
| `/rlenv/cybergen` | absent | grader + provenance + runtime-contract.json |
| `/rlenv/source` | `v8/` + `depot_tools/` + `.cipd`/`.gclient*`/`v8.patch` | `v8/` only |
| `/rlenv/source/v8` | 31 GiB — keeps `out/`, `build/`, `buildtools/`, `third_party/llvm-build` | 0.8 GiB — toolchain removed, cannot rebuild V8 |
| `/rlenv/problem` | `id types eval_flags.json patch_locations.json prompt` | same 5 files |
| `/rlenv/mcp/server` | different build (9.3 MB) | different build (8.9 MB) |
| Entrypoint | `/rlenv/mcp/server` | `/bin/sh -c 'while :; do sleep 3600; done'`, WORKDIR `/testbed` |
| `/tests` | absent | present (empty) |

## Step 1 — get the upstream image (skopeo, not docker pull)

```bash
skopeo copy --retry-times 10 \
  docker://ghcr.io/exploitbench/v8-r1:cve-2024-0517 \
  dir:/data/temporal/upstream-v8-r1/cve-2024-0517
# then flatten into docker (skopeo's client is older than dockerd's min API)
skopeo copy dir:/data/temporal/upstream-v8-r1/cve-2024-0517 \
  docker-archive:/data/temporal/upstream-0517.tar:cve-2024-0517
docker load -i /data/temporal/upstream-0517.tar
docker tag cve-2024-0517:latest ghcr.io/exploitbench/v8-r1:cve-2024-0517
```

**Why not `docker pull`: it does not work here.** Every ghcr blob 307-redirects
to `pkg-containers.githubusercontent.com`, and the connection is cut every
70–200 MiB. `docker pull` does not resume per byte and simply stalls — measured
at **0 B over 150 s**, twice. `skopeo` resumes and sustains ~700–800 KB/s.

Budget: **~8 hours** for the 20 GiB, ~220 reconnects. Watch it with the partial
blob files, not `du -sm`:

```bash
ls -l <dir>/dir-put-blob*     # grows ~700 KB/s while a blob is in flight
```

`docker-archive` writes **uncompressed** layers: expect a ~65 GiB tar and a
~130 GiB image to load. Delete the tar and the `dir:` afterwards.

Verify each tag exists before pulling:

```bash
curl -s "https://ghcr.io/token?scope=repository:exploitbench/v8-r1:pull&service=ghcr.io" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'
curl -s -H "Authorization: Bearer $TOK" https://ghcr.io/v2/exploitbench/v8-r1/tags/list
```

## Step 2 — convert

```bash
scripts/convert.sh \
  --source ghcr.io/exploitbench/v8-r1:cve-2024-0517 \
  --problem-id CVE-2024-0517 \
  --destination hub.i.basemind.com/security/exploitbench_v8_cve_2024_0517_0002:secbmk-converted \
  --workdir /data/temporal/expben-convert
```

`--dry-run` stops after assembling the build context. `--push` uploads.
`--max-score N` sets the runtime contract's `max_score` (see caveats).

What it does:

1. starts a throwaway container from the upstream image;
2. **reads the build facts first** — `.git` is about to be deleted, and that is
   where the commits are:
   `HEAD` = vulnerable commit, `refs/heads/main` = fix commit,
   `/rlenv/source/depot_tools` = the depot_tools pin;
3. prunes the vulnerable tree **in place**, so the 31 GiB toolchain never
   crosses the daemon boundary;
4. copies out `binaries/ problem/ mcp/ source/`;
5. assembles the build context (`prepare_payload.py`);
6. `docker build`.

## Step 3 — verify

```bash
docker run --rm --entrypoint /bin/sh <image> -c '
  ls -A /testbed; ls -A /rlenv
  readlink /testbed/source /testbed/grader/vuln /testbed/grader/fixed
  stat -c "%U:%G %a %n" /testbed /rlenv/source /rlenv/binaries
  for c in debug debug-asan release release-asan coverage; do
    test -x /rlenv/binaries/vuln/$c/d8 || echo "missing vuln/$c"
    test -x /rlenv/binaries/fixed/$c/d8 || echo "missing fixed/$c"
  done
  /rlenv/binaries/vuln/release/d8 -e "print(version())"'
```

The Dockerfile's last `RUN` already asserts the whole contract (users and gids,
directory modes, `/testbed` = 1777, the four symlinks, the ownership sweeps), so
a successful build means those hold.

## Design decisions you may need to change

- **`--max-score`.** The reference images are internally inconsistent: their
  `runtime-contract.json` says `max_score: 9`, while their `official_grade.py`
  scores all 16 flags (`patch_image_16cap.sh` rewrote the grader but not the
  contract). This converter defaults to 9 to match the observed file; pass
  `--max-score 16` if you want the contract to agree with the grader.
- **Which MCP server.** The converter keeps the **upstream** `mcp/server`. The
  sec-bmk images ship a different build of it. Swap in a sec-bmk one if you need
  bit-for-bit parity there.
- **Provenance.** `provenance/build-provenance.json` is written from the source
  image's *real* facts (commits, file hashes, tool hashes) — never invented.
  Fields that describe cybergren's own toolchain pins do not exist upstream and
  are omitted rather than faked.
- **`/rlenv/mcp/server` size, sidecar sets, and stray `*.profraw` in
  `coverage/`** differ between the families because the toolchains differ. That
  is not something this conversion can remove; it would need a rebuild.

## What this conversion deliberately does NOT do

- It does not rebuild V8. The upstream binaries are reused as-is, so `vuln` and
  `fixed` stay exactly the upstream build.
- It does not make the image "reproduce" a sec-bmk one for the same bug — the
  sec-bmk family for these bugs lives under `exploitbench_v8_*`, not
  `cybergen_v8_cr_*`, and the two were built on different toolchains.
- It does not touch the `runtime-base` image. That is
  `hub.i.basemind.com/security/cybergen-v8-runtime-base:tools-09d27ff3-src-fab22df3`
  (1.3 GiB); it must be pullable, or pass `--runtime-base` with your own.

## Provenance of this skill

`scripts/Dockerfile.template` follows the runtime contract embedded in
`temporal/cybergen/cybergen/v8_runtime.py` (the authoritative definition of this
format), reconciled against a real `phase3-v4` image where the two differ — e.g.
the image uses `/bin/sh -c 'while :; do sleep 3600; done'` while the contract
template says `tail -f /dev/null`; this follows the image.

`assets/v8-wrapper/` is copied from `temporal/cybergen/assets/v8-wrapper/`, and
`assets/task-image-README.md` from the exploitbench skill's
`templates/task-image-README.md`. Re-copy rather than edit when they change.
