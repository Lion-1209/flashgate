# 基于 MCP 的统一 AI–硬件交互层方案

> 项目代号：FlashGate Hardware Gateway  
> 文档状态：方案草案（Phase 0 部分实现于 v0.4.2——探针 fail-closed、
> 签名版本拒绝、incomplete 语义已落地，banner 全字段校验与
> evidence.mode 严格校验仍在清单上；Phase 1 已实现于 v0.5.0；
> Phase 2 起待触发条件——第一个非 ST 支持需求）  
> 日期：2026-09-05  
> 适用对象：架构评审、产品规划、研发拆解、PoC 实施

## 1. 摘要

本方案建议将 FlashGate 从面向单一 STM32 开发板的硬件在环验证工具，演进为基于 MCP（Model Context Protocol）的通用 Hardware Gateway。

核心设计原则是：

- MCP 统一 AI 对硬件能力的发现、调用和结果消费；
- Hardware Gateway 统一权限、设备状态、任务生命周期和验证证据；
- 适配器负责屏蔽 ST-Link、J-Link、UART、CAN、USB、SSH 等实现差异；
- 设备固件不必直接实现 MCP，只需提供适合自身资源条件的通信或证据接口；
- 标准化“能力和结果”，而不是强迫所有硬件使用同一种物理通信协议。

目标架构如下：

```text
┌─────────────────────────────────────────────────────────────┐
│ Claude / Codex / IDE Agent / CI / 自研 Agent                │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ FlashGate Hardware Gateway                                  │
│                                                             │
│  MCP Tools │ Capability Registry │ Operation Engine          │
│  Policy    │ Resource Lease      │ Evidence Store            │
└───────────────┬───────────────────────────────┬───────────────┘
                │                               │
        ┌───────▼────────┐              ┌───────▼────────┐
        │ Flash/Debug    │              │ Transport      │
        │ ST-Link/J-Link │              │ UART/CAN/USB   │
        │ OpenOCD/DFU    │              │ SSH/BLE/TCP    │
        └───────┬────────┘              └───────┬────────┘
                └──────────────┬────────────────┘
                               ▼
                    MCU / Linux Board / 仪器设备
```

## 2. 背景与问题

FlashGate 当前已经实现了完整的 STM32 硬件验证闭环：

```text
build → flash → boot evidence → identity check → probes → exit code
```

现有设计的优势包括：

- 通过 MCP 向 Agent 暴露 `build`、`flash`、`verify`、`probe` 等能力；
- 使用 YAML 描述板卡、构建命令、烧录地址、串口和探针；
- 支持 UART banner 与 SWD RAM signature 两种启动证据；
- 使用 Git SHA 和 dirty 状态关联工作区源码与板上固件；
- 支持寄存器读回，避免仅依赖固件自述；
- 通过 Stop hook 阻止 Agent 在真机验证失败时宣称任务完成。

但现有实现仍偏向“STM32 专用验证工具”，距离通用 AI–硬件交互层存在以下差距：

1. MCP 工具主要返回纯文本，模型必须解析日志和退出码。
2. 烧录实现直接依赖 STM32CubeProgrammer 和 ST-Link。
3. 板卡档案缺少正式的 Schema 版本及能力声明。
4. 缺少设备租约、资源锁、操作审计和危险动作策略。
5. 缺少跨设备一致的任务状态、错误分类和证据模型。
6. 底层串口行命令是自定义字符串，难以形成跨硬件的稳定语义。
7. 某些路径可能将未执行的检查视为成功，例如缺少串口时跳过功能探针。

## 3. 建设目标

### 3.1 核心目标

- 任何支持 MCP 的 Agent 都能以一致方式发现和操作授权硬件。
- 同一套上层工具可接入不同 MCU、调试器、通信链路和实验台。
- 每次操作返回机器可判定、可审计、可追溯的结构化结果。
- 关键结论必须由硬件证据支持，不能仅以命令成功或模型判断作为依据。
- 多 Agent、多设备并发时，不发生串口、调试器或板卡资源冲突。
- 对烧录、擦除、运动控制、供电控制等动作实施明确的安全策略。

