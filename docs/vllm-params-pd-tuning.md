# vLLM 参数全景与调优指南：PD 混部 vs PD 分离

> 基于本地 vLLM 源码检出（`vllm-observer/vllm`）整理，含源码位置引用。
>
> **关于参数数量**：`EngineArgs` 数据类共 **243 个字段**（`vllm/engine/arg_utils.py`，含 2 个内部下划线字段；另有 `host`/`port` 等 CLI-only 服务参数不在此列）。其中大量属于多模态(30)、LoRA(11)、观测(13)等场景功能项——**本文档精选对性能和部署架构有实际影响的 ~50 个核心参数**，按 16 类归组的全量清单见附录 **[vllm-params-full-inventory.md](vllm-params-full-inventory.md)**（自动从源码生成）。
> 查看全量 CLI 帮助：`vllm serve --help`。

---

## 目录

1. [参数分类全景](#一参数分类全景)
2. [调度器核心参数](#二调度器核心参数性能的发动机)
3. [内存与 KV Cache 参数](#三内存与-kv-cache-参数并发容量的地基)
4. [并行策略参数](#四并行策略参数决定资源切分方式)
5. [计算加速参数](#五计算加速参数免费性能的最后一档)
6. [PD 分离专用参数](#六pd-分离专用参数)
7. [准入控制与 QoS 参数](#七准入控制与-qos-参数过载保护)
8. [PD 混部 vs PD 分离：参数行为对比](#八pd-混部-vs-pd-分离参数行为对比)
9. [性能优化关键参数分级](#九性能优化关键参数分级)
10. [调参方法论](#十调参方法论先算账再调参)
11. [实战案例](#十一实战案例64g--tp4--qwen38-27b--200k--3p3d)

---

## 一、参数分类全景

| 类别 | 代表参数 | 一句话定位 |
|---|---|---|
| 模型与上下文 | `model` `max-model-len` `dtype` | 定义"服务什么" |
| **调度器** | `max-num-batched-tokens` `max-num-seqs` | **性能核心旋钮** |
| 内存/KV Cache | `gpu-memory-utilization` `kv-cache-dtype` | 并发容量的地基 |
| 并行策略 | `tensor-parallel-size` `data-parallel-size` | 资源切分方式 |
| 计算加速 | CUDA Graph / 编译 / 投机解码 | 不改语义的提速 |
| Prefix Caching | `enable-prefix-caching` | 复用历史计算 |
| PD 分离 | `kv-transfer-config` | KV 跨实例传输 |
| 准入控制 | `max-num-queued-tokens` | 过载熔断阀门 |
| 观测诊断 | `kv-cache-metrics` `enable-mfu-metrics` | 调参的眼睛 |

---

## 二、调度器核心参数（性能的发动机）

### 2.1 `--max-num-batched-tokens` ⭐⭐⭐

**单次 iteration（一次前向计算）最多处理的 token 总预算**（`config/scheduler.py:49`）。

每步预算的分配顺序：

```
token_budget = max_num_batched_tokens
  ├─ 1. 先满足所有 RUNNING 请求的 decode（每条 ≥1 token/步）
  └─ 2. 剩余预算 → prefill：短 prompt 整批塞入，长 prompt 按 chunked prefill 切块
```

- **调大** → prefill 吞吐↑、TTFT↓；但单步耗时↑、ITL 抖动↑、激活显存↑
- **约束**：≥ `max-num-seqs`；关闭 chunked prefill 时必须 ≥ `max-model-len`（否则启动报错）
- **默认值按硬件自适应**（`arg_utils.py` `get_batch_defaults()`）：

| 硬件 | 离线 LLM() | 在线 API Server |
|---|---|---|
| ≥160GB（B200/B300） | 16384 | 16384 |
| ≥70GB 非 A100（H100/H200） | 16384 | **8192** |
| 其他（含 64G） | 8192 | 2048 |
| CPU | 4096×world_size | 2048×world_size |

> 注：A100 因大预算有吞吐回退（PR #17885）被排除在 16384 默认之外。

### 2.2 `--max-num-seqs` ⭐⭐⭐

**单次迭代最多处理的序列条数**，即运行批（running batch）的上限，直接决定 decode 并发与 ITL。

- 混部/P 场景：限制单步并行 prefill 的请求数
- **D 场景（PD 分离）：这是第一旋钮**，`max_num_batched_tokens` 退化为它的从属参数
- 上限受 KV 池容量约束：看启动日志 `Maximum concurrency for X tokens per request: N`

### 2.3 `--max-num-scheduled-tokens`

调度器单步实际可发出的 token 数，通常等于 `max_num_batched_tokens`；**投机解码**时预留 draft slot 后会小于它（`scheduler.py:59`）。

### 2.4 `--long-prefill-token-threshold`

对长 prompt 的单次调度切块上限（默认 0 = 不启用）。想让 200K prompt 以更小的块交错调度、保护 decode ITL 时使用——比直接压低 `max_num_batched_tokens` 更精细（只限制长 prompt，不牺牲批量短 prompt 的打包宽度）。

### 2.5 `--enable-chunked-prefill`

V1 **默认开启**（模型支持时）。长 prompt 按 `max_num_batched_tokens` 预算切块，与 decode 混批。关闭时必须保证 `max-num-batched-tokens ≥ max-model-len`。

### 2.6 `--async-scheduling`

异步调度：CPU 调度与 GPU 计算重叠，消除 step 间空隙。小 batch/低延迟场景收益明显。

### 2.7 `--performance-mode`

一键性能倾向。`throughput` 模式下未显式指定的 `max_num_batched_tokens`/`max_num_seqs` 默认值自动翻倍（`arg_utils.py:2937`）。

### 2.8 `--scheduler-reserve-full-isl` / `--prefill-schedule-interval` / `--scheduling-policy`

分别为：调度时是否按完整输入长度预留 KV（防 prefill 中途 preemption）；prefill 调度间隔（DP 均衡相关）；`fcfs`（默认）或 `priority`（按请求优先级，值小先调度）。

---

## 三、内存与 KV Cache 参数（并发容量的地基）

### 3.1 `--gpu-memory-utilization` ⭐⭐⭐

GPU 显存使用比例（默认 0.9）。vLLM 启动时按它圈定总预算 → 扣除权重/激活/CUDA Graph → 剩余全部划给 KV 池。

> 独占显卡的生产部署建议 0.92~0.95；与其他进程共享显卡时必须留出余量。

### 3.2 `--kv-cache-memory-bytes`

直接指定 KV 池字节数（优先级高于按比例推导），支持人类可读写法（如 `40G`）。想要精确控制 KV 预算时使用。

### 3.3 `--kv-cache-dtype` ⭐⭐⭐

KV cache 精度：`auto`（跟随模型）/ `fp8`（容量减半，质量几乎无损）。**长上下文部署的第一杠杆**——直接等比放大并发容量。配套 `--kv-cache-dtype-skip-layers` 可对敏感层保持原精度。

### 3.4 `--block-size`

KV 块的 token 粒度（PagedAttention，平台自适应 16/32）。prefix 复用粒度与内部碎片率由它决定；一般不动。

### 3.5 `--cpu-offload-gb` / `--kv-offloading-size` / `--kv-offloading-backend`

权重 CPU 卸载量（省显存但拖慢每步）；KV 冷数据卸载到 CPU/远端（等效扩大 KV 池，LMCache 等后端），长上下文放不下时的兜底方案，代价是回读延迟。

### 3.6 `--num-gpu-blocks-override` / `--watermark`

手动覆盖 KV 块数（测试用）；水位线（默认 0.01，预留 1% KV 防调度饥饿）。

### 3.7 KV 容量速算公式

```
KV bytes/token = 2(K+V) × 层数 × KV头数 × head_dim × 精度字节数 ÷ TP数
并发容量 ≈ KV池字节 ÷ KV bytes/token
```

例：Qwen3.8-27B（64 层 / 4 KV 头 / head_dim 256）TP4，BF16 KV = **64 KiB/token/卡**。

---

## 四、并行策略参数（决定资源切分方式）

| 参数 | 作用 | 关键点 |
|---|---|---|
| `--tensor-parallel-size` | 张量并行，切权重和注意力头 | **KV 头数应能被 TP 整除**（4 头模型配 TP4 最优；TP8 会复制 KV 头浪费容量）|
| `--pipeline-parallel-size` | 流水线并行，按层切 | 跨机带宽差时用；增加流水线气泡 |
| `--data-parallel-size` | 数据并行复制实例 | 配 `--data-parallel-external-lb` 可让外部 LB 分流 |
| `--prefill/decode-context-parallel-size` | 上下文并行（PCP/DCP） | 超长上下文序列维度切分 |
| `--enable-expert-parallel` / `--moe-backend` | MoE 专家并行与内核 | MoE 模型必看 |
| `--distributed-executor-backend` | 分布式后端（mp/ray/external_launcher） | 多机部署选 ray |

> **算力 FLOPs 速算**：dense 模型 forward ≈ `2 × 参数量` FLOPs/token（27B → 54 GFLOPs/token）；MoE 用**激活参数量**。长上下文需另加 attention 二次项（200K 上下文平均再 +150 GFLOPs/token 级别）。

---

## 五、计算加速参数（"免费"性能的最后一档）

| 参数 | 作用 | 建议 |
|---|---|---|
| `--enforce-eager` | 关闭 CUDA Graph | **仅调试用**；开着约损失 10-30% 小 batch decode 性能 |
| `--cudagraph-capture-sizes` / `--max-cudagraph-capture-size` | Graph 捕获的 batch 档位 | 长上下文 OOM 时缩范围 |
| `--compilation-config` / `-O3` | torch.compile 图优化 | 新版本默认开；上线前 A/B 验证 |
| `--attention-backend` | 指定 attention 后端（FLASH_ATTN/FLASHINFER…） | 平台默认通常已最优，显存异常时切换 |
| `--speculative-config` ⭐ | 投机解码（MTP/EAGLE…，`num_speculative_tokens` 等） | **decode 提速 1.5~2×**；draft 占预算需放大 `max-num-batched-tokens`；长输出收益最大 |
| `--enable-flashinfer-autotune` | kernel 自动调优 | 部署后跑一轮 |
| `--enable-prefix-caching` ⭐⭐ | 前缀缓存（**V1 默认开**） | 多轮对话/RAG/共享 system prompt 场景 TTFT 收益巨大 |
| `--prefix-cache-retention-interval` | 前缀缓存保留时长 | 高 churn 场景调短 |
| `--stream-interval` | 流式输出合并粒度 | 见下方专节：跨机房/远端 LB 场景的关键调优项 |

### 5.1 `--stream-interval` 专节：流式输出合并粒度 ⭐（网络受限场景）

**语义**（`v1/engine/output_processor.py:299`，按 **token 数**计数而非调度步数）：

```
触发发送的条件（满足其一）：
  1. 请求结束（final 帧立即冲刷）
  2. 首 token（sent_tokens_offset == 0）→ TTFT 不受任何影响
  3. 自上次发送后新积累 token 数 ≥ stream_interval
```

- 默认 `1` = 每 token 一个 SSE chunk；请求级可通过 API 字段 `stream_interval` 覆盖，但引擎值是**下限**（生效值取 `max(引擎值, 请求值)`）
- 它**不减少任何计算**，纯粹是输出侧的合包；同时降低 detokenizer/API 进程 CPU

**为什么远端 LB 需要调大**：跨机房链路 RTT 高、BDP 大，每 token 一包（JSON 信封 + TLS record + TCP/IP 头 ≈ 200B 里的 ~100B 是开销）意味着：

- 千级并发流 × 30~60 包/秒 = 每秒数万个小包穿透 WAN，LB 解析/转发 CPU 和包处理开销线性爆炸
- 小包在高 RTT 有损链路上吞吐效率差，抖动被放大

**按 RTT 推荐取值**：

| LB ↔ 模型机房 RTT | 推荐 `stream-interval` | 依据 |
|---|---|---|
| < 5ms（同机房/同城） | `1`（默认） | 合包无收益 |
| 5~20ms（跨 AZ/园区） | `4~8` | 包量降 4-8×，肉眼无感 |
| 20~50ms（跨region） | `8~16` | 包量降一个量级，chunk 间隔 ~0.3s 仍流畅 |
| > 50ms（跨国/卫星） | `16~32` | 非交互场景（agent/长输出）可更大 |

**代价的量化**：客户端可见的更新粒度变粗，平均额外延迟 ≈ `(interval-1)/2 × 单token耗时`。以 50 tok/s（20ms/token）为例：interval=16 → 平均 +150ms、最坏 +320ms 的"打字机颗粒感"；但**首字立即出**（规则 2），交互体验的观感损失有限。

**别忘了传输层本身**（往往比 interval 更重要）：LB↔后端启用 HTTP/2 长连接与压缩、确认中间代理关闭缓冲（nginx 需 `proxy_buffering off` 或响应头 `X-Accel-Buffering: no`）、高 BDP 链路调大 TCP 缓冲区。

---

## 六、PD 分离专用参数

### 6.1 `--kv-transfer-config` ⭐⭐⭐

PD 分离的总开关（`config/kv_transfer.py`）：

```json
{
  "kv_connector": "NixlConnector",
  "kv_role": "kv_producer",
  "kv_connector_extra_config": {}
}
```

- **P 实例** = `kv_producer`：只做 prefill + 采样首 token，算完把 KV 发走
- **D 实例** = `kv_consumer`：加载 KV，继续 decode 到结束
- 传输层由第三方 connector 承担（NIXL/Mooncake/LMCache…），选型取决于网络（RDMA/UCX/共享内存）
- chunked prefill 的 chunk 同时是 KV 传输粒度——**计算与传输重叠**

### 6.2 `--kv-events-config`

对外发布 KV 事件流（供路由层做智能调度：知道哪些前缀在哪台实例上，路由到亲和节点提升 prefix 命中）。

### 6.3 路由层（vLLM 之外）

PD 分离必须配 router/proxy（`examples/disaggregated/` 有参考实现），职责：请求分发 P→D、失败转移、按 ISL:OSL 配比。

---

## 七、准入控制与 QoS 参数（过载保护）

### 7.1 `--max-num-queued-tokens` ⭐⭐

**prefill 积压 token 数上限**。当前处于 prefill 阶段的请求 prompt 总量达到上限时，新请求 **HTTP 503 拒收**（`async_llm.py` `check_admission()`，在流式响应开始前拦截）。

- 定值公式：`≈ target_TTFT × prefill吞吐`
- 本质是 **TTFT QoS 熔断阀**：把"全员 TTFT 恶化"变成"部分请求 503 去别处重试"
- 计数保守（chunked prefill 半成品按全量计、prefix 命中不扣除）→ 偏早拒，方向安全
- **PD 分离下设于 P 实例**，语义直接映射 P 池容量

### 7.2 `--max-num-queued-reqs`

未完成请求**条数**上限（waiting+running），同样 503。粗粒度容量阀；`max_num_queued_tokens` 才刻画真实 prefill 压力（100 条短请求 ≠ 10 条万级长请求）。

---

## 八、PD 混部 vs PD 分离：参数行为对比

### 8.1 架构本质差异

```
PD 混部（单实例）：
  client → [vLLM: prefill+decode 混批，同一张卡]
  参数目标：吞吐与延迟的折中

PD 分离（多实例）：
  client → router → [P×n: kv_producer] --KV传输--> [D×m: kv_consumer]
  参数目标：P 求吞吐，D 求延迟，各自独立调优
```

> **PD 分离本身不提升吞吐**（`docs/features/disagg_prefill.md` 明确说明）。价值 = TTFT/ITL 解耦调优 + 尾 ITL 可控 + 异构配比。

### 8.2 关键参数行为对照表

| 参数 | PD 混部 | P 实例（producer） | D 实例（consumer） |
|---|---|---|---|
| `max-num-batched-tokens` | ⭐ 折中值 8192 级别，平衡 TTFT 与 ITL | ⭐⭐ **大胆调大**（16K/32K）：纯 prefill 预算，无 ITL 顾虑；chunk 即 KV 传输粒度 | **退位**：设 2048 之类小值，仅需 ≥ `max-num-seqs`（+draft slot） |
| `max-num-seqs` | ⭐ 控制 decode 并发与混批宽度 | 次要：限制单步并行 prefill 请求数 | ⭐⭐ **第一旋钮**：决定 ITL 与全局并发 |
| `enable-chunked-prefill` | 默认开，块大小=预算 | 开：切块 + KV 分段传输重叠 | 无意义（无 prefill 计算） |
| `long-prefill-token-threshold` | 长 prompt ITL 保护 | 一般不用 | 不适用 |
| `kv-cache-dtype` | 容量杠杆 | 次要（P 上请求停留短） | ⭐ **决定全局并发容量** |
| `enable-prefix-caching` | TTFT 杠杆 | 有价值（路由层配合亲和） | 视 connector 而定 |
| `speculative-config` | decode 提速 | 不适用（P 无 decode） | ⭐ D 侧开 MTP，注意 draft slot 占预算 |
| `max-num-queued-tokens` | TTFT 熔断 | ⭐ 设在 P 上（= P 池容量） | 几乎无意义 |
| `max-num-queued-reqs` | 粗容量阀 | 设在 P 或 router | 一般不设 |
| `kv-transfer-config` | 不用 | ⭐ 必填 producer | ⭐ 必填 consumer |
| TP/DP 拓扑 | 单一权衡 | 可偏算力型卡 | 可偏显存/带宽型卡 |

### 8.3 混部 vs 分离的决策线

| 判断维度 | 选混部 | 选分离 |
|---|---|---|
| 流量画像 | 短 prompt、输出适中 | 超长输入（≥32K）或输出极长 |
| 延迟要求 | TTFT/ITL 可一起折中 | TTFT 与 ITL 有独立 SLO |
| 资源 | 单机少量卡 | 多机多卡可配比 |
| 运维复杂度 | 低（单进程） | 高（router/connector/传输网络） |
| 尾延迟敏感度 | 一般 | 高（分离是消除 prefill 干扰 decode 的最可靠手段） |

---

## 九、性能优化关键参数分级

### 第一梯队（不调对 = 浪费一半硬件）

| 参数 | 影响面 | 调法 |
|---|---|---|
| `gpu-memory-utilization` | KV 池 = 并发容量 | 独占卡 0.92~0.95 |
| `max-num-batched-tokens` | prefill 吞吐 / TTFT / ITL 抖动 | 按架构（见 8.2）与显存试 8K→16K |
| `max-num-seqs` | decode 并发 / ITL | 由 KV 容量与 ITL SLO 反推 |
| `kv-cache-dtype` | KV 容量 ×2 | 长上下文无脑 fp8（验证质量） |
| TP/DP 拓扑 | 一切的基础 | KV 头数整除 TP；先 TP 后 DP |
| `enable-prefix-caching` | TTFT | 保持默认开（V1） |

### 第二梯队（10%~2× 提升）

| 参数 | 条件 |
|---|---|
| `speculative-config`（MTP/EAGLE） | 有配套 draft 模型；输出越长收益越大 |
| CUDA Graph（别开 enforce-eager） | 小 batch decode 提速显著 |
| `-O3` 编译优化 | 新版本默认开，A/B 验证 |
| `performance-mode throughput` | 离线场景一键翻倍默认预算 |
| `async-scheduling` | 低延迟场景 |
| `long-prefill-token-threshold` | 长 prompt 与 decode 混跑的 ITL 保护 |

### 第三梯队（场景性收益）

`block-size`（碎片率）、`attention-backend`（平台差异）、`stream-interval`（流式开销）、`cpu-offload-gb`/`kv-offloading-size`（显存不够的续命手段）、`prefix-cache-retention-interval`（高 churn）。

### 不属于性能但必须设的

`max-num-queued-tokens` / `max-num-queued-reqs`（过载熔断）。

---

## 十、调参方法论：先算账，再调参

### Step 1 算显存账（决定可行域）

```
可用显存 = 总显存 × gpu-memory-utilization
KV池 = 可用 - 权重(TP分摊) - 激活(∝max_num_batched_tokens) - CUDA图/框架开销
并发容量 = KV池 ÷ (2×层数×KV头×head_dim×精度 ÷ TP)
```

### Step 2 看启动日志（事实校验）

- `Maximum concurrency for X tokens per request: N` → 真实并发容量
- KV blocks 数量、CUDA Graph 捕获档位
- 启动即 OOM → 降 `max-num-batched-tokens` 或 `gpu-memory-utilization`

### Step 3 基准扫参（数据决策）

```bash
# 用真实流量画像（ISL/OSL/QPS）回放
vllm bench serve --dataset-name random \
  --random-input-len <ISL> --random-output-len <OSL> \
  --num-prompts 200 --request-rate <QPS> ...

# 混部：扫 max-num-batched-tokens × max-num-seqs
# PD分离：P侧扫 batched-tokens（看TTFT），D侧扫 num-seqs（看ITL）—— 正交独立扫
```

### Step 4 定 SLO 与背压

`max-num-queued-tokens ≈ TTFT_SLO × prefill吞吐`，超额 503 由 LB 转移，避免静默劣化。

### 调参优先级口诀

> **显存账 → 并发容量 → chunk 宽度 → 加速器（投机/图/编译）→ 背压**
> （前两步决定"能跑多少"，第三步决定"跑多快"，第四步是白捡的，第五步保命）

---

## 十一、实战案例：64G × TP4 × Qwen3.8-27B × 200K × 3P3D

模型规格（HF config）：dense、64 层、4 KV 头、head_dim 256、原生 256K、自带 MTP 层。

### 显存账（每实例 TP4，单卡可用 ≈ 58.9GB）

| 方案 | 权重/卡 | 激活(16K预算) | KV池/卡 | 实例KV容量 | 200K并发 |
|---|---|---|---|---|---|
| BF16权重 + BF16 KV | 13.5GB | ~5GB | ~40GB | ~65万 tok | 3.2 条 |
| BF16权重 + FP8 KV | 13.5GB | ~5GB | ~40GB | ~130万 tok | 6.5 条 |
| FP8权重 + FP8 KV | 6.8GB | ~5GB | ~47GB | ~154万 tok | 7.7 条 |

> TP4 的隐藏红利：KV 头数 4 ÷ TP4 = 每卡 1 头，零冗余切分。

### 推荐配置

```bash
# P 实例 ×3
vllm serve Qwen/Qwen3.8-27B \
  --tensor-parallel-size 4 \
  --max-model-len 204800 \
  --max-num-batched-tokens 16384 \
  --max-num-seqs 32 \
  --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.92 \
  --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_producer",...}'

# D 实例 ×3
vllm serve Qwen/Qwen3.8-27B \
  --tensor-parallel-size 4 \
  --max-model-len 204800 \
  --max-num-seqs 64 \
  --max-num-batched-tokens 2048 \
  --kv-cache-dtype fp8 \
  --gpu-memory-utilization 0.92 \
  --kv-transfer-config '{"kv_connector":"NixlConnector","kv_role":"kv_consumer",...}'
```

### 吞吐量级估算

- **算力账**：dense 27B ≈ `2×27B = 54 GFLOPs/token`（精确 GEMM 44；200K 上下文平均加 attention 二次项 ~157 → **~200 GFLOPs/token**）
- 短 prompt（≤32K）：P 实例 ≈ 15~20K token/s；200K 满上下文：≈ 4~6K token/s
- **一条 200K prompt 的算力下限 TTFT ≈ 30~50s**（单 P 实例）——物理规律，只能加 P 摊
- decode 是访存瓶颈，不用 FLOPs 估：`batch × (权重+KV bytes) ÷ 显存带宽`

### 配比校验

3P:3D 是否合理取决于 ISL:OSL。200K:1K 极长输入画像 P 侧压力是 D 的几十倍；32K:4K 常规画像大致平衡。上线用真实流量回放校验，必要时调整实例角色数。

---

## 附：常用观测开关

| 参数 | 作用 |
|---|---|
| `--kv-cache-metrics` | KV 池使用率（容量校验） |
| `--enable-mfu-metrics` | MFU（算力利用率，判断 headroom） |
| `--per-request-spec-decode-metrics` | 投机解码接受率 |
| `--enable-logging-iteration-details` | 逐步迭代日志 |
| `--otlp-traces-endpoint` | OTel 追踪接入 |
| `/metrics`（默认暴露） | Prometheus 采集 |

---

*文档生成：2026-10-12 · 基于 vllm-observer 源码检出 · 参数以 `vllm/engine/arg_utils.py` 为准*
