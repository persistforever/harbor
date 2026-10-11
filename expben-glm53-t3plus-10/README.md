# expben-glm53-t3plus-10 — 10 个 evolve 任务的定义与对应关系

从 `job_20261009025449828641`（model `glm-5.3`）里选出 **glm-5.3 达到 T3 及以上**的
10 个 evolve 任务，覆盖 **5 个原始镜像**，每个镜像贡献 2 个方向。

**本包只装"要构建什么"，不装构建环境。** d8 在目标机器上编译。

```
5 个原始镜像 × 2 个方向 = 10 个任务
  T1 × 2    cybergen_v8_cr_58283795f222
  T2 × 4    cybergen_v8_cr_49e49f879a60 ×2, 6294f47ea49c ×1, 619a24b99698 ×1
  T3 × 4    6294f47ea49c ×1, 619a24b99698 ×1, 678e837e8c74 ×2
```

## 目录

```
expben-glm53-t3plus-10/
├── README.md                     本文件
├── selection.json                每题：tier / bitmap / capabilities / trial / 镜像对应
├── problems.tsv                  10 行，可直接喂 run-pipeline.sh --problems-file
├── config/problem-commits.json   本包 5 个 problem 的 fix / vulnerable commit
└── tasks/<problem>_<direction>/  10 份
    ├── task.toml                 题目契约（含 docker_image = evolve 后镜像）
    ├── instruction.md 等         harness 侧任务文件
    ├── source-image.json         problem_id / direction_id / 原始镜像 / evolve 镜像
    └── inputs/                   ≈48 KB：evolve 的核心数据
        ├── phase2.patch.diff          ← 这道题的 evolve diff（最关键，丢了就没了）
        ├── base-vuln-to-fixed.patch.diff
        ├── evolved-vuln-to-fixed.patch.diff
        ├── prompt / patch_locations.json / task.json / compile-inputs.json
        └── evolution-image.json
```

## 构建前需要准备的（**不在本包内**）

| 需要什么 | 从哪来 |
|---|---|
| `expben-v8-evolve-pipeline/` 与 `skills/` | 同一个仓库，随本包一起带过去 |
| **5 个 source 镜像** `<problem>:v3-official-16cap`（共 ~48 GB） | **自己构建**（见 `SOURCE-IMAGES.md`，用 cybergen 三步），或从 registry 拉 |
| **构建工具链**（gn / gn-args / pkg-config / source-deps） | 目标机器上 `bootstrap.sh --ensure --problem <id>` 自建 |
| docker（**旧版 builder 可用即可，不需要 buildx**）+ ~50 GB 磁盘/题 | 宿主 |

> **source 镜像是 `mode: cr-commit` 的纯 CR 题**：`fix` 提交 + 其父提交即 vulnerable，
> 无自定义注入补丁。用 `cybergen new --cr-commit <fix> → build → publish --tag
> v3-official-16cap` 重建，`--cr-commit` 的 5 个 SHA 在 `config/problem-commits.json`。
> 细节与三个坑见 [`SOURCE-IMAGES.md`](SOURCE-IMAGES.md)。

`bootstrap.sh` 会自己 `git fetch` 每题需要的 V8 revision；这需要能连
`chromium.googlesource.com`，**没有离线路径**。

## 在目标机器上构建

```bash
# 1. 工具链 + 源镜像（有 cache 就纯读，没有才拉）
cd ../expben-v8-evolve-pipeline
config=/path/to/expben-glm53-t3plus-10/config
scripts/bootstrap.sh --ensure --problem cybergen_v8_cr_49e49f879a60   # 每题一次
#   或直接 docker login && docker pull <每题 source 镜像>

# 2. 用本包自带的 problem-commits（只含这 5 题）
cp /path/to/expben-glm53-t3plus-10/config/problem-commits.json config/problem-commits.json

# 3. 构建
scripts/run-pipeline.sh --stage baseline --configs release \
  --problems-file /path/to/expben-glm53-t3plus-10/problems.tsv
scripts/run-pipeline.sh --stage evolved  --configs release \
  --problems-file /path/to/expben-glm53-t3plus-10/problems.tsv
```

`--configs release` 是快速模式；要 5 个 config 齐全就省略它（每题约 5 倍时间）。

## ⚠️ 本包**不含** build inputs，这是有意的

早期版本打算连 `source-deps.tar`（**每题 3.6 GB**）一起存，那是 V8 的**构建工具链**
（`build/`、`buildtools/`、`third_party/llvm-build`、`tools/clang`…），不是"这道题的数据"：

- 它由 `bootstrap.sh` 从 V8 checkout + CIPD 重建得到；
- 其中 `build/`、`buildtools/`、`llvm-build` 是**跨题相同**的，`gni/`、`test/` 才随题变化。

存它会把包从 **825 KB** 撑到 ~36 GB，且对能连网的机器是纯冗余。

## 已知边界（构建前请读）

- 三个继承自上游模板、已在本仓库修掉的 bug：官方补丁 `patch --dry-run` 的**阶段归属**、
  `rm -f` 删目录。详见 `skills/expben-v8-evolve-build/SKILL.md`。
- **GC / 写屏障类的 evolve 方向会让 `mksnapshot` SIGSEGV**，而上游的重试兜底只覆盖
  "堆校验器签名"，不覆盖裸 SIGSEGV。本包 10 个方向都不碰 `src/heap/`，理论上不触发；
  但如果你换成 GC 类方向，就会撞上（解法：复用 baseline 的 `snapshot_blob.bin`）。
- **只做本地镜像，不要 push。**