### 3.2 非目标

- 不要求资源受限 MCU 直接运行 MCP Server。
- 不定义新的 UART、CAN、SWD 或网络物理协议。
- 不试图为所有硬件抽象完全相同的业务功能。
- 不允许模型绕过 Gateway 直接获得无限制的寄存器或执行权限。
- 第一阶段不建设完整云端硬件农场调度系统。

## 4. 设计原则

### 4.1 分层标准化

协议分为三个层次：

| 层次 | 负责内容 | 建议标准 |
|---|---|---|
| AI 交互层 | 能力发现、调用、结构化返回 | MCP |
| 硬件语义层 | 设备能力、任务状态、证据、错误和策略 | FlashGate Contract |
| 设备接入层 | 烧录、调试、通信和采集 | ST-Link、J-Link、OpenOCD、UART、CAN 等 |

### 4.2 能力优先

模型不应猜测设备是否支持串口、SWD、寄存器读取或功能探针。所有操作必须建立在设备显式声明的 capability 上。

### 4.3 证据优先

“命令执行成功”不等于“硬件工作正常”。通过必须包含至少一种可验证证据：

- 启动 banner；
- RAM signature；
- 寄存器读回；
- GPIO/ADC/传感器采样；
- 外部仪器测量；
- 日志或故障转储；
- 固件自检结果与独立读回的组合。

### 4.4 失败关闭

显式要求的验证步骤若未执行，结果必须为 `failed` 或 `incomplete`，不能返回 `passed`。

### 4.5 安全默认值

- 只读能力默认开放；
- 改变设备状态的能力需要明确声明；
- 高风险能力需要策略授权或人工确认；
- 所有参数由服务端验证，不能依赖模型自律。

## 5. 总体架构

### 5.1 MCP 接入层

面向 Claude、Codex、IDE Agent、CI 和自研系统提供稳定工具集合，负责：

- 输入 Schema 校验；
- 输出 Schema 校验；
- 工具权限和风险标记；
- MCP 错误与领域错误转换；
- 人类可读摘要与结构化结果同时返回。

### 5.2 Capability Registry

维护设备、实验台和适配器的能力视图，例如：

```json
{
  "device_id": "bench-01/apollo-h743",
  "device_type": "mcu-board",
  "online": true,
  "capabilities": {
    "firmware.flash": ["stlink"],
    "device.reset": ["stlink"],
    "evidence.boot": ["uart", "swd-signature"],
    "probe.functional": ["uart"],
    "debug.memory.read": ["swd"],
    "power.control": []
  }
}
```

能力注册表由设备档案、适配器探测和当前连接状态共同生成。

### 5.3 Operation Engine

将 MCP 工具调用转换为可跟踪的操作状态机：

```text
created
  ↓
validating
  ↓
waiting_for_resource
  ↓
running
  ├── succeeded
  ├── failed
  ├── cancelled
  └── timed_out
```

每个操作必须具备：

- `operation_id`；
- 调用者和设备标识；
- 创建、开始、结束时间；
- 当前阶段；
- 超时和取消状态；
- 输入参数摘要；
- 结构化结果；
- 证据引用；
- 审计信息。

### 5.4 Resource Lease

硬件资源按以下粒度加锁：

- 设备；
- 调试器序列号；
- 串口；
- 电源通道；
- 外部仪器通道。

建议采用带 TTL 的租约：

```json
{
  "lease_id": "lease-01J...",
  "device_id": "bench-01/apollo-h743",
  "owner": "agent-session-123",
  "expires_at": "2026-09-05T12:05:00Z"
}
```

操作结束、取消或租约超时后必须释放资源。

### 5.5 Policy Engine

根据动作、设备、参数和环境判断是否允许执行。

建议风险等级：

| 等级 | 示例 | 默认策略 |
|---|---|---|
| R0 只读 | 查询状态、读取日志 | 自动允许 |
| R1 可恢复 | reset、普通 probe | 在租约内允许 |
| R2 修改性 | flash、配置写入 | 需要明确授权 |
| R3 高风险 | erase、熔丝、供电、运动控制 | 人工确认或专用策略 |

