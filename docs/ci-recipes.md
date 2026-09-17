# CI 验收配方：让发布门禁独立于 Stop hook

Stop hook 拦的是**写代码的 agent**；它拦不住人手滑，也拦不住没挂 hook 的
机器。发版前的最后一道门应该在 CI 里：提交/标签触发一次真机验证，板子
不点头，流水线就红。本文给出在 GitHub Actions 上跑 flashgate 的官方配方。

先说硬约束，不说漂亮话：**GitHub 托管_runner_上没有你的板子**。ST-Link、
USB 转串口、目标板都在你的桌子上，所以硬件验证 job 必须跑在挂了台架的
**self-hosted runner** 上。托管的 ubuntu/windows runner 只适合跑无硬件的
测试（本仓库的 `ci.yml` 四腿矩阵已经在做这件事：197+ 测试，串口/调试器
全 mock）。

## 配方一：push/PR 的真机门禁

`.github/workflows/hw-verify.yml`（放进你的固件仓库；flashgate 仓库自身
的 CI 不含此文件，因为 Actions runner 够不到示例板）：

```yaml
name: hw-verify
on:
  push:
    branches: [main]
  pull_request:

concurrency:
  group: bench          # one board: overlapping runs queue here (serialized)
  cancel-in-progress: false   # NEVER true here: cancelling mid-verify can
                         # abort during flashing and leave the board unknown

jobs:
  verify:
    runs-on: [self-hosted, bench]     # the runner physically wired to the board
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install git+https://github.com/Lion-1209/flashgate
      - name: Bench sanity
        run: flashgate --board boards/my-board.yaml doctor
      - name: Hardware gate
        shell: bash        # "verify --json > file" must write UTF-8/ASCII;
                           # Windows PowerShell 5.1's > writes UTF-16LE
        run: flashgate --board boards/my-board.yaml verify --all-probes --json > record.json
      - name: Upload evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: verify-record
          if-no-files-found: ignore   # a step BEFORE verify failed: nothing
          path: record.json           # to upload; the red step already says why
```

要点：

- **`concurrency.group: bench` 把重叠的 run 排队串行**（`cancel-in-progress`
  保持 `false`）：一块板同一时刻只被一个 job 验证。flashgate 自带的台架锁
  （`.flashgate/verify.lock`，见 GUIDE §8）本来就会把并发 verify 串行化并
  让后来者等待，concurrency 只是少烧排队分钟数。**不要**把
  `cancel-in-progress` 改成 `true`：取消可能落在烧录中途，把板子留在
  未知状态。
- **安全边界（先读这段再开 PR 触发）**：`pull_request` 触发 +
  self-hosted runner 意味着 **PR 里的代码（含 PR 分支版 workflow 本身）
  会在挂台架的机器上执行**——那台机器有 USB 设备、在内网。公开仓库接
  受外部 PR 时这是 GitHub 官方明确警告的风险。三条出路任选：仓库私有；
  组织设置里对外部贡献者强制 workflow 审批；或 PR 验证改
  `workflow_dispatch`/打标签手动触发。单人私有仓库随便用。
- **退出码就是门禁**：verify 非 0 时 step 失败、流水线红，无需任何额外
  判断。退出码语义见 README 的表（1 构建 / 2 烧录 / 3 板子沉默 / 4 启动
  错误串 / 5 身份不可信 / 6 环境 / 7 探针失败）。
- **`--json > record.json`**：stdout 是纯 ASCII JSON（人类日志走 stderr），
  重定向后落成 artifact，事后可审计"当时验证的是哪棵树、板子说了什么"。
  记录 schema 见 [record-schema.md](record-schema.md)；完整逐次记录也在
  板卡档案指向的固件目录 `.flashgate/records/` 下累积，需要的话把整个
  目录一并上传。
- **`if: always()`**：失败运行更要留证据（`if-no-files-found: ignore`
  覆盖"verify 之前的步骤就挂了、没有 record 可传"的情况）。

## 配方二：发版前的最后闸门（tag → 先验板，后发布）

在固件仓库现有的"tag push → 构建产物 → 发布"流水线前面插一个硬件 job。
形态与 flashgate 仓库自己的 `release.yml` 相同（tag → build →
PyPI），下面以它为模板，发布步骤换成你的固件产物上传；`needs` 是关键——
发布必须等板子点头：

