# V8 源码 revision 与构建工具链相差一年半 —— 已在本机解决

**状态**：**已解决，不需要联网机器补产物。**
（文件名 `SOURCE-IMAGES-BLOCKED.md` 是历史遗留：本文最初是"卡住了、求补包"的记录。
结论保留在下面，新增第 2 节说明这些东西当时以为拿不到、实际都能拿到。）

**影响范围**：`cybergen_v8_cr_15a72141745c`、`cybergen_v8_cr_1b85c84da74a`。
两题**不是同一个时代**：前者 V8 ~14.2 / 2025-09 / DEPS pin LLVM 22，第 1 节的工具链代差
讲的是它；后者 V8 12.8 / 2024-06 / DEPS pin LLVM 19，与 fallback 的 V8 12.3（2024-02）
只隔五个月，真正卡住它的是**本机统一装的 LLVM 22 那套 libc++**（见第 7 节），
不是代差。

---

## 1. 原问题

用 `expben-v8-evolve-pipeline --stage source` 造源镜像时，`gn gen` 能过、`ninja d8` 全灭：

```
../../v8/src/utils/memcopy.h:355:10: error: no member named 'atomic_ref' in namespace 'std'
```

编译命令行里能直接看到版本代差：

```
-DCR_CLANG_REVISION=\"llvmorg-18-init-17730-gf670112a-3\"     ← fallback 的工具链
-DCR_LIBCXX_REVISION=9d119c1f4a097b7d27210874f4eba3fc91a83a4e
```

根因：`bootstrap.sh` 注入的 shared fallback checkout 是 **V8 12.3 / LLVM 18（2024-04）** 时代的，
题目源码是 **LLVM 22（2025-09）** 时代的。中间隔了一年半、四个 LLVM 大版本。
`std::atomic_ref` 是 C++20 里 LLVM 19 才落地的，所以每个翻译单元都死在这个头上，
16 个 clang 进程一起卡死，`-k 0` 跑下去只会空转。

---

## 2. 当时以为拿不到、实际都能拿到的四样

| 要什么 | DEPS 里的源 | 当时判断 | 实际 |
|---|---|---|---|
| clang 工具链 `llvmorg-22-init-8940` | `dep_type: gcs`，bucket `chromium-browser-clang` | 包体在 `commondatastorage.googleapis.com` / `chrome-infra-packages.appspot.com`，两个域名都不通 | **能拿到**：同一个 bucket 在 `storage.googleapis.com` 用 **path 式 URL** 可以访问（见下），sha256 与 DEPS 逐位吻合 |
| libc++ / libc++abi / libunwind | chromium.googlesource.com 的 llvm-project **子树镜像**，sha 只存在于 chromium 侧（`api.github.com` 返回 422） | 拿不到 | **能拿到**：工具链包名后缀 `g4d4cb757` 就是上游 revision，`github.com/llvm/llvm-project` 上有同样的源码 |
| llvm-libc `src/` | 同上，`external/github.com/llvm/llvm-project/libc.git` | 同上 | **能拿到**：同属 llvm-project，同一个 revision 的 `libc/` |
| chromium 的 `__config_site` | `buildtools/third_party/libc++/__config_site` | 未意识到需要 | **能拿到**：`chromium/chromium @ a6fc5852`（题目 revision 前一天）里就是 LLVM 22 时代的真件 |

### 关键那条：GCS 桶的双域名

`commondatastorage.googleapis.com` 被墙，但**同一个桶**挂在 `storage.googleapis.com` 上：

```bash
# 不通
curl -I https://commondatastorage.googleapis.com/chromium-browser-clang/Linux_x64/clang-llvmorg-22-init-8940-g4d4cb757-1.tar.xz

# 通（path 式）
curl -L -o clang.tar.xz \
  https://storage.googleapis.com/chromium-browser-clang/Linux_x64/clang-llvmorg-22-init-8940-g4d4cb757-1.tar.xz

sha256sum clang.tar.xz   # 35a8629534f534aa6771470945fc0baa6906b3fffb28433bc08674d343b84c90
```

同一条 DEPS 里另外两个 Linux 对象（覆盖工具、objdump）走同一个前缀：

- `Linux_x64/llvm-code-coverage-llvmorg-22-init-8940-g4d4cb757-1.tar.xz`
  sha256 `56bb0093e2e8f71e682f03b0e379d7dac0bacfcc83bfccfd42a4fcd1310fbe75`
