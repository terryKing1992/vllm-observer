# vLLM 推理指标上报与监控部署文档（Linux 服务器版）

> 适用环境：Linux 服务器（Ubuntu 20.04+ / CentOS 8+），NVIDIA GPU 直通，Docker + NVIDIA Container Toolkit。
> 与 Windows/WSL2 版的差异：无需 `VLLM_WSL2_ENABLE_PIN_MEMORY`；PD 的 role/group 标签统一用**环境变量**注入；镜像信息完整列出。

## 1. 架构总览

```
┌──────────────┐   /metrics 5s  ┌────────────┐             ┌────────────┐
│ observer-vllm │ ─────────────▶ │            │             │  Grafana   │
│ 单机推理实例   │  job=vllm      │            │  datasource │  :13001    │
├──────────────┤                │            │◀───────────▶│            │
│ vllm-pd-p     │ ────────────▶ │ Prometheus │             │ 面板:       │
│ role=prefill  │  file_sd      │  :19090    │             │ - vLLM     │
├──────────────┤  自动打标签     │ recording  │             │ - vLLM PD  │
│ vllm-pd-d     │ ────────────▶ │  rules ×7  │             │ - Observer │
│ role=decode   │               └────────────┘             └────────────┘
└──────────────┘
   group 由环境变量注入（默认 qwen-pd-01）
```

## 2. 镜像与版本清单

| 组件 | 镜像 | 版本说明 |
|---|---|---|
| vLLM 推理（单机 + observer） | `vllm-observer:0.1.0-vllm0.29.0-cuda` | 本仓库构建，基于 vLLM v0.29.0；公开镜像可用 `docker.m.daocloud.io/vllm/vllm-openai:v0.29.0` |
| Prometheus | `prom/prometheus:v3.2.1` | 与现环境一致 |
| Grafana | `grafana/grafana:11.6.0` | 与现环境一致 |
| 模型（示例） | 本地目录 `/data/models/qwen25-0.5b-instruct` | 0.5B，按实际替换 |

镜像拉取（国内服务器可走镜像代理）：

```bash
docker pull prom/prometheus:v3.2.1
docker pull grafana/grafana:11.6.0
docker pull docker.m.daocloud.io/vllm/vllm-openai:v0.29.0
```

## 3. 前置条件（Linux）

```bash
# NVIDIA 驱动已装（nvidia-smi 可用），安装 Container Toolkit：
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 验证 GPU 在容器内可见
docker run --rm --gpus all docker.m.daocloud.io/vllm/vllm-openai:v0.29.0 nvidia-smi
```

创建部署目录与网络：

```bash
mkdir -p /opt/vllm-observer/deploy /opt/vllm-observer/targets
docker network create vllm-metrics
```

## 4. 环境变量（PD 标签统一定义处）

创建 `/opt/vllm-observer/deploy/.env`：

```bash
# ===== PD 角色与分组（Prometheus 聚合标签的唯一来源） =====
export VLLM_PD_GROUP="qwen-pd-01"        # 逻辑服务分组：同一套 PD 实例必须相同
export VLLM_PD_MODEL="qwen-pd"           # served-model-name

# ===== 实例角色（启动脚本按实例分别设置） =====
# export VLLM_ROLE="prefill"             # P 实例
# export VLLM_ROLE="decode"              # D 实例

# ===== 镜像 =====
export IMAGE_VLLM="vllm-observer:0.1.0-vllm0.29.0-cuda"
export IMAGE_PROM="prom/prometheus:v3.2.1"
export IMAGE_GRAFANA="grafana/grafana:11.6.0"

# ===== 端口 =====
export PROMETHEUS_PORT="19090"
export GRAFANA_PORT="13001"
export GRAFANA_ADMIN_PASSWORD="observer-admin"   # 生产必须改强密码

# ===== 模型与资源 =====
export MODEL_PATH="/data/models/qwen25-0.5b-instruct"
export GPU_UTIL_PD="0.14"                # 8GB 卡双实例参考值；A100/H100 可大幅调高
```

## 5. 部署步骤

### 5.1 启动 Prometheus + Grafana

`/opt/vllm-observer/deploy/prometheus.yaml`（PD 目标改用 **file_sd 自动发现**，标签由启动脚本生成）：

```yaml
global:
  evaluation_interval: 10s
rule_files:
  - /etc/prometheus/prometheus-rules.yml
scrape_configs:
  - job_name: vllm
    scrape_interval: 5s
    file_sd_configs:
      - files: ["/etc/prometheus/targets/*.json"]
        refresh_interval: 15s
```

