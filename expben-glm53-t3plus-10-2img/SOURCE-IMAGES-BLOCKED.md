# source 镜像本地构建被卡住：V8 源码 revision 与构建工具链相差一年半

**状态**：阻塞，需要一台能连 `chromium.googlesource.com` 的机器提供产物。
**影响**：本包 2 个 source 镜像在受限网络机器上无法用
`expben-v8-evolve-pipeline --stage source` 造出来。

| source 镜像 | fix commit（V8 约 14.2，2025-09） |
|---|---|
| `cybergen_v8_cr_15a72141745c` | `15a72141745c7c66baf0a776ec88177424b0172e` |
| `cybergen_v8_cr_1b85c84da74a` | `1b85c84da74a325826f4e940d1affee9d94c8beb` |

**结论先行**：`SOURCE-IMAGES.md` 里"用 cybergen 造源镜像"仍然是正解（那条路走
`gclient sync`，天然拿到匹配的工具链）。本文记录的是**想在不联网的机器上自己造**时
撞到的那堵墙，以及需要联网机器补哪几样东西。

---

## 1. 现象

用 `expben-v8-evolve-pipeline --stage source` 在受限网络机器上构建
`cybergen_v8_cr_15a72141745c`：

| 阶段 | 结果 |
|---|---|
| `prepare_problem_build_inputs.py` | 通过 |
| `gn gen`（release） | **通过**（补了 5 处兼容后） |
| `ninja d8`（release） | **失败**，2532 个目标里做到 1032 个 `.o` 就全 16 路编译器一起卡死 |

失败时 45 秒内：`clang++` 进程数 16 不变、完成的 `.o` 从 1032 → 1032（零进展）、
错误行数从 508 → 644（还在涨）。也就是**所有编译器都撞在同一个头文件错误上**。

三个报错文件（同一个根因）：

```
../../v8/src/utils/memcopy.h:355:10: error: no member named 'atomic_ref' in namespace 'std'
  355 |     std::atomic_ref<T>(destination[i]).store(value, std::memory_order_relaxed);
../../v8/src/builtins/builtins-typed-array.cc   （同上，经 memcopy.h）
../../v8/src/wasm/value-type.h                  （同上量级）
```

编译命令行里能直接看到版本：

```
-DCR_CLANG_REVISION=\"llvmorg-18-init-17730-gf670112a-3\"
-DCR_LIBCXX_REVISION=9d119c1f4a097b7d27210874f4eba3fc91a83a4e
```

---

## 2. 根因：不是缺包，是工具链代数不对

`bootstrap.sh` 注入的那份 "shared fallback" checkout 是 **V8 12.3（2024-04）** 时代的；
题目源码是 **2025-09-29**（V8 约 14.2）。中间隔了一年半、两个大版本。

| 组件 | 本题 revision 的 DEPS 要求 | fallback 里实际有的 |
|---|---|---|
| clang | `llvmorg-22-init-8940-g4d4cb757`（DEPS:316 起的预编译包名） | `18.0.0git` @ `f670112a` |
| libc++ | `4b4a57f5cf627639c041368120af9d69ed40032c`（LLVM 22） | `_LIBCPP_VERSION 190000`，且 `__atomic/atomic_ref.h` **整个文件不存在** |
| libc++abi | `864f61dc9253d56586ada34c388278565ef513f6` | 同步老龄 |
| libunwind | `322be580a5a193a921c349a15747eeeb9a716ad1` | 同步老龄 |
| build/ | `dd54bc718b7c5363155660d12b7965ea9f87ada9` | V8 12.3 时代那一份 |

前四处是 `git archive <vulnerable>` 拿不到的（它们来自 gclient 的 DEPS 解析），
所以本地构建只能靠 fallback；而 fallback 的版本和源码 revision 对不上。

这也解释了为什么这套流程之前给 `00348ef5c3c1`、`1158ae719749` 用没问题——那两道题
本身就是老龄 revision，和 fallback 同代。

---

## 3. 已经在本地修好的 5 处（代码已落，不用重复劳动）