### 5.6 Adapter Layer

适配器只负责将统一内部接口转换为底层工具调用。

```python
class FlashAdapter:
    def discover(self) -> list[ProbeInfo]: ...
    def flash(self, artifact, target, options) -> AdapterResult: ...
    def reset(self, target) -> AdapterResult: ...

class TransportAdapter:
    def open(self, endpoint, options) -> Session: ...
    def send(self, session, payload) -> AdapterResult: ...
    def receive(self, session, timeout) -> AdapterResult: ...

class EvidenceAdapter:
    def clear(self, device, contract) -> AdapterResult: ...
    def collect(self, device, contract, timeout) -> EvidenceResult: ...
```

首批适配器建议：

- `StlinkCubeProgrammerAdapter`；
- `UartTransportAdapter`；
- `SwdSignatureEvidenceAdapter`；
- `UartBannerEvidenceAdapter`；
- `ConsoleProbeAdapter`。

后续可增加：

- J-Link；
- OpenOCD/CMSIS-DAP；
- DFU；
- ESPTool；
- CAN/Modbus；
- SSH/Linux service；
- 示波器、逻辑分析仪和电源设备。

## 6. MCP 工具设计

### 6.1 推荐工具集合

#### 设备与能力

```text
hardware.list_devices
hardware.describe_device
hardware.acquire
hardware.renew_lease
hardware.release
```

#### 固件生命周期

```text
firmware.build
firmware.flash
device.reset
```

#### 操作与验证

```text
device.invoke
device.observe
device.verify
```

#### 调试与证据

```text
evidence.get
operation.get
operation.cancel
debug.console_read
debug.console_send
debug.memory_read
```

其中 `debug.*` 应作为高级能力单独授权，不应成为模型的默认操作路径。

### 6.2 `device.verify`

建议输入：

```json
{
  "device_id": "bench-01/apollo-h743",
  "lease_id": "lease-01J...",
  "firmware_ref": {
    "workspace": "/repo/examples/apollo-h743",
    "artifact": "build/Debug/Apollo.bin"
  },
  "requirements": {
    "build": true,
    "flash": true,
    "boot_evidence": true,
    "identity_match": true,
    "probes": ["led-demo"]
  },
  "allow_skip": false,
  "timeout_ms": 120000
}
```

建议输出：

```json
{
  "schema_version": "1.0",
  "operation_id": "op-01J...",
  "status": "failed",
  "code": "PROBE_ASSERTION_FAILED",
  "device_id": "bench-01/apollo-h743",
  "stage": "functional_probe",
  "firmware": {
    "git_sha": "2c58bd3",
    "dirty": true,
    "build_id": "2026-09-05T10:00:00Z"
  },
  "checks": [
    {"name": "build", "status": "passed"},
    {"name": "flash", "status": "passed"},
    {"name": "boot", "status": "passed"},
    {"name": "identity", "status": "passed"},
    {
      "name": "probe:led-demo",
      "status": "failed",
      "code": "ASSERTION_FAILED"
    }
  ],
  "evidence": [
    {
      "evidence_id": "ev-01J...",
      "kind": "register_readback",
      "source": "TIM3.CCR1",
      "expected": "<= 1000",
      "actual": 1200
    }
  ],
  "timing": {
    "started_at": "2026-09-05T10:00:00Z",
    "duration_ms": 8342
  },
  "summary": "Firmware booted, but led-demo register assertion failed."
}
```

### 6.3 `device.invoke`

该工具用于调用设备声明的高层能力：

```json
{
  "device_id": "bench-01/apollo-h743",
  "lease_id": "lease-01J...",
  "capability": "led.set_mode",
  "arguments": {
    "channel": 0,
    "mode": "breath"
  }
}
```

Gateway 根据设备档案映射为实际串口命令：

```yaml
commands:
  led.set_mode:
    transport: console
    request: "led{channel} {mode}"
    response: "OK led{channel} state={state}"
```

