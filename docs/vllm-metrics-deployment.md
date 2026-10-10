# vLLM 推理指标上报与监控部署文档

> 适用环境：Windows + Docker Desktop（WSL2 后端），GPU 本机直通。
> 本文档描述的组件均已在本仓库 `deploy/` 目录下落地，可直接复用。

## 1. 架构总览

```
┌──────────────┐    /metrics     ┌────────────┐              ┌────────────┐
│ observer-vllm │ ──────────────▶ │            │              │  Grafana   │
│ 单机推理实例   │  job=vllm       │            │  datasource │  :13001    │
├──────────────┤                 │ Prometheus │◀────────────▶│            │
│ vllm-pd-p     │ ──────────────▶ │  :19090    │   (prometheus uid)       │
│ Prefill 角色  │  job=vllm-prefill│           │              │ 面板:      │
├──────────────┤  role=prefill   │  recording │              │ - vLLM     │
│ vllm-pd-d     │ ──────────────▶ │   rules ×7 │              │ - vLLM PD  │
│ Decode 角色   │  job=vllm-decode│ (vllm_pd:*)│              │ - Observer │
└──────────────┘  role=decode    └────────────┘              └────────────┘
        同一 group=qwen-pd-01
```

## 2. 组件与端口

| 容器 | 镜像 | 宿主机端口 | 说明 |
|---|---|---|---|
| observer-vllm | vllm-observer:0.1.0-vllm0.29.0-cuda | 127.0.0.1:18000 | 单机推理实例，内置 observer 上报 |
| vllm-pd-p | 同上 | 127.0.0.1:18100 | PD 演示 - Prefill 角色 |
| vllm-pd-d | 同上 | 127.0.0.1:18101 | PD 演示 - Decode 角色 |
| deploy-prometheus-1 / vllm-observer-metrics-prometheus-1 | prom/prometheus:v3.2.1 | 19090 | 指标采集与聚合 |
| vllm-observer-metrics-grafana-1 | grafana/grafana:11.6.0 | 13001 | 可视化 |

## 3. 部署步骤

### 3.1 启动 Prometheus + Grafana（compose）

```powershell
cd E:\github\vllm-workspace\vllm-observer\deploy
$env:GRAFANA_ADMIN_PASSWORD = "observer-admin"   # 也可写入 .env
docker compose up -d prometheus grafana
```

`compose.yaml` 关键挂载：

```yaml
volumes:
  - ./prometheus.yaml:/etc/prometheus/prometheus.yml:ro
  - ./prometheus-rules.yml:/etc/prometheus/prometheus-rules.yml:ro   # 聚合规则
  - ./grafana/provisioning:/etc/grafana/provisioning:ro              # 数据源/面板自动装配
  - ./grafana/dashboards:/var/lib/grafana/dashboards:ro              # 面板 JSON
```

### 3.2 启动推理实例

**单机实例**（含 observer 主动上报）：

```powershell
docker run -d --name observer-vllm --gpus all --ipc=host `
  -e VLLM_WSL2_ENABLE_PIN_MEMORY=1 `
  -v observer-hf-models:/models:ro `
  -p 127.0.0.1:18000:8000 `
  vllm-observer:0.1.0-vllm0.29.0-cuda `
  /models/qwen25-0.5b-instruct --served-model-name qwen-observed `
  --host 0.0.0.0 --port 8000 --dtype half --max-model-len 2048 `
  --max-num-seqs 4 --gpu-memory-utilization 0.5 --enforce-eager
```

> **WSL2 必需**：`VLLM_WSL2_ENABLE_PIN_MEMORY=1`，否则多实例场景报
> `RuntimeError: UVA is not available`。

**PD 双实例**（监控拓扑等价的 PD 环境，role 标签区分）：

```powershell
$img = "vllm-observer:0.1.0-vllm0.29.0-cuda"
docker run -d --name vllm-pd-p --gpus all --ipc=host `
  -e VLLM_WSL2_ENABLE_PIN_MEMORY=1 -v observer-hf-models:/models:ro `
  -p 127.0.0.1:18100:8000 $img `
  /models/qwen25-0.5b-instruct --served-model-name qwen-pd `
  --host 0.0.0.0 --port 8000 --dtype half --max-model-len 1024 `
  --max-num-seqs 2 --gpu-memory-utilization 0.14 --enforce-eager

docker run -d --name vllm-pd-d --gpus all --ipc=host `
  -e VLLM_WSL2_ENABLE_PIN_MEMORY=1 -v observer-hf-models:/models:ro `
  -p 127.0.0.1:18101:8000 $img `
  /models/qwen25-0.5b-instruct --served-model-name qwen-pd `
  --host 0.0.0.0 --port 8000 --dtype half --max-model-len 1024 `
  --max-num-seqs 2 --gpu-memory-utilization 0.14 --enforce-eager
```

### 3.3 接入监控网络（手动 run 的容器必须）

```powershell
docker network connect vllm-observer-metrics_default observer-vllm
docker network connect vllm-observer-metrics_default vllm-pd-p
docker network connect vllm-observer-metrics_default vllm-pd-d
```

> 容器每次重建后需要重新执行。生产建议统一由 compose 管理。

### 3.4 Prometheus 采集配置（deploy/prometheus.yaml）

```yaml
global:
  evaluation_interval: 10s
rule_files:
  - /etc/prometheus/prometheus-rules.yml
