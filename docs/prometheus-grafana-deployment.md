# Prometheus + Grafana 监控部署文档（Linux）

> 目标：在 Linux 服务器上只部署 Prometheus + Grafana 监控栈，接入任意图暴露 `/metrics` 的服务
> （vLLM、业务应用等）。聚合规则与面板配置教程见附录。

## 1. 组件与版本

| 组件 | 镜像 | 端口（示例） |
|---|---|---|
| Prometheus | `prom/prometheus:v3.2.1` | 19090 |
| Grafana | `grafana/grafana:11.6.0` | 13001 |

## 2. 前置条件

```bash
# Docker 已安装并运行
docker --version

# 创建部署目录与专用网络
mkdir -p /opt/monitoring/{targets,grafana}
docker network create monitoring
```

## 3. 环境变量

创建 `/opt/monitoring/.env`：

```bash
export IMAGE_PROM="prom/prometheus:v3.2.1"
export IMAGE_GRAFANA="grafana/grafana:11.6.0"
export PROMETHEUS_PORT="19090"
export GRAFANA_PORT="13001"
export GRAFANA_ADMIN_PASSWORD="observer-admin"   # 生产必须改强密码
```

## 4. 部署 Prometheus

### 4.1 配置文件 `/opt/monitoring/prometheus.yaml`

```yaml
global:
  evaluation_interval: 10s
rule_files:                              # 暂无规则可先注释掉
  - /etc/prometheus/rules/*.yml
scrape_configs:
  - job_name: monitoring-self            # 采集自身，验证链路
    static_configs:
      - targets: ["localhost:9090"]
  - job_name: generic-metrics            # 通用采集：file_sd 自动发现
    scrape_interval: 5s
    file_sd_configs:
      - files: ["/etc/prometheus/targets/*.json"]
        refresh_interval: 15s
```

### 4.2 启动

```bash
cd /opt/monitoring && source .env
mkdir -p rules targets

docker run -d --name prometheus --restart always --network monitoring \
  -v /opt/monitoring/prometheus.yaml:/etc/prometheus/prometheus.yml:ro \
  -v /opt/monitoring/rules:/etc/prometheus/rules:ro \
  -v /opt/monitoring/targets:/etc/prometheus/targets:ro \
  -p ${PROMETHEUS_PORT}:9090 \
  ${IMAGE_PROM} \
  --config.file=/etc/prometheus/prometheus.yml \
  --web.enable-remote-write-receiver \
  --storage.tsdb.retention.time=15d
```

说明：
- `--web.enable-remote-write-receiver`：允许 Pushgateway/应用 remote-write 推送（不需要可去掉）
- `--storage.tsdb.retention.time=15d`：数据保留 15 天，按磁盘调整

### 4.3 添加采集目标（file_sd，无需重启）

```bash
cat > /opt/monitoring/targets/my-app.json <<'EOF'
[{
  "targets": ["my-app:8000"],
  "labels": {"service": "my-app", "env": "prod"}
}]
EOF
```

把 `my-app` 容器接入网络即可：`docker network connect monitoring my-app`。
15 秒内自动生效；标签（role/group 等）在这里定义，供后续聚合使用。

## 5. 部署 Grafana

### 5.1 数据源自动配置 `/opt/monitoring/grafana/provisioning/datasources/prometheus.yaml`

```yaml
apiVersion: 1
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
```

> 容器名 `prometheus` 与 `--name` 保持一致，走内部网络解析。

### 5.2 启动

```bash
docker run -d --name grafana --restart always --network monitoring \
  -e GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD} \
  -v /opt/monitoring/grafana/provisioning:/etc/grafana/provisioning:ro \
  -v /opt/monitoring/grafana/dashboards:/var/lib/grafana/dashboards:ro \
  -p ${GRAFANA_PORT}:3000 \
  ${IMAGE_GRAFANA}
```

## 6. 验证

```bash
# 1. Prometheus 自身 target up
curl -s http://localhost:${PROMETHEUS_PORT}/api/v1/targets | grep -o '"health":"[^"]*"'

# 2. 数据源连通（Grafana → Prometheus）
curl -s -u admin:${GRAFANA_ADMIN_PASSWORD} \
  http://localhost:${GRAFANA_PORT}/api/datasources/uid/prometheus/health

# 3. 浏览器访问
# Grafana:    http://<服务器IP>:13001  (admin / ${GRAFANA_ADMIN_PASSWORD})
# Prometheus: http://<服务器IP>:19090
```

## 7. 日常运维

| 操作 | 命令 |
|---|---|
| 添加采集目标 | 写 `targets/*.json`，自动生效 |
| 修改聚合规则 | 写 `rules/*.yml` → `curl -X POST http://localhost:19090/-/reload`* |
| 添加/修改面板 | 放 `grafana/dashboards/*.json`，约 10s 自动加载 |
| 重置 Grafana 密码 | `docker exec grafana grafana cli --homepath /usr/share/grafana admin reset-admin-password <新密码>` |
| 数据持久化 | Prometheus 数据在容器内 `/prometheus`，生产建议加 `-v prom-data:/prometheus` |

*\* 需要 `--web.enable-lifecycle` 启动参数；否则 `docker restart prometheus`。*

