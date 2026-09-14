# 验证记录

2026-09-15，Windows / Python 3.13。此次修正原先偏离需求的 pull 方案，改为实例直接主动 Remote Write。没有改动相邻 vLLM/Ascend 源码。

## 已通过

- 19 项 pytest：原有请求日志、Langfuse、真实本机流式 HTTP、源码契约测试全部通过。
- 新增：实例向本机 HTTP 接收端主动 POST，检查 Remote Write headers、Snappy block/protobuf 数据、排序标签、时间戳、身份和无 traceId 标签。
- 接收端首次返回 503 后继续发送新快照，失败计数递增；正常关闭发送 alive=0/healthy=0。
- 生命周期在 startup.complete 后启动、shutdown 关闭；不依赖任何入站 scrape。
- 非法间隔拒绝启动；本地健康检查失败能上报 unhealthy。
- Ruff lint/format、Git diff --check 通过。
- Compose config 校验通过，scrape_configs 为空，receiver 开关已启用。

新增依赖 cramjam 2.12.1；protobuf 7.36.1。原测试依赖见 pyproject.toml 约束。

## 真实接收端验证

已执行 scripts/verify_remote_write.py，临时 Prometheus v3.2.1 开启 Remote Write 接收，scrape_configs 为空。验证通过：主动 POST 成功入库、当前样本携带旧心跳时在线数为0、正常离线、全部9个看板面板的 PromQL 可执行。测试容器已停止并自动清理；下载的镜像保留。

## 未验证范围

实际 NPU 推理及开销、Grafana 视觉效果、Kubernetes 部署与 CI 跨平台矩阵仍需对应环境验收。请求阶段是框架统计的 wall time，非 NPU kernel 时间。主动上报没有持久 WAL，不保证断网期间全部瞬时数据不丢失。

## 本地 Langfuse 实际入库验证

2026-09-15，使用用户已有 http://localhost:3000 服务，执行 scripts/verify_langfuse.py。密钥仅经子进程环境注入，没有写入项目文件。

- 模拟请求经过 ObserverMiddleware → FilterChain → AsyncExportFilter 后台线程 → LangfuseSink → 本地 OTLP endpoint。
- 导出失败数为0，随后从 /api/public/v2/observations 按 traceId 读回 GENERATION，确认实际入库。
- traceId：fe106eb852d84f86b38f2a60a5b03f6f；observationId：390454a54a569b27。
- 服务 observer-validation，实例 windows-langfuse-check，模型 demo-model。
- 读回 metadata 中 queue=0.01s、prefill=0.02s、decode count=3 / total=0.03s / mean=0.01s、HTTP total=0.0882192s，与控制台一致。
- 这是模拟请求的集成验证，不是昇腾模型性能结果。测试记录保留在 Langfuse，便于查看。

Grafana Compose 默认主机端口调整为3001，避免与已有 Langfuse 冲突。
