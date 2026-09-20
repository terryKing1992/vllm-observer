# Prometheus 本地 Docker 部署教程

适用环境：Windows PowerShell + Docker Desktop（Linux容器）。使用手工 `docker run` 部署，与本工程的vLLM实例主动上报方案配套，无需Compose、模型或显卡。

模型实例通过HTTP POST向Prometheus的Remote Write接收端发送指标。本教程不配置模型抓取任务，不使用Pushgateway。

## 1. 准备环境与配置

在 **vllm-observer 工程根目录**打开PowerShell：

```powershell
docker version
docker info --format '{{.OSType}}'
$observerRoot = (Get-Location).Path
$observerPromConfig = (Resolve-Path './deploy/prometheus.yaml').Path
Get-Content -Encoding UTF8 $observerPromConfig
```

Docker服务端应可访问，容器类型应为linux。本工程已有 `deploy/prometheus.yaml`：

```yaml
global:
  evaluation_interval: 10s
scrape_configs: []
```

`scrape_configs: []` 表示不主动抓取实例。`evaluation_interval` 用于规则计算，不控制模型上报周期；observer默认每10秒发送一次新快照。

教程固定使用工程已验证的 `prom/prometheus:v3.2.1`。下载镜像并校验配置：

```powershell
docker pull prom/prometheus:v3.2.1
if ($LASTEXITCODE -ne 0) { throw 'Prometheus image download failed' }
docker run --rm --entrypoint /bin/promtool `
    --mount "type=bind,source=$observerPromConfig,target=/etc/prometheus/prometheus.yml,readonly" `
    prom/prometheus:v3.2.1 check config /etc/prometheus/prometheus.yml
if ($LASTEXITCODE -ne 0) { throw 'Prometheus configuration is invalid' }
```

