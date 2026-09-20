# Docker 部署真实模型并接入 Observer

当前优先采用 [手工部署指南](manual-deployment.md)，逐步执行docker build/run。本文保留此前的Compose方案作为后续参考。

这套配置运行真实 vLLM 模型服务，使用独立镜像层安装 `vllm-observer`，由 `sitecustomize` 自动挂载 HTTP、引擎统计与时间校准。模型实例主动向 Prometheus Remote Write 上报，trace 在后台发送到已有 Langfuse。

## 本机检查结果与选型

2026-09-15 在本机检查：Docker Desktop 29.2.1，Linux/x86_64 容器，16 个逻辑 CPU，Docker VM 内存约 15.2 GiB；显卡为 RTX 4060 Laptop、显存 8188 MiB、驱动 560.94。已使用官方 `nvidia/cuda:12.6.3-base-ubuntu22.04` 临时容器执行 `nvidia-smi`，成功读到该显卡。该测试验证 GPU 透传，尚未验证模型推理。

默认使用 [Qwen2.5-0.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct)，用于以较少资源跑通真实请求与观测链路。模型首次启动会下载到 Docker 命名卷，后续复用缓存。

固定官方镜像：GPU 为 `vllm/vllm-openai:v0.29.0`，CPU 为 `vllm/vllm-openai-cpu:v0.29.0-x86_64`。两种标签已通过 Docker Hub 官方仓库查询确认，CPU 不需要 NVIDIA 驱动。GPU 镜像的 [CUDA 基线](https://github.com/vllm-project/vllm/blob/v0.29.0/docker/versions.json) 为 13.0.3；[CUDA 13 的驱动基线为 580 系列及以上](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html)。本机 560.94 尚不满足，应先通过 NVIDIA 官方渠道更新适用于 RTX 4060 Laptop 的 Windows 驱动并重启。不要把面向部分专业/数据中心 GPU 的 CUDA forward compatibility 当成这张 GeForce 的解决方案。

本机现有 Langfuse、new-api 和 Kubernetes 容器保持运行。现有 `9090` 是 MinIO，`3001` 是 new-api，因此本示例端口为：

| 服务 | 宿主机地址 | 容器内访问方式 |
|---|---|---|
| vLLM | http://localhost:18000 | 8000 |
| Prometheus | http://localhost:19090 | 模型向 `http://prometheus:9090/api/v1/write` POST |
| Grafana | http://localhost:13001 | 数据源为 `http://prometheus:9090` |
| 已有 Langfuse | http://localhost:3000 | `http://host.docker.internal:3000` |

容器中的 `localhost` 指容器自身。发往已有宿主机服务时使用 `host.docker.internal`；同一 Compose 内部服务使用服务名。不要把 MinIO 的 9090 当成 Prometheus 接收端。

## 准备配置

在 `vllm-observer` 工程根目录打开 PowerShell：

```powershell
# 首次执行；如已有 model.env，直接编辑它，保留已有设置。
if (-not (Test-Path deploy/model.env)) {
    Copy-Item deploy/model.env.example deploy/model.env
}

$env:GRAFANA_ADMIN_PASSWORD = Read-Host '设置本次 Grafana admin 密码'
$env:LANGFUSE_PUBLIC_KEY = Read-Host 'Langfuse Public Key'
$observerSecret = Read-Host 'Langfuse Secret Key' -AsSecureString
$env:LANGFUSE_SECRET_KEY = [System.Net.NetworkCredential]::new('', $observerSecret).Password
```

密钥通过启动终端的环境变量传给容器，不写入镜像或示例文件。`deploy/model.env` 已被 Git 忽略。`LANGFUSE_BASE_URL` 在该文件中使用容器可访问的地址；后面从宿主机执行验证脚本时单独改为 localhost。

首次镜像下载与构建需要访问 Docker Hub 和 Python 包源，模型下载需要访问 Hugging Face。GPU 基础镜像压缩层约 9.6 GB，CPU 约 1.84 GB，解压、构建层和模型缓存会占用更多空间。本机 C 盘检查时仅约 18 GiB 可用，应在 Docker Desktop 中确认磁盘映像所在盘及剩余容量；E/F 盘检查时空间较多。不要清理已有 Langfuse 数据来腾空间。

## 启动 GPU 模型

先完成驱动更新并确认 Docker Desktop 使用 WSL2 Linux 容器。官方说明见 [Docker Desktop GPU 支持](https://docs.docker.com/desktop/features/gpu/)。

```powershell
docker run --rm --gpus all nvidia/cuda:13.0.3-base-ubuntu22.04 nvidia-smi

docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile gpu config --quiet
docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile gpu up -d --build
docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile gpu logs -f model-gpu
```

看到服务启动成功后，在另一个终端检查：

```powershell
Invoke-RestMethod http://localhost:18000/health
Invoke-RestMethod http://localhost:18000/v1/models
```

初次下载超过 healthcheck 的等待窗口时，观察容器日志；健康状态本身不会自动终止模型进程。GPU 默认最多占用约一半显存预算，`max-model-len=2048`、`max-num-seqs=4`，并启用 `--enforce-eager`，减少首次验收的编译/图捕获成本。性能优化需后续单独评估。

## 暂不更新驱动时：CPU 模型

这同样运行真实 vLLM 推理，但不验证 CUDA/NPU 后端。只启动一种 profile，两个模型服务默认共用 18000 端口。

```powershell
docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile cpu config --quiet
docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile cpu up -d --build
docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile cpu logs -f model-cpu
```

默认绑定逻辑 CPU 0–3，KV cache 1 GiB，最大上下文 1024，并发序列 2。CPU 镜像有指令集要求，具体见 [vLLM CPU 官方说明](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/)。本机 Docker 内已有多个服务，启动前用 `docker stats --no-stream` 检查内存余量；CPU 路线尚待真实模型启动验收。

## 验证真实请求、日志、Langfuse 与主动上报

宿主机需安装本工程开发依赖及 Node.js/npx。在前面注入密钥的 PowerShell 中执行：

```powershell
.venv/Scripts/python scripts/smoke.py --base-url http://localhost:18000 --model qwen-observed

# CLI 在宿主机上查询 Langfuse，使用宿主机地址。
$env:LANGFUSE_BASE_URL = 'http://localhost:3000'
.venv/Scripts/python scripts/verify_langfuse.py --server-url http://localhost:18000 --model qwen-observed
```

第二条脚本真正请求模型，再轮询 Langfuse，要求出现 `serve-model-request → generate-response → queue/prefill/decode` 五个节点，检查每个节点的原生 Trace ID、metadata.traceId、父子 ID、时间包含关系、模型名、实际 token 数及 decode 聚合。它不使用 demo 的固定 token 数替代模型统计。成功输出 `hierarchy_verified` 和该条 trace 的访问链接；缺阶段会失败，不把只有 HTTP 根节点当成完整打点成功。

按脚本输出的 traceId 查容器日志（CPU 路线替换服务名）：

```powershell
$observerTraceId = '替换为脚本输出的32位trace_id'
docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile gpu logs --no-color model-gpu | Select-String -SimpleMatch $observerTraceId
```

应有一条请求汇总 JSON：`trace_id` 一致、`engine_requests > 0`、包含 queue/prefill/decode，decode 有 count/total_seconds/mean_seconds。Langfuse 各节点 Metadata 中也有相同 traceId。

打开 Prometheus http://localhost:19090，执行：

```promql
observer_instance_info
```

应看到 `service="local-qwen"`、`model="qwen-observed"` 和 `instance_id="local-gpu-01"`（或 CPU）。Grafana 用 admin 和刚设置的密码登录，打开 **vLLM Observer · 主动上报** 看板。模型容器向 Prometheus 的服务名地址主动 POST；Prometheus 的 `scrape_configs` 仍为空。

如果修改模型名，请同时修改 `MODEL_ID` 与 `SERVED_MODEL_NAME`，请求时使用 served name。新增副本时必须独立设置 instance ID 和宿主机端口；当前配置不是多 worker 聚合器，不直接用 `--scale` 复制固定 ID 的服务。

## 更新、关闭与迁移昇腾

修改 observer 代码后重新执行对应 profile 的 `up -d --build`。镜像直接执行 `vllm serve`，Compose通过PYTHONPATH传入bootstrap路径；自定义镜像已有路径时，用OBSERVER_CONTAINER_PYTHONPATH设置完整容器路径列表（冒号分隔）。打点总开关为 `OBSERVER_ENABLED`，改成 0 后重建容器即可关闭可选注入。完整机制与已有 sitecustomize 共存要求见 [可选启动指南](sitecustomize.md)，部署时需检查并合并已有sitecustomize。

停止本套服务，保留下载的模型缓存：

```powershell
docker compose --env-file deploy/model.env -f deploy/compose.model.yaml --profile gpu --profile cpu down
```

昇腾环境使用现场已经验证的 vLLM-Ascend/CANN 基础镜像构建同一 observer 层（通过 `VLLM_BASE_IMAGE` 指定），沿用现场容器的 NPU 设备映射、驱动目录、网络和 TP 参数。GPU profile 中的 `gpus: all` 是 NVIDIA 配置，不能直接用于 Ascend。不要把整棵本机工程挂载覆盖镜像里的 vllm 包：本 Dockerfile 只复制 observer包、项目元数据与bootstrap，两份上游源码都不进入构建上下文。

如果 Ascend 镜像依赖自带 entrypoint，请保留原入口的初始化，再在模型命令前设置 bootstrap 的 PYTHONPATH 和 `OBSERVER_ENABLED=1`；不要直接覆盖其初始化。线上通过 `OBSERVER_ENVIRONMENT=production` 标识环境，并使用线上可访问的 Langfuse/Prometheus 地址。真实 Ascend 设备与所选版本的阶段采集仍需现场执行同一验收脚本。

## 本次已验证的范围

- Docker GPU 透传成功：临时 CUDA 12.6 容器读到 RTX 4060 与 8 GB 显存。
- GPU/CPU 两套 Compose 均通过 `config --quiet`；使用新端口避开已存在的服务。
- sitecustomize启动开关、延迟导入和实际token聚合审计纳入回归测试；当前启动方式为vllm serve直接读取PYTHONPATH，没有额外Python入口包装。
- 尚未构建 vLLM 派生镜像、下载模型或运行真实推理；GPU 路线等待驱动更新，CPU 路线提供可执行配置。基础镜像标签存在不等于运行验收通过。

Docker 部署参考：[vLLM 官方容器指南](https://docs.vllm.ai/en/stable/deployment/docker/)。
