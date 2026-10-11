# expben-glm53-t3plus-10-2img — 2 个原始镜像 → 10 个 evolve 任务

从两个 job（`20261009/job_20261009025449828641` 与 `20260929/job_20260929084610989009`，
model 均为 `glm-5.3`）里选出 **glm-5.3 达到 T3 及以上**的 10 个 evolve 任务，
只覆盖 **2 个原始镜像**（每个镜像 5 个方向），这样目标机器只要构建 2 个 source 镜像。

```
2 个原始镜像 × 5 个方向 = 10 个任务

T3 × 7    15a72141745c ×5, 1b85c84da74a ×2
T2 × 2    1b85c84da74a ×2
T1 × 1    1b85c84da74a ×1（bitmap 65535，16/16 满分）
```

**为什么不是 5 个镜像**：从单个 job 里，没有任何原始镜像有 ≥5 个 T3+ 方向（最多 4）。
合并 0929 job 后才凑出两个各有 ≥5 个 T3+ 方向的镜像。

## 目录

```
expben-glm53-t3plus-10-2img/
├── README.md                     本文件
├── SOURCE-IMAGES.md              两个 source 镜像怎么自建（cybergen 三步）
├── selection.json                每题：tier / bitmap / capabilities / trial / 镜像对应
├── problems.tsv                  10 行，可直接喂 run-pipeline.sh --problems-file
├── config/problem-commits.json   本包 2 个 problem 的 fix / vulnerable commit
└── tasks/<problem>_<direction>/  10 份
    ├── task.toml                 题目契约（含 docker_image = evolve 后镜像）
    ├── instruction.md 等
    ├── source-image.json         problem_id / direction_id / 原始镜像 / evolve 镜像
    └── inputs/                   ≈48 KB：phase2.patch.diff（evolve 核心）等
```

## ⚠️ 镜像 tag 带 `-clean` 后缀

本包的 evolved 镜像 tag 是 **`t2t1-0915-*-compiled-v1-clean`**，与早先那批
`t2t1-0928-*-compiled-v1` 不同名。拉取或引用时别写错：

```
hub.i.basemind.com/security/cybergen_v8_cr_15a72141745c:t2t1-0915-ba353dad-compiled-v1-clean
hub.i.basemind.com/security/cybergen_v8_cr_1b85c84da74a:t2t1-0915-63711ad8-compiled-v1-clean
…
```

## 构建前需要准备的

| 需要什么 | 从哪来 |
|---|---|
| `expben-v8-evolve-pipeline/` 与 `skills/` | 同一个仓库，随本包一起带过去 |
| **2 个 source 镜像** `<problem>:v3-official-16cap` | **自建**（见 `SOURCE-IMAGES.md`），或从 registry 拉 |
| **构建工具链**（gn / gn-args / pkg-config / source-deps） | 目标机器上 `bootstrap.sh --ensure --problem <id>` 自建 |
| **V8 checkout**（含两题的 fix 与其父提交） | `bootstrap.sh` 会自己 `git fetch`；需能连 `chromium.googlesource.com` |
| docker（旧版 builder 即可）+ 磁盘 | 宿主 |

## 在目标机器上构建

```bash
cd ../expben-v8-evolve-pipeline
P=/path/to/expben-glm53-t3plus-10-2img

# 0. 用本包自带的 problem-commits（只含这 2 题）
cp "$P/config/problem-commits.json" config/problem-commits.json

# 1. 让 V8 checkout 具备两题的两个 revision
scripts/bootstrap.sh --ensure --problem cybergen_v8_cr_15a72141745c
scripts/bootstrap.sh --ensure --problem cybergen_v8_cr_1b85c84da74a

# 2. （可选）自建 source 镜像；若从 registry 拉则跳过
scripts/run-pipeline.sh --stage source --v8-git /data/temporal/v8 \
  --donor <任一 sec-bmk 镜像> --configs release \
  --problems-file "$P/problems.tsv"

# 3. 原始镜像（pre-evolve）与最终镜像（训练数据）
scripts/run-pipeline.sh --stage baseline --configs release --problems-file "$P/problems.tsv"
scripts/run-pipeline.sh --stage evolved  --configs release --problems-file "$P/problems.tsv"
```

`--configs release` 是单 config 快速模式；要 5 config 齐全（与已发布镜像等价）就**省略它**。

## 已知边界

- 三个继承自上游模板、本仓库已修的 bug：官方补丁 `patch --dry-run` 的阶段归属、
  `rm -f` 删目录、`ninja` 解析链。详见 `skills/expben-v8-evolve-build/SKILL.md`。
- **GC / 写屏障类的 evolve 方向会让 `mksnapshot` SIGSEGV**。本包 10 个方向都不碰
  `src/heap/`，理论上不触发。
- **只做本地镜像，不要 push。**