`promtool`应输出SUCCESS。使用命名卷保存指标是官方Docker部署指南推荐的方式，见 [Prometheus安装说明](https://prometheus.io/docs/prometheus/latest/installation/)。

## 2. 创建网络和数据卷

```powershell
docker network inspect observer-manual *> $null
if ($LASTEXITCODE -ne 0) {
    docker network create observer-manual
    if ($LASTEXITCODE -ne 0) { throw 'Docker network creation failed' }
}
docker volume create observer-prometheus-data
if ($LASTEXITCODE -ne 0) { throw 'Docker volume creation failed' }
docker ps -a --filter name=observer-prometheus
```

网络供模型、Prometheus和Grafana互访。数据卷挂载到容器 `/prometheus`，删除并重建容器时可继续复用历史指标。`docker volume create`对已有同名卷不会清空数据。

如果之前已按手工模型指南创建 `observer-prometheus`，配置一致时执行 `docker start observer-prometheus` 后跳到第4步，不重复创建。同名容器需要改参数时，按第7步重建并复用数据卷。

## 3. 启动 Prometheus

```powershell
docker run -d --name observer-prometheus `
    --network observer-manual --network-alias prometheus `
    --restart unless-stopped `
    -p 127.0.0.1:19090:9090 `
    --mount "type=bind,source=$observerPromConfig,target=/etc/prometheus/prometheus.yml,readonly" `
    --mount type=volume,source=observer-prometheus-data,target=/prometheus `
    prom/prometheus:v3.2.1 `
    --config.file=/etc/prometheus/prometheus.yml `
    --storage.tsdb.path=/prometheus `
    --storage.tsdb.retention.time=15d `
    --web.enable-remote-write-receiver
if ($LASTEXITCODE -ne 0) { throw 'Prometheus container creation failed' }
docker logs --tail 50 observer-prometheus
```

| 配置 | 用途 |
|---|---|
| `127.0.0.1:19090:9090` | 本机访问19090；容器服务仍监听9090 |
| `--network-alias prometheus` | 同一网络内可用prometheus作为主机名 |
| 配置文件readonly挂载 | 读取工程配置文件 |
| `--storage.tsdb.path=/prometheus` | 指标数据写入挂载的数据卷 |
| `--storage.tsdb.retention.time=15d` | 按时间保留约15天数据；不是磁盘容量上限 |
| `--web.enable-remote-write-receiver` | 启用 `/api/v1/write` 接收端 |
| `--restart unless-stopped` | Docker服务重启后恢复容器，手动停止的容器除外 |

本机9090已被Langfuse的MinIO占用，因此选择19090。模型上报给Prometheus，与Langfuse的3000端口是两条独立链路。Remote Write接收开关和接口见 [Prometheus存储说明](https://prometheus.io/docs/prometheus/latest/storage/)。

## 4. 检查服务和接收开关

等待日志显示服务准备就绪，打开 **http://localhost:19090** 。也可执行：

```powershell
Invoke-RestMethod 'http://localhost:19090/-/ready'
$observerFlags = Invoke-RestMethod 'http://localhost:19090/api/v1/status/flags'
$observerFlags.data.'web.enable-remote-write-receiver'
$observerFlags.data.'storage.tsdb.path'
$observerLoadedConfig = Invoke-RestMethod 'http://localhost:19090/api/v1/status/config'
$observerLoadedConfig.data.yaml
```

接收开关应为true，存储路径应为 `/prometheus`，加载配置中 `scrape_configs` 应为空。Targets列表没有模型实例是正常现象：本方案的数据由实例主动发送，不能用Targets或默认 `up` 指标判断是否收到上报。

不要用浏览器GET `/api/v1/write` 判断接收是否成功；该接口接收的是protobuf/Snappy格式的POST，不是JSON或Prometheus文本。用下一步执行真实写入验收。

## 5. 无需模型，先验证主动上报

使用工程已有Python环境；尚未安装依赖时执行：

```powershell
# 如果已有.venv，直接复用，不重复创建。
if (-not (Test-Path '.venv/Scripts/python.exe')) {
    python -m venv .venv
}
.venv/Scripts/python -m pip install -e .
.venv/Scripts/python scripts/verify_remote_write.py --base-url http://localhost:19090
```

脚本会向当前Prometheus写入一个唯一的 `validation-...` 测试实例，验证：POST入库、心跳过期排除、离线状态，以及工程Grafana看板的查询能否执行。成功输出以 `PASS: Remote Write ingestion` 开头。测试最后会把实例标为离线，历史时序保留至过期；无需vLLM、GPU或Langfuse。

查看已收到的身份指标：

```powershell
$observerQuery = [uri]::EscapeDataString('observer_instance_info')
$observerResult = Invoke-RestMethod "http://localhost:19090/api/v1/query?query=$observerQuery"
$observerResult.data.result | ConvertTo-Json -Depth 6
```

查询API返回的是实际时序数据，接口说明见 [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)。

## 6. 接入模型实例并查看在线数量

根据observer实际运行位置配置上报地址：

| Observer运行位置 | OBSERVER_REMOTE_WRITE_URL |
|---|---|
| Windows宿主机进程 | `http://localhost:19090/api/v1/write` |
| 同一observer-manual网络中的容器 | `http://prometheus:9090/api/v1/write` |

宿主机模拟服务示例：

```powershell
.venv/Scripts/python -m pip install -e '.[dev]'
$env:OBSERVER_SERVICE = 'demo'
$env:OBSERVER_MODEL = 'demo-model'
$env:OBSERVER_INSTANCE_ID = 'demo-1'
$env:OBSERVER_PUSH_ENABLED = '1'
$env:OBSERVER_LANGFUSE_ENABLED = '0'
$env:OBSERVER_REMOTE_WRITE_URL = 'http://localhost:19090/api/v1/write'
.venv/Scripts/python -m uvicorn vllm_observer.demo:app --port 8000
```

模拟服务仅用于验证上报。真实模型按 [手工模型部署指南](manual-deployment.md)启动：容器加入observer-manual网络，设置 `PYTHONPATH` 与 `OBSERVER_ENABLED=1`，直接执行 `vllm serve`。启动后即有心跳，无需先发推理请求。

在Prometheus查询页面执行以下PromQL：

```promql
# 所有仍可查询到的实例身份；包含离线实例的历史信息
observer_instance_info
```

```promql
# 当前在线实例数：alive=1，且心跳年龄在0至30秒之间
count(
  (observer_instance_alive == 1)
  and (time() - observer_heartbeat_timestamp_seconds < 30)
  and (time() - observer_heartbeat_timestamp_seconds >= 0)
) or vector(0)
```

```promql
# 按服务、模型汇总在线实例；没有在线实例的组不返回序列
count by (service, model) (
  (observer_instance_alive == 1)
  and (time() - observer_heartbeat_timestamp_seconds < 30)
  and (time() - observer_heartbeat_timestamp_seconds >= 0)
)
```

不能直接用 `count(observer_instance_info)` 作为在线数量，它可能包含刚离线实例的样本。每个副本需要唯一的instance ID，traceId不作为指标标签。更多健康和上报语义见 [架构与部署说明](deployment.md)。

Grafana手工启动见 [模型部署指南第6步](manual-deployment.md#6-手工启动-grafana)，其数据源使用同一Docker网络内的 `http://prometheus:9090`。

本教程宿主机端口只绑定127.0.0.1。远端昇腾服务器不能访问该回环端口；要接收远端上报，需另行将端口发布到可达的主机地址并配置访问控制，再使用该地址作为Remote Write目标。同一Docker网络的模型不受宿主机回环端口绑定影响。

## 7. 日常操作与数据保留

```powershell
docker logs --tail 100 observer-prometheus
docker stop --time 30 observer-prometheus
docker start observer-prometheus
docker volume inspect observer-prometheus-data
```

修改配置文件后先运行第1步promtool校验，再执行 `docker restart --time 30 observer-prometheus` 重新加载；配置文件被编辑器整体替换导致挂载仍指向旧文件时，重建容器并重新挂载。可以通过第4步的status/config接口确认实际加载内容。

修改端口、镜像版本或启动参数需要重建容器：

```powershell
docker stop --time 30 observer-prometheus
docker rm observer-prometheus
# 回到第3步，继续挂载同一个observer-prometheus-data卷。
```

以上操作保留命名卷中的指标。不要在需要保留历史时删除该卷。Prometheus接收端的数据持久化，与observer发送端是否能补发是两回事：当前observer没有持久化发送队列，断网期间瞬时数据不能保证补齐。

## 排障

| 现象 | 检查方法 |
|---|---|
| Docker API无法连接 | 确认Docker Desktop已启动并使用Linux容器 |
| 容器名称冲突 | 检查已有observer-prometheus，复用或按第7步重建 |
| 端口被占用 | `docker ps`核对映射；修改19090并同步宿主机上报URL |
| 页面能打开，但没有指标 | 运行verify_remote_write.py；仅部署Prometheus不会自动产生模型指标 |
| `/api/v1/write`返回404 | 检查接收开关和完整URL，确保目标不是MinIO |
| POST返回400 | 检查发送格式、实例ID是否重复、时间戳是否乱序及容器日志 |
| 模型容器无法连接 | 确认与接收端同属observer-manual网络，使用prometheus:9090 |
| Targets为空 | 主动上报模式预期如此，用observer指标查询验证 |
| 上报成功但在线数为0 | 检查alive、心跳是否超过30秒、模型机与Prometheus时钟是否同步 |
| 重建后历史数据消失 | 核对挂载的卷名与 `--storage.tsdb.path`；不要换成新卷 |

## 本次验证记录

2026-09-15：10个PowerShell命令块通过语法解析；使用已有prom/prometheus:v3.2.1镜像，在随机宿主机端口启动临时容器，完成promtool配置检查、接收开关与存储路径检查、Remote Write真实写入、心跳过期/离线判断、全部看板查询和本文3条PromQL查询。测试容器已停止并自动删除，没有更改既有服务。命名卷跨容器恢复未在此次临时容器验证中测试。
