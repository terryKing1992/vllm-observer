# vllm-observer

vLLM / vLLM-Ascend 请求日志、阶段耗时、实例主动上报和异步 Langfuse 导出。

**模型实例直接向 Prometheus Remote Write 接口 POST 指标。Prometheus 不抓取实例，不需要实例地址清单，也不需要反向访问模型端口。**

## Windows 演示

先启动接收端（Docker Desktop 已启动）：

```powershell
$env:GRAFANA_ADMIN_PASSWORD='replace-with-your-password'
docker compose -f deploy/compose.yaml up -d
```

安装并启动模拟实例：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e '.[dev,langfuse]'
$env:OBSERVER_SERVICE='demo'
$env:OBSERVER_MODEL='demo-model'
$env:OBSERVER_INSTANCE_ID='demo-1'
$env:OBSERVER_REMOTE_WRITE_URL='http://localhost:9090/api/v1/write'
.venv/Scripts/python -m uvicorn vllm_observer.demo:app --port 8000
```

另一个终端用相同 service/model，改 instance ID 为 demo-2、端口为 8001，启动第二个副本。**无需修改 Prometheus 配置**。即使没有请求，实例也每 10 秒主动上报心跳。

Grafana：`http://localhost:3001`（避免与本地 Langfuse 的 3000 端口冲突），admin / 自设密码，打开 **vLLM Observer · 主动上报**。Prometheus：`http://localhost:9090`。正常退出发送 alive=0；强制结束或断网后，超过 30 秒无新心跳不再计为在线。在线表示近期上报，无法仅凭断流区分实例崩溃和网络中断。

发送模拟请求：

```powershell
.venv/Scripts/python scripts/smoke.py --model demo-model
```

响应返回 x-trace-id，控制台一条 JSON 包含 queue/prefill/decode/http_total，decode 含 count/total_seconds/mean_seconds。Demo 不运行模型，耗时是模拟值。

## 昇腾真实接入

在已有稳定 vLLM-Ascend Linux 环境中安装本工程：

```bash
python -m pip install -e '.[langfuse]'
export OBSERVER_SERVICE=chat
export OBSERVER_MODEL=qwen
export OBSERVER_INSTANCE_ID=replica-01
export OBSERVER_REMOTE_WRITE_URL=http://prometheus:9090/api/v1/write
export PYTHONPATH="/opt/vllm-observer/bootstrap${PYTHONPATH:+:$PYTHONPATH}"
export OBSERVER_ENABLED=1
# Langfuse 如需开启，在启动前通过 Secret 注入 LANGFUSE_* 环境变量：
export OBSERVER_LANGFUSE_ENABLED=1
vllm serve /path/to/model --served-model-name qwen
```

将 /opt/vllm-observer 换为实际部署目录。保留现有设备/TP参数，不使用 --disable-log-stats。**通过本工程 sitecustomize 可选加载，无需 --middleware、不注册 vLLM 插件、不修改 vLLM/Ascend 源码**。每副本一个 API 进程；同机多副本必须显式指定不同 instance ID，TP rank 不算副本。

未设置 OBSERVER_ENABLED=1 时启动 hook 不加载观察器。已启用的进程要关闭打点，需要以 OBSERVER_ENABLED=0 重启。具体支持范围与已有 sitecustomize 共存方式见 [可选启动指南](docs/sitecustomize.md)。

上报包含 observer 的请求计数、耗时、在途请求、心跳、健康和导出状态；不自动转发 vLLM 原生全部指标（如 KV 缓存）。

## Langfuse

启动前设置 OBSERVER_LANGFUSE_ENABLED=1、LANGFUSE_BASE_URL、LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY。按同一个 traceId 查看以下时间层级；decode 合并为一个有真实起止边界的区间，并保留 count/total/mean，不逐 token 展开。不收集 prompt/输出文本/请求体。

```text
serve-model-request (SPAN)
└─ generate-response (GENERATION，模型名、token用量)
   ├─ queue (SPAN)
   ├─ prefill (SPAN)
   └─ decode (SPAN，聚合重复生成阶段)
```

已有本地服务可设置 LANGFUSE_BASE_URL=http://localhost:3000。在进程环境中配置密钥后执行 `.venv/Scripts/python scripts/verify_langfuse.py`：发送模拟请求，再通过官方 langfuse-cli 读取并审计5个节点的类型、父子关系、时间包含关系、模型和token。需要 Node.js/npx；脚本不保存密钥。加 `--trace-id <ID>` 可只读审计已有demo trace。实际结果见验证记录。

## 文档与验证

- [架构与时间语义](docs/architecture.md)
- [主动上报部署、参数和排障](docs/deployment.md)
- [开发与过滤器扩展](docs/development.md)
- [验证记录](docs/validation.md)

```powershell
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check src tests scripts
.venv/Scripts/python -m ruff format --check src tests scripts
```