该映射属于设备适配层，AI 不需要知道实际字符串格式。

## 7. 统一结果模型

所有工具建议采用一致的结果信封：

```json
{
  "schema_version": "1.0",
  "operation_id": "op-...",
  "status": "succeeded | failed | incomplete | cancelled | timed_out",
  "code": "STABLE_MACHINE_CODE",
  "device_id": "...",
  "stage": "...",
  "data": {},
  "evidence": [],
  "warnings": [],
  "summary": "Human-readable message"
}
```

### 7.1 状态语义

- `succeeded`：所有必需步骤执行并通过；
- `failed`：至少一个必需步骤执行失败；
- `incomplete`：必需步骤因能力、资源或配置不足而没有执行；
- `cancelled`：调用者或策略取消；
- `timed_out`：达到操作级超时。

### 7.2 稳定错误码

建议第一版错误分类：

```text
INVALID_ARGUMENT
DEVICE_NOT_FOUND
DEVICE_OFFLINE
CAPABILITY_UNAVAILABLE
RESOURCE_BUSY
LEASE_REQUIRED
LEASE_EXPIRED
POLICY_DENIED
CONFIRMATION_REQUIRED
BUILD_FAILED
ARTIFACT_NOT_FOUND
FLASH_FAILED
RESET_FAILED
BOOT_EVIDENCE_TIMEOUT
BOOT_ERROR
IDENTITY_MISMATCH
PROBE_FAILED
PROBE_ASSERTION_FAILED
TRANSPORT_ERROR
ADAPTER_ERROR
OPERATION_TIMEOUT
OPERATION_CANCELLED
INTERNAL_ERROR
```

现有 0–7 退出码可以继续作为 CLI 兼容层，但 MCP 不应只依赖退出码表达结果。

## 8. 设备档案设计

建议将现有板卡 YAML 升级为版本化设备档案：

```yaml
schema_version: "1.0"

device:
  id: apollo-h743
  type: mcu-board
  vendor: ALIENTEK
  mcu: STM32H743IIT6

firmware:
  workspace: ../examples/apollo-h743
  configure:
    command: cmake --preset Debug
  build:
    command: ninja -C build/Debug
    timeout_s: 300
  artifact:
    path: build/Debug/Apollo.bin
    format: bin

connections:
  debugger:
    adapter: stlink-cubeprogrammer
    selector:
      serial_number: ""
    connect: port=SWD
  console:
    adapter: uart
    selector:
      vid: 0x1A86
      pids: [0x7523, 0x5523]
    baudrate: 115200

capabilities:
  firmware.flash:
    connection: debugger
  device.reset:
    connection: debugger
  evidence.boot:
    providers: [uart-banner, swd-signature]
  probe.functional:
    connection: console
  debug.memory.read:
    connection: debugger
    policy: advanced-debug

evidence:
  uart-banner:
    contract_version: "1"
    pattern: "FLASHGATE-BOOT board={board} git={git} build={build} rtos={rtos}"
    required_fields: [board, git, build]
    timeout_s: 15
  swd-signature:
    contract_version: "1"
    address: "0x2001FF00"
    size: 64
    required_fields: [version, git, build, crc32]

probes:
  led-demo:
    requires: [probe.functional]
    steps:
      - invoke:
          capability: led.set_mode
          arguments: {channel: 0, mode: breath}
      - observe:
          capability: led.read_state
        assert:
          - path: state
            op: eq
            value: BREATH
          - path: pwm_ccr
            op: lte
            value: 1000

policy:
  flash:
    risk: R2
  debug.memory.read:
    risk: R1
```

配置加载阶段必须检查：

- `schema_version` 是否受支持；
- capability 引用的 connection 是否存在；
- evidence 必需字段是否完整；
- probe 的依赖能力是否存在；
- 地址、超时、波特率和参数范围是否合法；
- 未知枚举值是否应立即拒绝。

## 9. 固件侧契约

固件侧按设备能力选择实现，不要求全部具备。

