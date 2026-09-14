# 需求与架构

| 需求 | 实现 | 验收行为 |
|---|---|---|
| 多实例状态 | 实例主动 Remote Write、心跳超时、本地健康检查、Grafana | 两副本启停，在线/健康数变化 |
| 无业务源码修改的日志 | ASGI + V1 运行时包装 | 每个完成/异常/取消 HTTP 请求一条 JSON |
| 重复阶段汇总 | 每请求 Aggregate | decode 总时间除以首 token 后 token 数 |
| Windows 与昇腾 | 无设备依赖核心、模拟服务、Linux 适配 | 跨平台单测，独立 NPU 验收 |
| trace 与异步上报 | ContextVar、请求 ID 映射、有界队列、OTLP | ID 一致，断网不阻塞请求 |
| 扩展性 | Adapter / Filter / Sink 分离 | 新后端不修改采集适配器 |

```mermaid
flowchart LR
    H[HTTP] --> M[ASGI 与 trace 上下文]
    M --> V[vLLM AsyncLLM]
    V --> S[请求结束统计]
    S --> A[阶段聚合]
    M --> A
    A --> F[过滤器链]
    F --> P[内存指标]
    P --> R[后台主动 Remote Write]
    R --> T[Prometheus]
    F --> L[JSON 日志]
    F --> Q[有界队列]
    Q --> O[后台 Langfuse OTLP]
    T --> G[Grafana]
```

## 采集与时间语义

generate 包装将外部引擎 request ID 关联当前 HTTP trace。后台统计通过 external_req_id 查找，不假设后台任务继承 ContextVar。生成器结束和取消释放映射；同 HTTP 多序列汇总。安装在 API middleware 构造阶段，不在 TP worker 中启动服务。原方法先执行，观测处理错误被隔离；签名检查不能替代真实兼容性测试。

| 阶段 | 定义 |
|---|---|
| http_total | ASGI 进入至应用返回，含流式发送/客户端背压 |
| http_first_body | 首个非空 HTTP body 发出前，可能是 SSE 角色帧，不是模型 TTFT |
| queue | 引擎首次 QUEUED 至首次 SCHEDULED |
| prefill | 首次 SCHEDULED 至首次 NEW_TOKEN，含等待/抢占 |
| decode | 首次 NEW_TOKEN 至最后 NEW_TOKEN，count 为首 token 后的输出 token 数 |

decode mean 按 token 加权，不是 kernel 或调度迭代平均耗时。推测解码一步可产生多 token。单 token 输出 count=0、mean=0。并行序列阶段之和可能超过 HTTP 时间，不能相加当作完整时间轴。缺失阶段保持缺失。

引擎使用已计算的差值，HTTP 使用 perf_counter，墙钟只给 HTTP observation 定位。没有跨进程时钟拼接，因此 Langfuse 阶段为属性，不伪造 child span 起止时间。

## 实例主动上报

每副本后台主动 POST Remote Write 数据到 Prometheus；不使用抓取或 Pushgateway。service/model/instance_id 标识副本，traceId 不作为指标标签。初次成功上报即被识别，零请求也有心跳。

模型健康检查在本进程调用应用 /health，结果连同心跳一起上报；接收端无需访问模型端口。在线定义为 alive=1 且心跳时间在最近30秒。正常退出尽力上报离线，异常退出靠超时判定。历史序列保留不影响当前在线数。

发送任务独立于 Filter 请求链：MetricsFilter 只更新内存，RemoteWriter 定时发送快照。失败不阻塞推理，不排队累积旧心跳；下一周期重试新状态。当前无持久 WAL，断网期间瞬时数据可能丢失；不应宣称无损上报。单副本一个 API 进程，实例 ID 唯一，时间需同步。

## Filter 合约与导出

`process(event) -> event | None`：返回事件继续，None 停止，异常记录后继续。默认 Metrics → Console → AsyncExport。自定义中间件子类通过 filters 参数注入新链。过滤器应快速执行并返回新字典；IO 放后台 Sink。过滤器修改原事件后再异常会影响后续消费者，应避免这种实现。

队列默认 1024，满后丢弃当前 trace 并计数。OTLP 超时预算 2 秒，失败记录计数；不落盘、不保证投递。正常退出等待最多 5 秒，强制退出可丢数据。HTTP 路径无 Langfuse 网络 IO；stdout 日志同步写出，日志采集端需保持畅通。

通过 [Langfuse 官方 OTLP HTTP 入口](https://langfuse.com/integrations/native/opentelemetry) 导出，独立 provider 不修改应用全局配置。W3C traceparent 保留 trace ID/父 span ID；否则接受非全零的 32 位小写十六进制 x-trace-id，非法值生成新 ID。暂不传播 tracestate/baggage，默认全量采集，不依赖上游 sampled 标记。