```yaml
name: Publish to PyPI
on:
  push:
    tags: ["v*"]

jobs:
  hw-verify:
    runs-on: [self-hosted, bench]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install git+https://github.com/Lion-1209/flashgate
      - run: flashgate --board boards/my-board.yaml verify --all-probes --json > record.json
        shell: bash
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: verify-record
          if-no-files-found: ignore
          path: record.json

  publish:
    runs-on: ubuntu-latest
    needs: hw-verify          # the board signs off before PyPI does
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: |
          pip install build
          python -m build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

这是"发布门禁独立于 hook"的直接实现：写代码时是 Stop hook 拦 agent，
发版时是 CI 拦人——两条门走同一条 verify 链路、同一套退出码契约、同一
种证据记录（hook 另有指纹缓存短路和"同一棵坏树最多拦两次"的放行上限；
CI 每次全量跑，只会更严）。

## 配方三（变体）：远程台架，runner 不碰硬件

如果台架与仓库不在一处，可以让 runner 通过 `bench-serve`（Arm
device-connect 协议）驱动远程台架，仓库里的 runner 只做编排。但注意
两条红线，不满足就别用：

1. **D2D 模式零认证**：同网段任何人都能发现台架并 `start_verify`——
   这会**烧写板子**。仅适合可信局域网；共享网络必须走认证基础设施
   （device-connect server 模式）。
2. **只看 operation payload**：mesh 会把任何正常送达的应答包成
   `success: true`（包括 busy）；判定验证结果只能看 operation 的
   `state` / `exit_code` / record checks，绝不能看传输层 success。

配置细节（单飞行、单实例锁、`--stop` 排空停机、组播不通时切单播）见
README 的 Remote bench 一节。对 CI 而言，配方一的本地直连通常是更简单、
更少活动部件的选择；台架与仓库确不在一处时才值得付出配方三的复杂度。

## Runner 准备清单（一次性）

1. 在挂台架的机器上装 runner，打标签 `bench`，加入固件仓库。
2. 接线与工具链：ST-Link + USB 转串口（如 CH340），`flashgate doctor`
   全绿为准——它会逐项检查后端可执行文件、探针在线、串口可解析、
   cmake/ninja/arm-none-eabi-gcc 在 PATH（或 ST 工具包内）。
3. Windows 上建议先在**交互会话**里把 doctor 跑绿再装成服务——服务
   会话的设备可见性、PATH 继承与交互会话不同，出问题时先用 doctor
   定位，不要盲改 workflow。
4. 串口被别的程序占用（串口助手/VSCode serial monitor）会让 verify
   以退出码 6 失败——runner 机器上别开这些东西。
5. 固件仓库的 `.gitignore` 里保留 `.flashgate/`（示例工程已带），
   避免每次记录落盘都把 CI 检出的树弄脏。`record.json` 重定向落在
   检出根目录属未跟踪文件，`actions/checkout` 默认每次清理，不跨 run
   残留；`.flashgate/records/` 的历史同样只存活于单个 job 内，跨 run
   留痕靠 artifact。
6. **钉住 flashgate 版本**（可复现性）：`pip install git+…@v0.8.0`
   （tag）或 `pip install flashgate==0.8.0`（PyPI）。注意证据记录里
   `tool.version` 只到版本号粒度——同一版本号下 main 前移是审不出来
   的，钉 tag 才把工具侧也钉死。

## 这道门证明了什么（以及不证明什么）

一次 CI verify 证明的是：**这块板、这个台架、这次检出的树**，走完了
编译→烧录→板子自证→探针断言的完整链路。它不证明：别的板卡修订版、
别的工具链版本、任何物理效果超出探针断言范围的行为。要覆盖前者，靠
兼容矩阵和多板档案（每块板一个 runner 标签 + 一个 job，artifact 名带
板名区分，如 `verify-record-${{ matrix.board }}`）；后者永远需要
独立观测（仪器/夹具/人眼），flashgate 不假装替代——把"验证了什么、
没验证什么"写进每份记录的检查范围声明机制，是规划中的后续项。