聚合规则 `prometheus-rules.yml` 与仓库 `deploy/prometheus-rules.yml` 相同（`vllm_pd:*` 7 条）。

```bash
cd /opt/vllm-observer/deploy
source .env
docker run -d --name prometheus --restart always --network vllm-metrics \
  -v $PWD/prometheus.yaml:/etc/prometheus/prometheus.yml:ro \
  -v $PWD/prometheus-rules.yml:/etc/prometheus/prometheus-rules.yml:ro \
  -v /opt/vllm-observer/targets:/etc/prometheus/targets:ro \
  -p ${PROMETHEUS_PORT}:9090 \
  ${IMAGE_PROM} \
  --config.file=/etc/prometheus/prometheus.yml \
  --web.enable-remote-write-receiver

docker run -d --name grafana --restart always --network vllm-metrics \
  -e GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD} \
  -v $PWD/grafana/provisioning:/etc/grafana/provisioning:ro \
  -v $PWD/grafana/dashboards:/var/lib/grafana/dashboards:ro \
  -p ${GRAFANA_PORT}:3000 \
  ${IMAGE_GRAFANA}
```

> 原生 Linux 无需 WSL 的 pin-memory 变量；`--command` 后接 Prometheus 启动参数。

### 5.2 启动脚本（role/group 由环境变量注入）

`/opt/vllm-observer/deploy/run-vllm.sh`——启动一个 vLLM 实例并**自动注册**到 Prometheus file_sd：

```bash
#!/usr/bin/env bash
# 用法: VLLM_ROLE=prefill ./run-vllm.sh pd-p 18100
set -euo pipefail
source /opt/vllm-observer/deploy/.env

ROLE="${VLLM_ROLE:?必须设置 VLLM_ROLE=prefill|decode}"
SUFFIX="${1:?用法: VLLM_ROLE=prefill $0 <实例后缀> <端口>}"
PORT="${2:?缺少端口}"
NAME="vllm-${SUFFIX}"

# 1. 启动容器（role/group 同时写入容器环境变量，便于日志排障）
docker run -d --name "${NAME}" --restart always --gpus all --ipc=host \
  --network vllm-metrics \
  -e VLLM_ROLE="${ROLE}" \
  -e VLLM_GROUP="${VLLM_PD_GROUP}" \
  -v "${MODEL_PATH}:/models:ro" \
  -p 127.0.0.1:"${PORT}":8000 \
  "${IMAGE_VLLM}" \
  /models/$(basename "${MODEL_PATH}") \
  --served-model-name "${VLLM_PD_MODEL}" \
  --host 0.0.0.0 --port 8000 --dtype half \
  --max-model-len 1024 --max-num-seqs 2 \
  --gpu-memory-utilization "${GPU_UTIL_PD}" --enforce-eager

# 2. 生成 Prometheus file_sd 目标（标签真正生效的地方）
cat > "/opt/vllm-observer/targets/${NAME}.json" <<EOF
[{
  "targets": ["${NAME}:8000"],
  "labels": {
    "role": "${ROLE}",
    "group": "${VLLM_PD_GROUP}",
    "model": "${VLLM_PD_MODEL}"
  }
}]
EOF

echo "started ${NAME} role=${ROLE} group=${VLLM_PD_GROUP} port=${PORT}"
```

启动 PD 双实例：

```bash
chmod +x run-vllm.sh
VLLM_ROLE=prefill ./run-vllm.sh pd-p 18100
VLLM_ROLE=decode  ./run-vllm.sh pd-d 18101
```

> 同一逻辑服务扩容 N 个 P 实例：`VLLM_ROLE=prefill ./run-vllm.sh pd-p2 18102`，
> file_sd 15s 内自动生效，**无需改 Prometheus 配置**。

### 5.3 单机实例（可选，无 role 标签）

```bash
source .env
docker run -d --name observer-vllm --restart always --gpus all --ipc=host \
  --network vllm-metrics -v "${MODEL_PATH}:/models:ro" \
  -p 127.0.0.1:18000:8000 "${IMAGE_VLLM}" \
  /models/$(basename "${MODEL_PATH}") --served-model-name qwen-observed \
  --host 0.0.0.0 --port 8000 --dtype half --max-model-len 2048 \
  --max-num-seqs 4 --gpu-memory-utilization 0.5 --enforce-eager

cat > /opt/vllm-observer/targets/observer-vllm.json <<EOF
[{"targets": ["observer-vllm:8000"], "labels": {"model": "qwen-observed"}}]
EOF
```

