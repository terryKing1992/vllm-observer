# 验证记录

2026-09-15，Windows / Python 3.13。此次修正原先偏离需求的 pull 方案，改为实例直接主动 Remote Write。没有改动相邻 vLLM/Ascend 源码。

## 最新：Prometheus 本地 Docker 独立部署教程

新增 [Prometheus本地Docker教程](prometheus-docker.md)，包括手工docker run、19090端口、Remote Write接收开关、命名卷与显式存储路径、宿主机/容器上报地址、无需模型的验收、在线数量查询及排障。README和相关部署文档已链接。

- 教程10个PowerShell命令块通过语法解析，Git diff空白检查通过。
- 使用已有prom/prometheus:v3.2.1镜像在随机端口启动临时容器：promtool配置有效，接收开关为true，存储目录为/prometheus。
- verify_remote_write.py真实写入与心跳过期/离线检查通过，全部Grafana看板PromQL和教程3条PromQL均可执行。
- 临时容器已自动删除，既有服务未修改；命名卷跨容器恢复未在此次检查中实测。此次只调整教程，没有重新执行之前53项代码回归。

## 此前：PYTHONPATH 注入与 vllm serve 直接启动

按用户要求移除deploy/start_vllm.py，Dockerfile直接使用 `ENTRYPOINT ["vllm", "serve"]`。手工docker run显式设置 `PYTHONPATH=/opt/vllm-observer/bootstrap` 与 `OBSERVER_ENABLED=1`，由sitecustomize自动加载打点。参考Compose配置也同步加入PYTHONPATH；不再使用额外Python启动包装。

- 完整回归 **53 passed**；移除旧包装脚本的3项测试，增加启用/关闭两种情况下的子进程环境继承测试。父/子进程都不显式调用install，仍能按开关加载引擎与HTTP打点。
- Ruff lint/format、Git diff空白检查及手工指南9个PowerShell命令块语法检查通过。
- 自定义镜像原有PYTHONPATH需在部署时保留，Linux容器路径用冒号分隔；已有sitecustomize按启动指南合并，不再由包装脚本提前检查。
- 本次没有构建vLLM镜像或运行真实推理；验证的是启动加载机制与现有观察器回归。

## 此前：手工部署操作指南

按用户要求，默认操作入口改为 [手工部署指南](manual-deployment.md)，使用docker build/run逐步启动Prometheus、模型和Grafana，复用已有Langfuse。提供CPU/GPU参数及昇腾Linux直接安装命令，README同步采用手工操作和19090/13001端口。

本次9个PowerShell命令块均通过语法解析，Git diff空白检查通过。仅调整文档，没有重新执行之前已通过的54项代码测试。未启动新服务、构建模型镜像或进行真实推理验收。

## 此前：Docker 真实模型部署准备

2026-09-15：完成 [Docker 模型部署指南](docker-model.md)与 GPU/CPU Compose、observer 派生镜像 Dockerfile、保留 PYTHONPATH 的可选 sitecustomize 启动入口。模型验收脚本新增 `--server-url` 和 `--model`，支持请求真实服务并审计真实 token 与 decode 聚合。

- Docker Desktop Linux/x86_64，16 个逻辑 CPU，约15.2 GiB VM内存；官方 CUDA 12.6 临时容器成功识别 RTX 4060 Laptop、8188 MiB显存、驱动560.94，容器已自动删除。
- 固定 vLLM v0.29.0 GPU/CPU官方标签存在，GPU镜像CUDA基线13.0.3，需要更新当前驱动后运行。此检查不等于vLLM推理通过。
- 两套Compose配置解析通过，并断言宿主机模型18000、Prometheus19090、Grafana13001，内部Remote Write与宿主机Langfuse地址正确。没有修改既有服务。
- 完整回归 **54 passed**，Ruff lint/format通过，覆盖入口参数/环境保留、开关、sitecustomize冲突及真实token审计。
- 尚未构建vLLM派生镜像或下载模型；未声称真实CUDA/CPU/NPU推理与火焰图已完成验收。后续执行指南中的真实模型验证命令确认。

## 此前：Langfuse 节点详情中的 traceId

2026-09-15：修复每个节点的 Metadata 未显式展示 traceId 的问题。此前原生 Trace ID 已存在，但未映射到节点详情的 metadata 字段。

- 导出器为根节点、generation、queue、prefill、decode 统一设置 `langfuse.observation.metadata.traceId`，值取请求事件的 `trace_id`；自定义节点 metadata 不能覆盖它。
- 完整回归 **49 passed**；Ruff lint/format 通过。OTLP 编码测试逐个检查五个节点的原生 ID 与 metadata.traceId，并覆盖自定义值冲突和无上游父节点场景。
- 已向本地 Langfuse 发送模拟请求，通过官方 CLI 读回全部五个节点：[87011825ae204a8db8fa8359833c5bc4](http://localhost:3000/project/cmu1dr0rx0006o307nusja3lt/traces/87011825ae204a8db8fa8359833c5bc4)。脚本断言请求头、响应头、控制台汇总日志、节点原生 Trace ID、每个节点 metadata.traceId 全部一致，同时复核父子层级、时间范围、模型/token 和 decode 聚合。
- 查看方式：打开上述 trace，选择任意火焰图节点，在详情 Metadata 中查找 `traceId`。此记录通过真实服务 API 验证，未做浏览器截图验收。
- 服务更新后需要重启并发送新请求；之前生成的历史节点不会自动补充字段。密钥仅经进程环境注入，没有写入项目文件。

## 此前：sitecustomize 与火焰图层级

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