- `Linux_x64/llvmobjdump-llvmorg-22-init-8940-g4d4cb757-1.tar.xz`
  sha256 `9236697d64fc9444b22c90a112f6b3a76ee1edf5b3891af67de0849deb274514`

### GitHub 侧

`raw.githubusercontent.com` 时通时断（同一批请求里 200 和 000 交替），
**`api.github.com` 的 contents 接口更稳**，直接把文件 base64 发回来：

```bash
curl -s -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/chromium/chromium/contents/buildtools/third_party/libc%2B%2B/__config_site?ref=a6fc5852b1a8" \
| python3 -c "import base64,json,sys; print(base64.b64decode(json.load(sys.stdin)['content']).decode())"
```

`chromium.googlesource.com`、`commondatastorage.googleapis.com`、
`chrome-infra-packages.appspot.com` 仍然不通。

---

## 3. 代码里改了什么

### `expben-v8-evolve-pipeline/scripts/prepare_problem_build_inputs.py`

原来只有 4 条 vendored 表项（simdutf / partition_alloc / highway / abseil），
现在扩到 9 条，并且新增了两套机制：

| 表 / 机制 | 作用 |
|---|---|
| `VENDORED_THIRD_PARTY` 新增 5 条 | `llvm-build/Release+Asserts`、`libc++/src`、`libc++abi/src`、`libunwind/src`、`llvm-libc/src` |
| 表项第 4 列 `replace` | 这 5 条是**整树替换**而不是叠加。上游删过的文件（libc++ 的 `src/tz.cpp`、`src/legacy_pointer_safety.cpp`）如果留在地上，会被新的同名头文件连带包含进去；纯拷贝做不到删除 |
| `COMPAT_BUILD_SUBSTITUTIONS`（新） | 单点文本替换，7 条。`gn gen` 就会炸的那些版本戳，见下 |
| `COMPAT_BUILD_FILES` 新增 2 条 | `buildtools/third_party/libc++/{__config_site,__assertion_handler}` |

`COMPAT_BUILD_SUBSTITUTIONS` 的 7 条，每条都是"不换工具链看不出来、换了不修就 `gn gen` 失败"的：

| 文件 | 改什么 | 不改会怎样 |
|---|---|---|
| `tools/clang/scripts/update.py` ×3 | `CLANG_REVISION` / `CLANG_SUB_REVISION` / `RELEASE_VERSION` → LLVM 22 | `--print-revision` 会把 `PACKAGE_VERSION` 与工具链的 `cr_build_revision` **逐字比对**，不一致直接返回 1，`gn gen` 立刻失败 |
| `build/toolchain/toolchain.gni` | `clang_version` `"18"` → `"22"` | `update.py` 拿它当 `--verify-version`，与自己的 `RELEASE_VERSION` 比对，不一致报 "clang_version in build/toolchain/toolchain.gni is likely outdated" |
| `buildtools/third_party/libc++/BUILD.gn` ×2 | `legacy_pointer_safety.cpp` → `expected.cpp`；`-std=c++20` → `-std=c++23` | 前者 ninja 找不到文件、后者 libc++ 自己编不过（`resize_and_overwrite`、`std::expected` 都是 C++23） |
| 同上 | libc++ target 补 `deps = [ "//third_party/llvm-libc:llvm-libc-shared" ]` | LLVM 22 的 `charconv` 从 libc++ 伸到 llvm-libc 的 shared 头库（`#include "shared/fp_bits.h"`）。必须是**赋值**不是追加：原来那行 `deps =` 只在 `export_libcxxabi_from_executables` 为假时才存在，而 asan 配置下它是真，追加写法会漏掉 5 个配置里的 2 个 |

### `expben-v8-evolve-pipeline/scripts/prepare_context.py`

`REQUIRED_SOURCE_DEPS` 里要求 `third_party/libc++/src/include/__algorithm/ranges_shift_left.h`。
**上游在 LLVM 22 删掉了这个文件**（P2440 落地时一并移除），所以这条断言对所有 LLVM 22 的题目
必然报 "source dependency bundle is incomplete"。已改为两代都有的
`__algorithm/shift_left.h`，校验意图不变（证明随工具链一起来的 libc++ 头是齐的）。

---

## 4. 本机缓存（换机要重做）