## 8. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| target 一直 DOWN | 目标容器不在同一网络 / 未监听 0.0.0.0 | `docker network connect`；应用监听 `0.0.0.0` 而非 `127.0.0.1` |
| Grafana 面板 No data | 数据源 URL 不通 / 查询窗口无样本 | 检查第 6 节健康接口；时延类直方图指标需先有流量 |
| Grafana 登录密码无效 | 环境变量仅首次初始化生效 | 见第 7 节重置命令 |
| `bucket label "le" is missing` | histogram 查询聚合漏掉 le | 必须 `sum by (..., le) (rate(..._bucket[5m]))` |
| rules 不生效 | rule_files 路径/格式错误 | `curl http://localhost:19090/api/v1/rules` 看报错 |

---

# 附录 A：聚合规则（Recording Rules）配置教程

## A.1 为什么用聚合规则

把复杂 PromQL（分位数、多实例求和、口径选择）预先算好存成新指标，好处：
1. 面板/告警直接引用，查询快、口径统一
2. 多实例聚合（如 PD 双角色）在规则层固定取数侧，避免重复计数

## A.2 示例：vLLM PD 分离聚合规则

`/opt/monitoring/rules/vllm-pd.yml`：

```yaml
groups:
  - name: vllm-pd
    rules:
      # TTFT 只取 prefill 实例（histogram 必须 rate → sum by(le) → quantile）
      - record: vllm_pd:ttft_seconds:p95
        expr: histogram_quantile(0.95,
                sum by (group, le) (rate(vllm:time_to_first_token_seconds_bucket{role="prefill"}[2m])))
      # TPOT 只取 decode 实例
      - record: vllm_pd:tpot_seconds:p95
        expr: histogram_quantile(0.95,
                sum by (group, le) (rate(vllm:request_time_per_output_token_seconds_bucket{role="decode"}[2m])))
      # 队列/运行数按角色分列（观察 P/D 配比失衡）
      - record: vllm_pd:queue_waiting
        expr: sum by (group, role) (vllm:num_requests_waiting)
      - record: vllm_pd:requests_running
        expr: sum by (group, role) (vllm:num_requests_running)
      # 吞吐去重：prompt 取 P 侧、generation 取 D 侧
      - record: vllm_pd:prompt_tokens_per_sec
        expr: sum by (group) (rate(vllm:prompt_tokens_total{role="prefill"}[2m]))
      - record: vllm_pd:gen_tokens_per_sec
        expr: sum by (group) (rate(vllm:generation_tokens_total{role="decode"}[2m]))
      # KV Cache 按实例分列，禁止跨角色聚合
      - record: vllm_pd:kv_cache_usage
        expr: max by (group, role, instance) (vllm:kv_cache_usage_perc)
```

## A.3 编写要点

1. **命名规范**：`level:metric:operations`（如 `vllm_pd:ttft_seconds:p95`）
2. **histogram 三步法**：`histogram_quantile(q, sum by (保留标签, le) (rate(bucket[m])))`——**le 必须保留**
3. **counter 用 rate**：计数器必须 `rate(...[2m])` 后再聚合，不能直接求和
4. **多角色去重口径**：先在表格里约定每个指标的唯一取数侧（P 或 D），再写规则
5. 生效验证：`curl http://localhost:19090/api/v1/rules` 查 health 与 lastError

---

# 附录 B：Grafana 面板配置教程

## B.1 面板自动加载（provisioning）

`/opt/monitoring/grafana/provisioning/dashboards/dashboards.yaml`：

```yaml
apiVersion: 1
providers:
  - name: default
    type: file
    options:
      path: /var/lib/grafana/dashboards
```

之后把任意 dashboard JSON 放入 `grafana/dashboards/`，约 10s 自动加载，修改同路径覆盖即热更新。

## B.2 导入社区面板

Grafana UI → Dashboards → New → Import → 输入grafana.com 的面板 ID →
数据源选 `Prometheus` → Import。

## B.3 手工建面板的关键配置

| 配置项 | 推荐值 | 说明 |
|---|---|---|
| 数据源 | Prometheus | |
| 面板类型 | Time series（曲线）/ Stat（单值）/ Gauge（水位） | |
| 查询窗口 | `[5m]` | 太短空闲时段易空，太长反应迟钝 |
| Unit | 时延选 `seconds`，吞吐选 `pps`，占比选 `percent(0.0-1.0)` | |
| Legend | `{{role}}/{{instance}}` 等 | 用标签动态命名序列 |
| Thresholds | 如 TTFT P95 黄 1s / 红 5s | 配合告警使用 |

## B.4 常用查询模板

```promql
# P95 分位数（多实例聚合，le 必须保留）
histogram_quantile(0.95, sum by (group, le) (rate(metric_bucket[5m])))

# 按服务聚合 QPS
sum by (service) (rate(http_requests_total[5m]))

# 成功率
sum by (service) (rate(http_requests_total{status=~"2.."}[5m]))
  / sum by (service) (rate(http_requests_total[5m]))

# 错误率告警示例（P95 > 1% 持续 5 分钟）
# sum(rate(errors_total[5m])) / sum(rate(requests_total[5m])) > 0.01
```

## B.5 引用附录 A 的聚合指标建面板

规则生效后，聚合指标与普通指标用法完全一致，直接查询即可：

```promql
vllm_pd:ttft_seconds:p95
vllm_pd:queue_waiting{role="prefill"}
```

好处：面板查询极简、口径统一，多面板复用同一条规则时性能远优于各写各的复杂 PromQL。