## 6. 标签与聚合口径

| 标签 | 注入方式 | 说明 |
|---|---|---|
| `role` | 环境变量 `VLLM_ROLE` → file_sd labels | prefill / decode |
| `group` | 环境变量 `VLLM_PD_GROUP` → file_sd labels | 同一逻辑服务 P+D 同组（聚合 key） |
| `model` | 环境变量 `VLLM_PD_MODEL` | 多模型隔离 |

聚合取数口径（`prometheus-rules.yml`，与仓库一致）：

| 指标 | 取数侧 | 规则名 |
|---|---|---|
| TTFT P95 | `role="prefill"` | `vllm_pd:ttft_seconds:p95` |
| TPOT P95 | `role="decode"` | `vllm_pd:tpot_seconds:p95` |
| 队列 / 运行数 | 按 role 分列 | `vllm_pd:queue_waiting` / `requests_running` |
| Prompt 吞吐 | prefill | `vllm_pd:prompt_tokens_per_sec` |
| 生成吞吐 | decode | `vllm_pd:gen_tokens_per_sec` |
| KV Cache | 按实例分列 | `vllm_pd:kv_cache_usage` |

## 7. Grafana 面板

面板 JSON 从仓库 `deploy/grafana/dashboards/` 复制即可（observer.json / vllm-pd.json / vllm-native.json），
provisioning 目录一并复制，Grafana 自动加载：

```bash
cp -r vllm-observer/deploy/grafana /opt/vllm-observer/deploy/
docker restart grafana
```

访问 `http://<服务器IP>:${GRAFANA_PORT}`（admin / `${GRAFANA_ADMIN_PASSWORD}`）。

## 8. 验证清单

```bash
# 1. 目标全部 up，P/D 携带 role/group 标签
curl -s http://localhost:${PROMETHEUS_PORT}/api/v1/targets | \
  python3 -m json.tool | grep -E '"job"|"role"|"group"|"health"'

# 2. 指标入库
curl -s "http://localhost:${PROMETHEUS_PORT}/api/v1/query?query=vllm:num_requests_running"

# 3. 聚合规则生效（7 条）
curl -s http://localhost:${PROMETHEUS_PORT}/api/v1/rules | grep vllm_pd

# 4. 发流量后查时延
curl -s "http://localhost:${PROMETHEUS_PORT}/api/v1/query?query=vllm_pd:ttft_seconds:p95"
```

## 9. 常见问题（Linux 版）

| 现象 | 原因 | 解决 |
|---|---|---|
| 容器内看不到 GPU | 未装 nvidia-container-toolkit | 见第 3 节安装并重启 docker |
| 面板 `bucket label "le" is missing` | histogram 聚合漏 le | 查询必须 `sum by (group, role, le) (rate(..._bucket[5m]))` |
| 时延面板无数据 | 直方图依赖窗口内流量（5m） | 发流量后查看；建议保留 30s 探活请求 |
| PD 目标不出现 / 标签缺失 | targets/*.json 未生成或格式错误 | 检查 `run-vllm.sh` 第 2 步输出；`docker logs prometheus` 看 file_sd 报错 |
| Grafana 密码不对 | 环境变量仅首次初始化生效 | `docker exec grafana grafana cli --homepath /usr/share/grafana admin reset-admin-password <新密码>` |
| 实例间 OOM / 显存不足 | GPU_UTIL_PD 过高 | 8GB 卡双实例从 0.14 起步；大卡按需调高 |
| 共享内存不足 | 多进程 IPC | 已加 `--ipc=host`；否则需 `--shm-size` |

## 10. 生产化建议

1. **真实 PD 分离**：本方案为监控拓扑等价（独立实例 + role 标签，无 KV 传输）；生产用 vLLM disagg + Mooncake/LMCache，额外暴露 KV transfer 指标
2. **服务发现**：K8s 环境把 file_sd 换成 `kubernetes_sd_configs`，role/group 从 Pod 标签 relabel，规则与面板不变
3. **GPU 指标**：部署 `dcgm-exporter` DaemonSet 补齐硬件层指标（利用率/显存/温度/功耗）
4. **告警**：基于 `vllm_pd:*` 配置（TTFT P95 > 5s 且 waiting > 5 持续 2min → 扩容；P/D 队列持续失衡 → 配比调整）
5. **凭据**：`GRAFANA_ADMIN_PASSWORD` 等全部换强随机值，`.env` 权限 600，不入库