scrape_configs:
  - job_name: vllm                    # 单机实例
    scrape_interval: 5s
    static_configs:
      - targets: ["observer-vllm:8000"]
  - job_name: vllm-prefill            # PD - prefill 角色
    scrape_interval: 5s
    static_configs:
      - targets: ["vllm-pd-p:8000"]
        labels: {role: prefill, group: qwen-pd-01, model: qwen-pd}
  - job_name: vllm-decode             # PD - decode 角色
    scrape_interval: 5s
    static_configs:
      - targets: ["vllm-pd-d:8000"]
        labels: {role: decode, group: qwen-pd-01, model: qwen-pd}
```

**标签设计（PD 聚合的关键）**：

| 标签 | 说明 |
|---|---|
| `role` | prefill / decode，区分 P/D 实例 |
| `group` | 同一逻辑服务的 P+D 实例同组，服务级聚合依据 |
| `instance` | 单实例排障 |

Kubernetes 环境下改用 `kubernetes_sd_configs` + `relabel_configs` 从 Pod 标签
`vllm.role` / `vllm.group` 自动提取，规则与面板无需改动。

### 3.5 聚合口径（deploy/prometheus-rules.yml）

PD 分离下同一请求会同时出现在 P/D 指标中，必须固定取数侧，否则聚合翻倍：

| 指标 | 取数侧 | 规则名 |
|---|---|---|
| TTFT P95 | prefill | `vllm_pd:ttft_seconds:p95` |
| TPOT P95 | decode | `vllm_pd:tpot_seconds:p95` |
| 队列/运行数 | 按 role 分列 | `vllm_pd:queue_waiting` / `vllm_pd:requests_running` |
| Prompt 吞吐 | prefill | `vllm_pd:prompt_tokens_per_sec` |
| 生成吞吐 | decode | `vllm_pd:gen_tokens_per_sec` |
| KV Cache | 按实例分列（不跨角色聚合） | `vllm_pd:kv_cache_usage` |

修改规则后：`docker cp deploy\prometheus-rules.yml <prometheus容器>:/etc/prometheus/ && docker restart <prometheus容器>`。

## 4. Grafana 面板

访问 **http://localhost:13001**（admin / 环境变量 `GRAFANA_ADMIN_PASSWORD`）。

| 面板 | 文件 | 内容 |
|---|---|---|
| vLLM Observer · 主动上报 | `dashboards/observer.json` | observer_* 上报指标 + vLLM 原生指标（已合并 19 panel） |
| vLLM PD 分离架构监控 | `dashboards/vllm-pd.json` | 24 panel：服务级聚合 / P-D 分角色对比 / 阶段耗时拆解 / 缓存与请求画像 |
| vLLM Serving Overview | `dashboards/vllm-native.json` | 社区导入面板（ID 25620） |

面板 JSON 放在 `deploy/grafana/dashboards/` 下自动加载，修改后约 10s 生效。

## 5. 验证清单

```powershell
# 1. 采集目标全部 up
curl http://localhost:19090/api/v1/targets   # 3 个 target 均 up，P/D 带 role/group 标签

# 2. 指标入库
curl "http://localhost:19090/api/v1/query?query=vllm:num_requests_running"

# 3. 聚合规则生效
curl "http://localhost:19090/api/v1/rules"  # vllm_pd:* 7 条规则

# 4. 发流量后看时延
curl "http://localhost:19090/api/v1/query?query=vllm_pd:ttft_seconds:p95"
```

## 6. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `UVA is not available` | WSL2 多进程共享 GPU | 容器加 `-e VLLM_WSL2_ENABLE_PIN_MEMORY=1` |
| Grafana 登录密码不对 | `GF_SECURITY_ADMIN_PASSWORD` 仅首次初始化生效 | `docker exec <grafana> grafana cli --homepath /usr/share/grafana admin reset-admin-password observer-admin` |
| 面板 `bucket label "le" is missing` | histogram 聚合漏掉 le 标签 | 查询必须 `sum by (group, role, le) (rate(..._bucket[5m]))` |
| 时延面板无数据 | 直方图依赖窗口内流量（5m） | 发流量后查看；空闲时 No data 属正常 |
| Prometheus target 全部消失 | 容器重建后未接入监控网络 | 重新 `docker network connect` |
| compose up 报端口冲突 | 旧容器仍占用端口 | 先 `docker rm -f` 旧容器再 up |
| GPU 显存不足 | 8GB 卡跑 3 实例 | PD 实例 `--gpu-memory-utilization 0.14` 起步 |

## 7. 生产化建议（与本套环境差异）

1. **真实 PD 分离**：本环境为监控拓扑等价（两个独立实例 + role 标签），无 KV Cache 传输；生产使用 vLLM disagg + Mooncake/LMCache，额外暴露 KV transfer 耗时/失败指标
2. **GPU 指标**：vLLM 不暴露硬件层指标，需部署 dcgm-exporter（WSL 支持有限，生产 K8s 直接 DaemonSet）
3. **服务发现**：static_configs 换成 kubernetes_sd + relabel，标签从 Pod 注入
4. **探活流量**：保留低频探测请求（如 30s 一条），避免空闲时段时延面板空白被误判为监控故障
5. **告警**：基于 `vllm_pd:*` 规则配置（TTFT P95 > 5s 且 waiting > 5 持续 2min → 扩容）