全部记在 `expben-v8-evolve-pipeline/scripts/prepare_problem_build_inputs.py` 里，
`VENDORED_THIRD_PARTY` / `COMPAT_BUILD_FILES` / `COMPAT_BUILD_FRAGMENTS` 三张表 +
`rjob_compile_d8.sh` 的兼容参数，都是按 DEPS pin 取得并逐条验证过 `gn gen` 通过的。

| # | 依赖 | 症状 | 来源（已 vendored） |
|---|---|---|---|
| 1 | `third_party/simdutf` | `ERROR at //BUILD.gn:5032: Unable to load third_party/simdutf/BUILD.gn` | chromium/chromium @ `a6fc5852b1a8` |
| 2 | `third_party/partition_alloc` | `ERROR at //BUILD.gn:5104: Unable to load .../partition_alloc/BUILD.gn` | chromium/chromium @ `a6fc5852b1a8` |
| 3 | `third_party/highway/src` | `ninja: error: third_party/highway/src/hwy/abort.cc missing and no known rule`（**gn gen 是过的**，只在 ninja 阶段暴露） | google/highway @ `84379d1c`（DEPS 自己的 pin） |
| 4 | `third_party/abseil-cpp` | `src/base/platform/mutex.cc:63: error: no member named 'lock' in 'absl::Mutex'`（**版本漂移**，不是缺目录） | chromium/src/third_party/abseil-cpp @ `c3655ab8`（该 sha 在 github.com/abseil/abseil-cpp 里存在） |
| 5 | build/ 兼容 | `avx512.gni` 缺失、`is_nacl_saigo` 未声明、`no_exit_time_destructors` config 未定义、`.gn` allowlist 改名、`use_libcxx_modules` 残留赋值 | 逐条打补丁 |

3 和 4 是同一类病的两种表现：**目录不在**（gn gen 就能发现）和**版本不对**
（gn gen 通过、编译期才炸，更贵）。

---

## 4. 本机解决不了的四样

| 要什么 | DEPS 里的源 | 为什么本机拿不到 |
|---|---|---|
| clang 工具链（`llvmorg-22-init-8940`） | `dep_type: gcs`，bucket `chromium-browser-clang`，对象 `Linux_x64/clang-llvmorg-22-init-8940-g4d4cb757-1.tar.xz`（**53 MB xz**，sha256 `35a8629534f534aa6771470945fc0baa6906b3fffb28433bc08674d343b84c90`） | 包体在 `commondatastorage.googleapis.com` / `chrome-infra-packages.appspot.com`，两个域名本机都不通 |
| `third_party/libc++/src` | `external/github.com/llvm/llvm-project/libcxx.git` @ `4b4a57f5` | 该 sha 在 github.com/llvm/llvm-project 里**不存在**（`api.github.com` 返回 422，`raw.githubusercontent` 取 `libcxx/include/__config` 返回 404）。chromium 的 llvm 镜像是它自己切的子树镜像，commit 是生成的，sha 和上游对不上 |
| `third_party/libc++abi/src` | `.../libcxxabi.git` @ `864f61dc` | 同上，422 |
| `third_party/libunwind/src` | `.../libunwind.git` @ `322be580` | 同上，422 |

对比一下就很清楚——凡是**真·GitHub 仓库的镜像**，同一个 sha 直接拿得到；是
chromium 自己切的子树镜像，就拿不到：

| 依赖 | 镜像类型 | 本机能否取到 |
|---|---|---|
| abseil-cpp / highway | GitHub 仓库镜像，sha 一致 | ✅ 已 vendored |
| simdutf / partition_alloc | chromium 自持仓库，但有 `chromium/chromium` 主仓可替代 | ✅ 已 vendored |
| libc++ / libc++abi / libunwind | llvm 子树镜像，sha 只存在于 chromium 侧 | ❌ |
| clang 工具链 | GCS 预编译包 | ❌ |

---

## 5. 需要联网机器做什么（二选一）

### 方案 A（推荐）：给出这一 revision 的 deps 包，本地继续构建

在一台能连 `chromium.googlesource.com` 的机器上：

```bash
export PATH=/path/to/depot_tools:$PATH
mkdir -p /srv/v8deps && cd /srv/v8deps

gclient config --name v8 https://chromium.googlesource.com/v8/v8.git --unmanaged
gclient sync -D --nohooks --revision v8@15a72141745c7c66baf0a776ec88177424b0172e

tar -cf /tmp/v8-deps-15a72141745c.tar -C /srv/v8deps/v8 \
    build buildtools third_party
```