### Level 1：启动证据

最低要求：提供可解析、带协议版本和固件身份的启动证据。

```text
FLASHGATE/1 BOOT board=apollo-h743 git=2c58bd3-dirty build=2026-09-05T10:00:00Z
```

### Level 2：调试口证据

在固定、非缓存、调试器可读的 RAM 区域发布版本化签名。解析器必须拒绝未知布局版本，不能只校验 magic 和 CRC。

### Level 3：高层能力命令

固件可以继续使用轻量 UART 行协议，但建议具备：

- 协议版本；
- 请求 ID；
- 明确的成功和错误类型；
- 能力发现；
- 参数校验；
- 幂等性信息；
- 可选的结构化负载。

资源受限设备可以使用紧凑文本或 CBOR，不要求直接处理 MCP JSON-RPC。

## 10. 安全方案

### 10.1 工具分类

- `hardware.describe_device`：只读；
- `device.observe`：只读或低风险；
- `device.invoke`：依 capability 决定；
- `firmware.flash`：修改性、非幂等；
- `debug.console_send`：开放世界、高风险；
- `debug.memory_write`、`firmware.erase`：默认不暴露。

### 10.2 参数边界

危险参数必须由服务端强制校验，例如：

```yaml
capabilities:
  motor.set_speed:
    arguments:
      rpm:
        type: integer
        minimum: 0
        maximum: 1200
    confirmation:
      required_above:
        rpm: 1000
```

### 10.3 审计

建议记录：

- 调用者；
- MCP 会话；
- 工具及参数摘要；
- 设备和租约；
- 策略判定；
- 执行结果；
- 证据哈希；
- 时间戳和耗时。

日志中不得泄露 API Key、设备凭据或完整敏感负载。

## 11. 与 BYOK 的关系

BYOK 属于 AI Host 或模型服务层，不属于 FlashGate 的核心职责。

```text
用户 API Key → Claude/Codex/自研 Agent Host
Agent Host   → MCP → FlashGate Hardware Gateway
```

FlashGate 不应要求模型 API Key，也不应代理模型调用。它只负责硬件能力、权限、执行和证据。

硬件侧更接近 BYOH（Bring Your Own Hardware）：用户通过设备档案和适配器接入自己的板卡或实验台。

## 12. FlashGate 改造计划

### Phase 0：修正语义一致性

目标：保证“通过”真正代表所有要求已经执行。

- 显式要求 probe 时，缺少串口必须返回 `CAPABILITY_UNAVAILABLE`；
- banner 必须包含配置声明的所有身份字段；
- SWD signature 拒绝未知版本；
- 严格校验 `evidence.mode`；
- 为 skip、fail、incomplete 建立不同语义；
- 增加相应单元测试。

### Phase 1：结构化 MCP 契约

目标：消除模型对日志文本的依赖。

- 定义统一 Result Envelope；
- MCP 工具返回 `structuredContent`；
- 为工具声明 input/output Schema；
- 添加稳定错误码；
- 保留文本 summary 和 CLI 退出码兼容层；
- 为读写及破坏性工具增加注解和策略元数据。

### Phase 2：适配器化

目标：解除核心流程与 STM32CubeProgrammer 的耦合。

- 提取 FlashAdapter、TransportAdapter、EvidenceAdapter；
- 将现有 ST-Link、UART、SWD 实现迁移为适配器；
- 设备档案通过 `adapter` 字段选择实现；
- 增加模拟适配器，支持无真机集成测试；
- 增加 J-Link 或 OpenOCD 作为第二种烧录实现，验证抽象合理性。

### Phase 3：多设备与资源治理

目标：支持多个 Agent 和多个实验台。

- 引入 `device_id`；
- 实现设备发现和 capability registry；
- 实现租约、资源锁和超时释放；
- 引入 operation 状态机、取消和查询；
- 证据与操作关联存储；
- 增加并发冲突测试。

### Phase 4：安全策略与远程实验台

目标：满足团队和远程环境使用需求。

