# 主动上报部署与排障

## 数据流

模型实例后台任务 → HTTP POST /api/v1/write → Prometheus → Grafana。采用 protobuf + Snappy Remote Write 1.0，无 Pushgateway、无实例抓取、无 file/Kubernetes SD。部署配置 scrape_configs 为空；Prometheus 需开启 --web.enable-remote-write-receiver。

每个实例启动即自动上报身份和心跳，不需要预先维护数量。无法统计从未成功上报过的实例；若需要“期望副本数”，应另接部署平台的期望状态。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| OBSERVER_SERVICE | vllm | 服务名 |
| OBSERVER_MODEL | unknown | 固定模型标签 |
| OBSERVER_INSTANCE_ID | hostname | 唯一副本 ID；同机多副本必须设置 |
| OBSERVER_PUSH_ENABLED | 1 | 主动上报开关 |
| OBSERVER_REMOTE_WRITE_URL | http://localhost:9090/api/v1/write | 接收端地址 |
| OBSERVER_PUSH_INTERVAL_SECONDS | 10 | 每次发送完成后等待秒数，正有限值 |
| OBSERVER_PUSH_TIMEOUT_SECONDS | 2 | 本地健康检查与 HTTP 发送超时，正有限值 |
| OBSERVER_REMOTE_WRITE_TOKEN | 无 | 可选 Bearer token，通过 Secret 注入 |
| OBSERVER_ENGINE_ENABLED | 1 | 0 关闭真实引擎阶段采集 |
| OBSERVER_LANGFUSE_ENABLED | 0 | 1 开启 Langfuse |
| LANGFUSE_BASE_URL | 无 | 开启时必填，含协议、不含 /api |
| LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY | 无 | 开启时必填 |
| GRAFANA_ADMIN_PASSWORD | 无 | Compose 必填 |
| GRAFANA_PORT | 3001 | Grafana 本机端口，避开 Langfuse 3000 |

Kubernetes 通过 Downward API 注入 metadata.uid 为 instance ID，配置可出站访问的 Remote Write URL。无需给监控系统开通模型入站端口。单 API 进程；共享端口的多 worker 仍需独立汇总方案。同一时序只能有一个活动写入者，避免重复 ID 造成乱序/覆盖。

## 健康与心跳

后台在应用 startup.complete 后启动，即使零请求也上报。每轮在本进程调用应用 /health，超时或非 2xx 上报 healthy=0，然后在后台线程向 Prometheus POST，推理请求不等待网络。

正常 shutdown 尽力发送 alive=0/healthy=0。kill、断网、receiver 不可用时无法可靠发送离线通知；看板要求 alive=1 且心跳年龄在 0–30 秒内。修改上报间隔时同步修改看板阈值，阈值应大于完整检查/发送周期并留网络余量。模型机和 Prometheus 需同步时间。

失败后记录 observer_push_failures_total，下一周期发送新快照，不缓存重放历史 heartbeat，避免恢复时把旧实例误判为在线。没有持久 WAL，期间瞬时 gauge 会缺失；累计 counter 若进程仍存活可在下次成功发送中恢复累计值，但跨进程重启会丢失未发数据。接收端失败期间，失败计数也暂时无法到达 Prometheus。旧数据按 Prometheus retention 保留，在线数不依赖旧序列是否被删除。

/observer/metrics 仅保留本地调试，不是生产传输路径。原生 vLLM /metrics 不自动转发。健康状态来自应用 /health，不代表每次生成必然成功。

## 现场验收

1. 开启 receiver，确认不配置任何实例抓取。
2. 启动两个副本，无请求时看板在线数变 2；只开放实例向 receiver 的出站连接。
3. 流式/非流式请求检查 traceId、engine_requests>0、queue/prefill/decode，单 token decode count=0。
4. 正常退出一个实例看在线数下降；强杀另一个，30 秒后应为 0。
5. 屏蔽上报网络，推理仍能完成；恢复后自动重新上报。
6. 模型健康检查失败时，心跳仍新鲜但 healthy=0。
7. 验证 Langfuse 入库，并实测观测开关对吞吐/P95/CPU 的影响。

## 排障

404：检查 receiver 开关和完整 /api/v1/write 路径。401/403：检查网关凭证。400：检查服务器日志、重复 instance ID 或机器时钟。无心跳：确认 ASGI lifespan 开启及 PUSH_ENABLED=1。无引擎阶段：确认 V1 AsyncLLM、stats 未关闭；取消/失败请求可能没有完成统计。

源码基线：vLLM b3124a8237f21fcd3a4510002a5e2925b0dbedfe，Ascend c04c5db026e5cb39ef843c72496384eb37789775。不是硬件兼容认证；升级须重验。Windows 是模拟/观察器环境，实际 NPU 推理在 Linux 验证。

协议参考：[Remote Write 1.0](https://prometheus.io/docs/specs/prw/remote_write_spec/)，[Prometheus 接收端开关](https://prometheus.io/docs/prometheus/latest/storage/)。
