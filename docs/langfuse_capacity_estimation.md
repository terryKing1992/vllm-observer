# Langfuse OTLP 摄入容量估算（前置条件）

本文档是 ``OBSERVER_LANGFUSE_SAMPLE_RATE`` 调高前的容量评估记录。
公式：日摄入量 = QPS x 86400 x 采样率 x 每请求 span 数。

## 每请求 span 数（MECE 时间线）

| span | 数量 |
|------|------|
| root (serve-model-request) | 1 |
| api_prepare + 嵌套 (render_input / input_process / submit_engine) | 4 |
| generate-response | 1 |
| engine_handoff / waiting / prefill / decode / engine_finalize | 5 |
| response_transfer | 1 |
| 合计 | 12（竞争间隙升格为子 span 时 +N） |

## 参考部署估算（示例：8 卡单实例，QPS 20）

| 采样率 | 日 trace 数 | 日 span 数 | 说明 |
|--------|------------|-----------|------|
| 0.01（默认） | 17,280 | ~207,360 | 云版 Pro 套餐内 |
| 0.1 | 172,800 | ~2,073,600 | 需确认套餐配额与费用 |
| 1.0（错误必采叠加） | +错误量 | +12x错误量 | 错误率 >1% 时显著放大 |

## 调整规则

1. 未填写本表对应部署的实际 QPS 与目标采样率之前，禁止调高采样率。
2. 错误率高的灰度期优先降低正常采样率，保持错误必采。
3. 每次调整采样率在本文追加一行记录（日期 / 实例 / QPS / 采样率 / 预估日 span 数）。