| 路径 | 内容 |
|---|---|
| `/data/temporal/llvm-build-llvmorg-22/` | clang 22 + 覆盖工具 + objdump，布局直接对应 `third_party/llvm-build/Release+Asserts` |
| `/data/temporal/llvm-project-4d4cb757/{libcxx,libcxxabi,libunwind,libc}` | sparse + blobless clone，revision `4d4cb757f94470b95458fcbe3b88332b212feeee` |
| `/data/temporal/chromium-compat-a6fc5852/buildtools/third_party/libc++/{__config_site,__assertion_handler}` | LLVM 22 时代的 chromium 真件 |

---

## 5. 怎么验证（本机，分钟级）

```bash
# 1. 重新生成 build-inputs 并 gn gen，顺带全量扫缺失输入
/data/temporal/smoke/refresh.sh cybergen_v8_cr_15a72141745c release
#    期望：Done. Made 578 targets ...  /  missing inputs: 0 distinct path group(s)

# 2. 编之前必炸的那几个文件
NINJA=/data/temporal/smoke/v8/third_party/ninja/ninja
OUT=/data/temporal/smoke/out/x64.release
"$NINJA" -C "$OUT" -j16 \
  obj/v8_base_without_compiler/memcopy.o \
  obj/v8_base_without_compiler/builtins-typed-array.o \
  obj/buildtools/third_party/libc++/libc++/{charconv,expected,path}.o
```

`refresh.sh` 第 2 步不会重写兼容逻辑——它直接从 `rjob_compile_d8.sh` 里把那段 heredoc
抠出来执行，免得冒烟测试过了而容器里挂了。

---

## 6. 遗留风险

- **`-std=c++23` 只作用于 libc++ 自己**，消费方仍旧 `-std=c++20`，和 Chromium 一致。
- `_LIBCPP_INSTRUMENTED_WITH_ASAN`：chromium 2025 是在 `build/config/c++/BUILD.gn` 里按
  `is_asan` 设的，我们这份 2024 的 `build/` 没有这个设置。undefined 在 `#if` 里读作 0，
  是安全侧（libc++ 容器不加 ASAN 友好标注），只影响优化不影响正确性。
- clang 工具链是**上游 revision** 的，`buildtools/third_party/libc++/BUILD.gn` 里那份
  写死的源文件清单仍是 2024 的。目前对齐（LLVM 22 只与它差一增一减，已补），
  但下次再换工具链代数时，这里仍然是最先出问题的地方——`gn gen` 能过、编译期才炸。

---

## 7. 老 revision 的题目（`cybergen_v8_cr_1b85c84da74a`，V8 12.8）

第 1～6 节讲的是 LLVM 22 时代的题目；12.8 这题（fix `1b85c84da74a` = 2024-06-26，
vulnerable `6a4b9641`，DEPS pin LLVM 19）**不是**代差问题：它的 `src/` 里
`atomic_ref` 出现 0 次，fallback 的 V8 12.3 `build/` 也够用（冒烟测试 gn gen 直接过）。
它卡在另一处——**vendored LLVM 22 的 libc++ 要求 `//third_party/llvm-libc`**：

```
ERROR at //buildtools/third_party/libc++/BUILD.gn:228:14:
Unable to load ".../third_party/llvm-libc/BUILD.gn".
    deps = [ "//third_party/llvm-libc:llvm-libc-shared" ]
```

- `third_party/llvm-libc` 是 **V8 2025 年才进自家仓库的 fork**（12.8 的 `third_party/`
  只有 11 个目录，没有它）。`VENDORED_THIRD_PARTY` 往 `third_party/llvm-libc/src`
  里灌的是 LLVM 源码，`BUILD.gn` 一直指望题目的 checkout 提供——2025 的题目有，2024 的没有。
- 修法：`prepare_problem_build_inputs.py::write_llvm_libc_build_gn()`，当
  `git cat-file -e <vulnerable>:third_party/llvm-libc/BUILD.gn` 不存在时，把
  `chromium-compat-a6fc5852/third_party/llvm-libc/BUILD.gn.2025`（Chromium 那份两行 group：
  `include_dirs = [ "src" ]` + `LIBC_NAMESPACE=__llvm_libc_cr`）落成 `BUILD.gn`。
  **仅在该 revision 没有时写**：2025 的题目自带 fork 的真件，`ADD source-deps.tar`
  会盖在镜像里的树上面，不能拿通用件去覆盖。
- 所以 `BUILD.gn.2025` 这个 fragment 从"手动补进 smoke 树"变成了生成器的一步：
  之前它只被手工用过（第 5 节那份 ninja 清单就是这么来的），没接进
  `prepare_problem_build_inputs.py`，2024 的题目上必然复现。

同批 10 题里凡 V8 < 2025 的，都会吃这一条。
