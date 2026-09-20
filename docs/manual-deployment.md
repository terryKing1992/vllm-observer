# 手工部署模型与 Observer

在 **vllm-observer 工程根目录**打开 PowerShell，依次执行下列命令。使用 `docker build` / `docker run`，无需 Compose 或 model.env。模型是 Qwen2.5-0.5B-Instruct，复用本机已有 Langfuse。模型直接执行 `vllm serve`，通过启动环境中的 `PYTHONPATH` 加载sitecustomize打点。

## 1. 选择 CPU 或 GPU

当前机器驱动560.94，Docker GPU透传已经通过。选定的vLLM v0.29.0 GPU镜像使用 [CUDA 13.0.3](https://github.com/vllm-project/vllm/blob/v0.29.0/docker/versions.json)，需要 [580系列及以上驱动](https://docs.nvidia.com/cuda/archive/13.0.0/cuda-toolkit-release-notes/index.html)。暂不更新驱动可以先用CPU验证真实模型与打点；真实模型尚未启动验收。

**CPU：**

```powershell
$observerBaseImage = 'vllm/vllm-openai-cpu:v0.29.0-x86_64'
$observerImage = 'vllm-observer:0.1.0-vllm0.29.0-cpu'
$observerInstance = 'manual-cpu-01'
$observerDeviceArgs = @(
    '--cpuset-cpus', '0-3', '--cap-add', 'SYS_NICE',
    '-e', 'VLLM_CPU_KVCACHE_SPACE=1', '-e', 'VLLM_CPU_OMP_THREADS_BIND=0-3'
)
$observerModelArgs = @(
    '--dtype', 'bfloat16', '--max-model-len', '1024',
    '--max-num-seqs', '2', '--max-num-batched-tokens', '1024', '--enforce-eager'
)
```

**GPU：更新驱动后，用这一组替代上面的变量。**

```powershell
$observerBaseImage = 'vllm/vllm-openai:v0.29.0'
$observerImage = 'vllm-observer:0.1.0-vllm0.29.0-cuda'
$observerInstance = 'manual-gpu-01'
$observerDeviceArgs = @('--gpus', 'all')
$observerModelArgs = @(
    '--dtype', 'half', '--max-model-len', '2048', '--max-num-seqs', '4',
    '--gpu-memory-utilization', '0.5', '--enforce-eager'
)
```

GPU基础镜像压缩层约9.6 GB、CPU约1.84 GB，解压和模型缓存需要额外空间；检查Docker Desktop磁盘映像所在盘的容量。`docker stats --no-stream` 可查看现有服务的内存占用。

## 2. 手工启动 Prometheus

独立安装说明、数据持久化、接收验证与排障见 [Prometheus本地Docker部署教程](prometheus-docker.md)。以下为快速启动命令。

首次创建网络与接收端：

```powershell
docker network inspect observer-manual *> $null
if ($LASTEXITCODE -ne 0) {
    docker network create observer-manual
    if ($LASTEXITCODE -ne 0) { throw 'Docker network creation failed' }
}
$observerRoot = (Get-Location).Path
docker run -d --name observer-prometheus --network observer-manual --network-alias prometheus `
    -p 127.0.0.1:19090:9090 `
    --mount "type=bind,source=$observerRoot/deploy/prometheus.yaml,target=/etc/prometheus/prometheus.yml,readonly" `
    --mount type=volume,source=observer-prometheus-data,target=/prometheus `
    prom/prometheus:v3.2.1 `
    --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus `
    --web.enable-remote-write-receiver
if ($LASTEXITCODE -ne 0) { throw 'Prometheus startup failed; inspect the Docker error' }
```

访问 http://localhost:19090 。配置的 `scrape_configs` 为空；模型容器向 `http://prometheus:9090/api/v1/write` 主动POST。同一自定义网络中可用容器名/网络别名互访，参见 [Docker网络说明](https://docs.docker.com/engine/network/drivers/bridge/)。本机9090已被MinIO占用，因此宿主机改用19090。

若同名容器已经存在，先用 `docker ps -a --filter name=observer-prometheus` 检查；已按本指南创建且配置未变的容器可执行 `docker start observer-prometheus`，无需再次创建。

## 3. 手工构建模型镜像

在同一个PowerShell中，使用第1步选好的变量：

```powershell
docker build -f deploy/Dockerfile --build-arg "VLLM_BASE_IMAGE=$observerBaseImage" -t $observerImage .
if ($LASTEXITCODE -ne 0) { throw 'Image build failed; resolve it before starting the model' }
```

Dockerfile在官方镜像中安装 `vllm-observer[langfuse]` 和bootstrap文件，不编译或修改vLLM源码。`.dockerignore`排除两份上游子模块、虚拟环境与密钥文件，不会把本机源码覆盖到镜像中的vLLM包。

## 4. 手工启动模型并启用打点

密钥使用已有Langfuse项目的配置。若当前终端已经设置，则不重复输入：

```powershell
if (-not $env:LANGFUSE_PUBLIC_KEY) {
    $env:LANGFUSE_PUBLIC_KEY = Read-Host 'Langfuse Public Key'
}
if (-not $env:LANGFUSE_SECRET_KEY) {
    $observerSecret = Read-Host 'Langfuse Secret Key' -AsSecureString
    $env:LANGFUSE_SECRET_KEY = [System.Net.NetworkCredential]::new('', $observerSecret).Password
}
$observerRunArgs = @(
    'run', '-d', '--name', 'observer-vllm',
    '--network', 'observer-manual', '--init', '--shm-size', '1g',
    '--stop-timeout', '30', '-p', '127.0.0.1:18000:8000',
    '--add-host', 'host.docker.internal:host-gateway',
    '--mount', 'type=volume,source=observer-hf-cache,target=/root/.cache/huggingface',
    '-e', 'PYTHONPATH=/opt/vllm-observer/bootstrap',
    '-e', 'OBSERVER_ENABLED=1', '-e', 'OBSERVER_SERVICE=local-qwen',
    '-e', 'OBSERVER_MODEL=qwen-observed', '-e', "OBSERVER_INSTANCE_ID=$observerInstance",
    '-e', 'OBSERVER_REMOTE_WRITE_URL=http://prometheus:9090/api/v1/write',
    '-e', 'OBSERVER_LANGFUSE_ENABLED=1', '-e', 'OBSERVER_ENVIRONMENT=development',
    '-e', 'LANGFUSE_BASE_URL=http://host.docker.internal:3000',
    '-e', 'LANGFUSE_PUBLIC_KEY', '-e', 'LANGFUSE_SECRET_KEY',
    '-e', 'VLLM_WORKER_MULTIPROC_METHOD=spawn',
    '-e', 'VLLM_NO_USAGE_STATS=1', '-e', 'HF_HUB_DISABLE_TELEMETRY=1'
)
docker @observerRunArgs @observerDeviceArgs $observerImage `
    Qwen/Qwen2.5-0.5B-Instruct --served-model-name qwen-observed `
    --host 0.0.0.0 --port 8000 @observerModelArgs
if ($LASTEXITCODE -ne 0) { throw 'Model container creation failed; inspect the Docker error' }
docker logs -f observer-vllm
```

`-e LANGFUSE_SECRET_KEY`从终端读取环境变量，不将密钥写入示例文件。容器使用 `host.docker.internal:3000` 访问宿主机Langfuse，容器内部localhost指自身。

镜像入口为 `ENTRYPOINT ["vllm", "serve"]`，后面的模型和参数直接传给vLLM。上面的 `-e PYTHONPATH=/opt/vllm-observer/bootstrap` 让vLLM解释器启动时自动发现sitecustomize；API和EngineCore子进程继承环境变量后同样加载打点。没有额外的Python启动包装脚本。

这里的PYTHONPATH是Linux容器路径，不是Windows宿主机路径。如自定义基础镜像原本已有PYTHONPATH，请在 `-e` 中用冒号拼接，例如 `/opt/vllm-observer/bootstrap:/existing/container/path`，不要用宿主机的分号或Windows路径。已有sitecustomize时需按 [启动指南](sitecustomize.md)合并部署启动逻辑，Python只会自动加载一个同名模块；不会再由额外启动脚本检查冲突。

等待模型下载、初始化和API启动成功。日志跟随时Ctrl+C只退出日志查看，容器仍运行。若服务退出，执行 `docker ps -a --filter name=observer-vllm` 和 `docker logs --tail 100 observer-vllm` 排查。首次执行需要Docker Hub、Python包源与Hugging Face网络访问。

## 5. 验证真实模型、日志与 Langfuse

```powershell
Invoke-RestMethod http://localhost:18000/health
Invoke-RestMethod http://localhost:18000/v1/models
.venv/Scripts/python scripts/smoke.py --base-url http://localhost:18000 --model qwen-observed

# 在已经设置密钥的终端中执行；宿主机使用localhost查询Langfuse。
$env:LANGFUSE_BASE_URL = 'http://localhost:3000'
.venv/Scripts/python scripts/verify_langfuse.py --server-url http://localhost:18000 --model qwen-observed
```

验证脚本需要本工程Python环境与Node.js/npx。它请求真实模型，然后从Langfuse读回 `serve-model-request → generate-response → queue/prefill/decode` 五个节点，检查层级、起止时间、模型与实际token统计、decode聚合，以及每个节点原生Trace ID与metadata.traceId一致。成功打印 `hierarchy_verified` 和trace链接；只有HTTP根节点不算完整通过。

按输出ID查请求汇总日志：

```powershell
$observerTraceId = '替换为脚本输出的32位trace_id'
docker logs observer-vllm 2>&1 | Select-String -SimpleMatch $observerTraceId
```

检查JSON的trace_id一致、engine_requests大于0、存在queue/prefill/decode与count/total_seconds/mean_seconds。在Prometheus页面查询 `observer_instance_info`，应看到 `service="local-qwen"`、`model="qwen-observed"` 与本次instance ID。API启动后即有主动心跳，不依赖推理请求。

## 6. 手工启动 Grafana

```powershell
$observerGrafanaSecret = Read-Host '设置 Grafana admin 密码' -AsSecureString
$env:GF_SECURITY_ADMIN_PASSWORD = [System.Net.NetworkCredential]::new('', $observerGrafanaSecret).Password
$observerRoot = (Get-Location).Path
docker run -d --name observer-grafana --network observer-manual `
    -p 127.0.0.1:13001:3000 -e GF_SECURITY_ADMIN_PASSWORD `
    --mount "type=bind,source=$observerRoot/deploy/grafana/provisioning,target=/etc/grafana/provisioning,readonly" `
    --mount "type=bind,source=$observerRoot/deploy/grafana/dashboards,target=/var/lib/grafana/dashboards,readonly" `
    --mount type=volume,source=observer-grafana-data,target=/var/lib/grafana `
    grafana/grafana:11.6.0
```

打开 http://localhost:13001 ，用admin和刚设置的密码登录，查看 **vLLM Observer · 主动上报**。数据源用网络内的prometheus别名访问接收端。13001避开本机已有new-api的3001端口。

## 更新与停止

配置不变时，`docker stop --time 30 observer-vllm`停止，`docker start observer-vllm`重新启动。进程环境变量在容器创建时固定；改宿主机变量再start不会更新容器配置。

observer代码修改后，重做第3步构建，再替换本指南创建的模型容器：

```powershell
docker stop --time 30 observer-vllm
docker rm observer-vllm
# 重新执行第4步的docker run命令。
```

模型缓存保存在observer-hf-cache卷。关闭打点将第4步OBSERVER_ENABLED改为0并重建容器。增加副本需改容器名、宿主机端口和instance ID，并按总资源预算分配。监控服务分别用 `docker stop observer-prometheus observer-grafana` 停止。

## 昇腾 Linux 环境直接安装

在已有可正常推理的vLLM-Ascend环境中，也可直接安装observer并启动模型：

```bash
cd /opt/vllm-observer
python -m pip install '.[langfuse]'
export PYTHONPATH="/opt/vllm-observer/bootstrap${PYTHONPATH:+:$PYTHONPATH}"
export OBSERVER_ENABLED=1
export OBSERVER_SERVICE=chat
export OBSERVER_MODEL=qwen
export OBSERVER_INSTANCE_ID=ascend-01
export OBSERVER_REMOTE_WRITE_URL=http://prometheus-host:19090/api/v1/write
export OBSERVER_LANGFUSE_ENABLED=1
export OBSERVER_ENVIRONMENT=production
# 在该环境注入LANGFUSE_BASE_URL、LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY。
vllm serve /models/qwen --served-model-name qwen
```

将地址/路径替换为实际值，保留现场CANN初始化、NPU映射、TP和模型启动参数，不关闭log stats。NVIDIA镜像与 `--gpus all` 不适用于Ascend。实际NPU的阶段采集仍需运行第5步的真实模型验收脚本。

## 验证范围

GPU透传检查已通过。当前PYTHONPATH直接注入方案完成53项观察器回归（含子进程继承启用/关闭打点）；9个PowerShell命令块通过语法解析检查。未构建vLLM派生镜像、下载模型或完成真实GPU/CPU/NPU推理验收。

参考：[Docker run](https://docs.docker.com/engine/containers/run/)、[vLLM容器](https://docs.vllm.ai/en/stable/deployment/docker/)、[vLLM CPU](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/)。
