# 验证记录

2026-09-15，Windows / Python 3.13。此次修正原先偏离需求的 pull 方案，改为实例直接主动 Remote Write。没有改动相邻 vLLM/Ascend 源码。

## 最新：sitecustomize 与火焰图层级

- 已安装并应用官方 Langfuse skill：`C:/Users/Lenovo/.codex/skills/langfuse/SKILL.md`，来源 https://github.com/langfuse/skills/tree/main/skills/langfuse。
- 依据当天读取的 instrumentation、best-practices、官方 vLLM/OTel 指导完成重构，保留 OpenTelemetry SDK 1.44.0 的显式历史时间戳，使用官方 langfuse-cli 审计。
- 最终完整测试 **49 passed**，包含10项真实 Python 子进程启动/延迟import/失败降级测试、引擎两种step校准、缺失/偏移时钟、并发隔离、取消与迟到统计、五节点OTLP编码；Ruff lint/format通过。
- Windows系统临时目录曾因ACL导致6个夹具创建失败；改用项目.venv内新建隔离临时目录重跑全部通过，不是跳过测试。
- 两个上游源码子模块工作区均干净；无 vllm.general_plugins 入口，无上游源码修改。真实NPU运行仍待现场验收。
- 最终trace：[21c5eb9e2c8f433cad062dc243f6b9dc](http://localhost:3000/project/cmu1dr0rx0006o307nusja3lt/traces/21c5eb9e2c8f433cad062dc243f6b9dc)。script从发送到CLI查询/断言完整成功。
- 已读回5个节点：serve-model-request(SPAN) → generate-response(GENERATION) → queue/prefill/decode(SPAN)。确认父子ID、子时间落在父区间、模型demo-model、input=8/output=4、development环境，以及decode count=3且mean=total/3。
- decode实测区间约47.476ms，均值约15.825ms；模拟sleep受Windows调度影响，因此不再将固定sleep设定值作为实测耗时。
- 原有中间测试trace 38690701260b41c7ae240c0ff4173fc5 也已通过CLI审计。验证记录保留供检查，没有记录密钥。
- 当前浏览器连接列表为空，未完成UI截图验收；父子层级与时间信息已经真实Langfuse API验证。

## 此前：主动推送验证

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

## 此前：Langfuse 单节点汇总格式

2026-09-15，使用用户已有 http://localhost:3000 服务，执行 scripts/verify_langfuse.py。密钥仅经子进程环境注入，没有写入项目文件。

- 模拟请求经过 ObserverMiddleware → FilterChain → AsyncExportFilter 后台线程 → LangfuseSink → 本地 OTLP endpoint。
- 导出失败数为0，随后从 /api/public/v2/observations 按 traceId 读回 GENERATION，确认实际入库。
- traceId：fe106eb852d84f86b38f2a60a5b03f6f；observationId：390454a54a569b27。
- 服务 observer-validation，实例 windows-langfuse-check，模型 demo-model。
- 读回 metadata 中 queue=0.01s、prefill=0.02s、decode count=3 / total=0.03s / mean=0.01s、HTTP total=0.0882192s，与控制台一致。
- 这是模拟请求的集成验证，不是昇腾模型性能结果。测试记录保留在 Langfuse，便于查看。

Grafana Compose 默认主机端口调整为3001，避免与已有 Langfuse 冲突。
