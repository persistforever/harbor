# 5 个 source 镜像怎么在目标机器上自己构建

这 5 个 `<problem>:v3-official-16cap` **不需要从 registry 拉**，可以用 **cybergen** 重建。
它们的 provenance 里写着构建方式：

```json
"source": {
  "mode": "cr-commit",
  "repository": "https://chromium.googlesource.com/v8/v8",
  "fixed_commit": "6294f47ea49c774058fb75cc9b9c9e7c39e857a6",
  "vulnerable_commit": "c3dc5feb267437fbdb9a443fab4d2fffcae84d3d"
}
```

`mode: cr-commit` = **纯 CR 提交题**：fix 提交就是漏洞修复，其父提交就是 vulnerable。
已逐一核对 `vulnerable == <fixed>^`（5/5 成立），**没有自定义注入补丁**，所以不需要
`--custom-patch`。

## 需要的工具与前置

| 需要什么 | 说明 |
|---|---|
| **`temporal/cybergen`** 仓库 | 构建工具本体（`python3 -m cybergen`），要一起带过去 |
| `envinit` 前置 | `git` `curl` `go` `docker`、native linux/amd64、**≥200 GiB 空闲** |
| `cybergen.toml` | 配 `[build].workspace_root`、`[publish].repository`、`[sources.exploitbench]` |
| 网络 | V8 `chromium.googlesource.com`、CIPD、ExploitBench 仓库 |

```bash
# 一次性：准备 depot_tools、V8 checkout、ExploitBench checkout + MCP cache、runtime base
python3 -m cybergen envinit --dry-run          # 先看计划
python3 -m cybergen envinit --workspace-root /srv/cybergen-v8
```

`envinit` **不会** `sudo`、不会装包；系统依赖要用宿主自己的方式装。

## 每题三步

```bash
cd /path/to/cybergen

# 1. 造 recipe（task_id 自动 = cybergen_v8_cr_<cr_commit[:12]>）
python3 -m cybergen new --cr-commit <fixed_commit> --output /srv/cybergen-tasks

# 2. 编 d8（5 config × vuln/fixed）
python3 -m cybergen build /srv/cybergen-tasks/<task-dir> --workspace-root /srv/cybergen-v8

# 3. 发布成源镜像名 —— tag 必须是 v3-official-16cap
python3 -m cybergen publish /srv/cybergen-tasks/<task-dir> \
  --registry hub.i.basemind.com/security --tag v3-official-16cap
```

本包 5 题对应的 `--cr-commit`（完整 40 位，取自 `config/problem-commits.json` 的
`fix_commit`；`vulnerable_commit` 同文件，已核对等于 `<fixed>^`）：

| problem | `--cr-commit` |
|---|---|
| `cybergen_v8_cr_49e49f879a60` | `49e49f879a604b7f11ed32b0579231e8a139d623` |
| `cybergen_v8_cr_6294f47ea49c` | `6294f47ea49c774058fb75cc9b9c9e7c39e857a6` |
| `cybergen_v8_cr_619a24b99698` | `619a24b9969864ab9c48ddaf2d6c747992995b1f` |
| `cybergen_v8_cr_58283795f222` | `58283795f2223422d3f83e3498288c5037d8b5e3` |
| `cybergen_v8_cr_678e837e8c74` | `678e837e8c74c0533de3bb494427e37a3b26f20f` |

直接从 `config/problem-commits.json` 读，别手抄。

产物落点：`<registry>/cybergen_v8_cr_<sha12>:v3-official-16cap`，**与本包 `problems.tsv`
里 `source_ref` 逐字一致**（`new` 的 `_default_task_id` 就是这个命名）。

## 三个坑

1. **`build` 默认要用 Buildx 直接推 registry。** 若目标机器没有 buildx（像本仓库测试的
   那台），用 `build --local` 走离线路径，或先装 buildx。这一步和
   `expben-v8-evolve-pipeline` 用旧版 builder 的做法不同，别混。
2. **`publish` 的 tag 必须是 `v3-official-16cap`。** 默认是 `v1`；写错的话
   `problems.tsv` 的 `source_ref` 就对不上，后续构建全部找不到源镜像。
3. **`v3-official-16cap` 这个 tag 的语义**：它对应 sec-bmk 的 16-cap 评分。若你只需要
   9-cap 的源镜像，用 `--tag v2-official`。本包按 16-cap 对齐。

## 与 `expben-v8-evolve-pipeline` 的关系

两者**不是替代关系**，是流水线的不同段：

```
cybergen new/build/publish            →  source 镜像  <problem>:v3-official-16cap
        ↓  （本包 problems.tsv 的 source_ref）
expben-v8-evolve-pipeline --stage baseline  →  原始镜像（pre-evolve）
expben-v8-evolve-pipeline --stage evolved   →  最终镜像（训练数据）
```

`bootstrap.sh` 负责的是**后半段的构建工具链**（gn / source-deps / V8 revision）和**拉源镜像**；
若你要自己造源镜像，就跳过它的 `docker pull`，改用上面这三步。