- 策略引擎；
- 高风险动作确认；
- 审计日志；
- 远程 MCP transport 的认证和授权；
- 实验台健康检查和离线处理；
- 可选的队列与调度能力。

## 13. 推荐目录结构

```text
flashgate/
├── mcp/
│   ├── server.py
│   ├── schemas.py
│   └── tools/
├── core/
│   ├── capabilities.py
│   ├── operations.py
│   ├── results.py
│   ├── leases.py
│   ├── policy.py
│   └── evidence.py
├── profiles/
│   ├── loader.py
│   ├── schema.py
│   └── migrations.py
├── adapters/
│   ├── base.py
│   ├── flash/
│   ├── transport/
│   └── evidence/
├── cli/
├── hooks/
└── tests/
    ├── unit/
    ├── contract/
    ├── integration/
    └── hardware/
```

## 14. 测试策略

### 单元测试

- Schema 和设备档案校验；
- capability 解析；
- 错误码映射；
- 状态机转换；
- 参数边界；
- evidence 解析和版本拒绝。

### 契约测试

- 每个适配器必须通过统一 contract suite；
- MCP 输出必须符合 output Schema；
- 明确要求的步骤不能被静默跳过；
- CLI 与 MCP 对同一执行结果保持语义一致。

### 模拟集成测试

通过 Fake Adapter 模拟：

- 构建失败；
- 烧录超时；
- 旧固件身份；
- banner 缺字段；
- signature CRC 错误；
- probe 断言失败；
- 资源竞争；
- 租约过期；
- 操作取消。

### 真机测试

- 至少两类硬件；
- 至少两种烧录适配器；
- UART 与无 UART 两种实验台；
- Agent 完整修复闭环；
- 多次重复运行和断电恢复。

## 15. PoC 验收标准

第一阶段 PoC 建议满足：

1. Codex 或 Claude 能通过 MCP 发现一块 STM32 设备及其 capabilities；
2. Agent 能申请租约并执行完整 verify；
3. MCP 返回结构化结果和证据，不需要解析 CLI 日志；
4. 缺少串口但要求功能探针时返回 `CAPABILITY_UNAVAILABLE`；
5. 两个并发 Agent 不能同时烧录同一块板；
6. 未知 signature 版本被拒绝；
7. Flash 操作进入审计记录；
8. 现有 FlashGate CLI 和 Stop hook 保持兼容；
9. Fake Adapter 能在 CI 中覆盖完整成功和失败路径；
10. 新增第二种烧录适配器后，上层 MCP 工具无需改变。

## 16. 关键决策

| 决策 | 选择 | 原因 |
|---|---|---|
| AI 接入协议 | MCP | 跨模型、支持工具发现和 Schema |
| MCU 是否直接实现 MCP | 否 | 资源、复杂度和安全边界不合适 |
| 是否统一物理协议 | 否 | 复用成熟工具链，通过适配器接入 |
| 标准化对象 | 能力、操作、结果、证据、策略 | 对上层稳定且可扩展 |
| 结果格式 | 结构化 JSON + 文本摘要 | 同时服务机器和人 |
| 未执行检查 | incomplete/failed | 防止假阳性 |
| 并发方式 | 带 TTL 的独占租约 | 符合真实硬件资源特性 |
| BYOK 所属层 | Agent Host | FlashGate 不直接调用模型 |

## 17. 结论

基于 MCP 建设统一 AI–硬件交互层是合理方向，但 MCP 只应作为 Agent 与 Hardware Gateway 之间的标准接口。

FlashGate 的核心价值不应是重新定义一种硬件通信协议，而应是：

> 将不同硬件工具链包装成可发现、可授权、可执行、可验证、可审计的统一能力，并向 AI 提供可信的结构化硬件证据。

现有 FlashGate 已经验证了 build、flash、boot evidence、identity 和 probe 的核心闭环。下一阶段应优先完成语义修正与结构化 MCP 契约，然后再进行适配器化和多设备治理。这条演进路线既能保持当前工具的简单性，也能逐步形成真正通用的 AI Hardware Gateway。