`-D --nohooks` 是 `bootstrap.sh` 里同样的用法。`third_party/llvm-build/Release+Asserts`
是 `dep_type: gcs` 的 deps 条目（不是 hook），所以 `--nohooks` 不影响它，照样会下。

- **最小集**（约 1 GB，能解决当前已知的全部问题）：
  `build`、`buildtools`、
  `third_party/llvm-build/Release+Asserts`、
  `third_party/libc++/src`、`third_party/libc++abi/src`、`third_party/libunwind/src`、
  `third_party/abseil-cpp`、`third_party/highway/src`、
  `third_party/simdutf`、`third_party/partition_alloc`
- **保险做法**（约 3–5 GB）：整份 `third_party`。理由见下。

拿过来之后本地要做的事：把这份 `build`/`buildtools`/`third_party` 替换掉
fallback 里的对应目录，重跑 `prepare_problem_build_inputs.py --overwrite`，
先过 `gn gen` + 缺失输入扫描，再编 `d8`。

**顺带好处**：`build/` 和 `buildtools/` 也跟着对齐的话，上面第 5 项那一堆兼容补丁
（avx512.gni、is_nacl_saigo、no_exit_time_destructors、.gn allowlist、use_libcxx_modules）
**全部可以删掉**——它们存在的唯一原因就是"拿 2024 的 build/ 去编 2025 的源码"。
`build/` 只有几万行 GN，`buildtools` 也就百 MB，成本很低，收益是把整类兼容补丁清零。

### 方案 B：直接在联网机器上按 `SOURCE-IMAGES.md` 用 cybergen 造源镜像

那条路本来就是为这个场景写的，`gclient sync` 拿到的工具链天然匹配，
不用管本文列的任何一条。代价是要在那台机器上跑完整构建（每题 3 config × 2 side）。

---

## 6. 拿到东西之后怎么验证（本地）

```bash
# 1. 重新生成 build-inputs
python3 expben-v8-evolve-pipeline/scripts/prepare_problem_build_inputs.py \
  --problem-id cybergen_v8_cr_15a72141745c \
  --fix-commit 15a72141745c7c66baf0a776ec88177424b0172e \
  --pkg-config-path /data/temporal/pkg-config-wrapper.sh \
  --no-overlay-image --overwrite --output-root /data/temporal

# 2. gn gen + 缺失输入全量扫描（秒级，能一次列出所有缺的源文件）
#    见 /data/temporal/smoke/refresh.sh，它会直接执行 rjob_compile_d8.sh 里的兼容段，
#    然后扫 out/ 下所有 ninja 输入、把所有不存在的路径一次性打印出来
/data/temporal/smoke/refresh.sh cybergen_v8_cr_15a72141745c release

# 3. 编 d8（先单 config 验证，再上 docker）
ninja -C /data/temporal/smoke/out/x64.release d8 -j16
```

第 2 步那个扫描是这轮加的，专门治"3 和 4 那类只在编译期暴露的缺依赖"——
`gn gen` 只看 BUILD.gn 的 label，DEPS 解析出来的源文件它根本不检查。

---

## 附：本地现状（供参考，别重复踩）

- fallback checkout：`/data/temporal/v8`（V8 12.3）
- 构建输入：`/data/temporal/expben_v8_build/problems/<problem>/build-inputs/`
- 冒烟测试：`/data/temporal/smoke/`（`refresh.sh` 会重建 scratch 树、跑
  `rjob_compile_d8.sh` 的兼容段、`gn gen`、再扫缺失输入）
- vendored 缓存：`/data/temporal/{simdutf-amalgam,partition-alloc,highway-84379d1c,abseil-cpp-c3655ab8}`
- 已验证可用的 GitHub 通道：`gh-proxy.com`（clone）、`api.github.com`、
  `raw.githubusercontent.com`（小文件）；`github.com:443` 直连、`chromium.googlesource.com`、
  `commondatastorage.googleapis.com`、`chrome-infra-packages.appspot.com` 均不通。
