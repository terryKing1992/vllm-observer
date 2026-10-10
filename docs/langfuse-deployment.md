# Langfuse 可观测平台部署文档（vLLM Trace 上报）

> Langfuse 用于承接 vllm-observer 上报的**调用链 Trace**（Prompt/Completion/时延/token 用量），
> 与 Prometheus 指标监控互补：Prometheus 看"服务是否健康"，Langfuse 看"每次调用具体发生了什么"。

## 1. 架构与组件

```
observer-vllm ──trace 上报──▶ Langfuse Web (:3000) ──事件──▶ S3(MinIO)
                                    │        │
                              Postgres    ClickHouse ◀── Worker 异步消费
                              (元数据)    (trace 明细)
                                    │
                                  Redis (队列)
```

部署文件：`deploy/langfuse/docker-compose.yml`（Langfuse v4 自带依赖全家桶）

| 服务 | 镜像 | 端口 | 用途 |
|---|---|---|---|
| langfuse-web | langfuse/langfuse:4 | 0.0.0.0:3000 | Web UI + Ingestion API |
| langfuse-worker | langfuse/langfuse-worker:4 | 3030（内部） | 异步事件处理 |
| postgres | postgres:17 | 127.0.0.1:5432 | 组织/项目/用户元数据 |
| clickhouse | clickhouse-server:25.12 | 127.0.0.1:8123 | Trace/Observation 明细存储 |
| minio | minio | 0.0.0.0:9090 | 事件与媒体对象存储（S3 协议） |
| redis | redis:7 | 127.0.0.1:6379 | 队列 |

## 2. 部署步骤

```powershell
cd E:\github\vllm-workspace\vllm-observer\deploy\langfuse
docker compose up -d
docker compose ps   # 6 个服务全部 healthy
```

访问 **http://localhost:3000**。

## 3. 预置账号与项目（无需 UI 手工创建）

compose 通过 `LANGFUSE_INIT_*` 环境变量自动初始化：

| 项 | 值 |
|---|---|
| 组织 / 项目 | Observer Dev / vllm-observer |
| 登录用户 | `dev@observer.local` |
| 登录密码 | `observer-dev-password` |
| Project Public Key | `pk-lm-observer-local` |
| Project Secret Key | `sk-lm-observer-local` |

> 以上均为本地开发凭据（compose 文件内注明 dev-only），生产环境必须全部替换，
> 并将 `NEXTAUTH_SECRET` / `SALT` / `ENCRYPTION_KEY` 换成强随机值。

## 4. vLLM 侧接入 Trace 上报

observer 侧配置以下环境变量后，推理请求的 prompt/completion/时延/token 数会作为
Generation Trace 自动上报：

```yaml
# observer-vllm 容器环境变量
LANGFUSE_PUBLIC_KEY: pk-lm-observer-local
LANGFUSE_SECRET_KEY: sk-lm-observer-local
LANGFUSE_HOST: http://localhost:3000
```

上报后可在 Langfuse UI 的 Traces 页按 model / user / latency / token 过滤分析，
支持每次请求的完整 Prompt-Completion 对照。

## 5. 数据持久化

compose 使用命名卷，`docker compose down` 不丢数据（需删除卷才清除）：

| 卷 | 内容 |
|---|---|
| langfuse_postgres_data | 元数据 |
| langfuse_clickhouse_data | Trace 明细 |
| langfuse_minio_data | 事件/媒体对象 |
| langfuse_redis_data | 队列 |

完全重置（清空所有 trace）：`docker compose down -v`

## 6. 验证清单

```powershell
# 1. 服务健康
docker compose ps                        # 6 服务 healthy
curl http://localhost:3000/api/public/health

# 2. 向推理实例发请求
curl http://localhost:18000/v1/completions -H "Content-Type: application/json" `
  -d '{"model":"qwen-observed","prompt":"你好","max_tokens":32}'

# 3. UI 查看
# http://localhost:3000 → 登录 → 项目 vllm-observer → Traces
# 应出现新 trace，含 latency / token 用量 / prompt 内容
```

## 7. 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| Traces 一直为空 | observer 未配置 key/host，或请求未经过 observer | 检查第 4 节环境变量；发一条新请求验证 |
| `langfuse-langfuse-web-1` 反复重启 | 依赖服务未 healthy | `docker compose ps` 看 clickhouse/minio 状态；首次启动需等迁移完成 |
| 媒体上传失败 | minio endpoint 用了容器内地址 | web 服务中 `LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` 需浏览器可达（本配置为 localhost:9090） |
| 端口 3000 被占用 | Grafana 默认端口也是 3000 | 本栈 Grafana 已改 13001；如仍冲突改 compose 端口映射 |
| Redis 连接失败 | REDIS_AUTH 不匹配 | web/worker 与 redis 服务的 `REDIS_AUTH` 必须一致 |

## 8. 生产化建议

1. **存储**：ClickHouse 与 Postgres 换独立集群实例，MinIO 换云上对象存储（OSS/S3）
2. **凭据**：所有 secret 换强随机值并走密钥管理，禁止随 compose 明文分发
3. **采样**：高 QPS 场景在 observer 侧配置 trace 采样率，避免全量上报成本过高
4. **容量**：ClickHouse 按保留期（如 30 天）配置 TTL；MinIO 桶生命周期策略同理
5. **与指标联动**：trace 的 latency 字段可回流 Prometheus 做业务维度时延大盘
