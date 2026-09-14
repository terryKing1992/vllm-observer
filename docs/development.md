# AI Coding 工程工作流

1. 写明场景、输入输出、验收行为与性能约束。
2. 核对依赖源码/SHA，优先公开扩展入口；运行时包装记录兼容风险。
3. 设计事件与计时口径，缺失值不当作零，不混用 clock domain。
4. 测试前明确要防的错误、输入输出与最低成本层级：核心单测 → ASGI → SDK/网络 → NPU。
5. 分模块实现，执行测试/lint，更新配置和验收记录。
6. 审查并发隔离、队列上限、标签基数、热路径；硬件性能结论必须来自实测。

core 是事件/Filter，adapter 是版本相关采集，middleware 是 HTTP 生命周期，metrics 是指标，exporter 是后台上报，demo 是无卡演示。部署在 deploy，指导在 docs。
remote_write 是实例定时主动推送任务及 Remote Write 编码；metrics 只负责内存采集，请求过滤器不执行指标网络上报。新增指标使用同一 registry 即自动进入下一次推送。

bootstrap/sitecustomize.py 是部署入口，bootstrap.py 是延迟import包装，engine_clock.py 只在引擎最终输出附加同进程时钟校准。用module契约和真实Python子进程验证打点安装/降级，不能通过直接修改vLLM文件实现接入。Langfuse timeline结构及时间范围需上报后实际读回审计，不能只检查HTTP返回成功。

新增过滤器示例（保存到可导入模块，再用 --middleware 指定该类）：

```python
from vllm_observer.core import ConsoleFilter
from vllm_observer.metrics import MetricsFilter
from vllm_observer.middleware import ObserverMiddleware

class AddDeploymentFilter:
    def process(self, event):
        return {**event, "deployment": "canary"}

class CustomMiddleware(ObserverMiddleware):
    def __init__(self, app):
        metrics = MetricsFilter()
        super().__init__(app, metrics=metrics,
                         filters=[metrics, AddDeploymentFilter(), ConsoleFilter()])
```

显式传入 filters 会替换默认链；如需异步导出，需要加入 AsyncExportFilter 并管理其关闭。

新日志目标扩展 Filter/Sink，新阶段扩展 Adapter 并先定义边界。不在观测热路径做设备同步。测试替身不证明真实二进制兼容。

CI 提供 Windows/Linux × Python 3.10/3.13。工作流需推送后执行，不宣称本地通过等同 CI 已运行。开发使用隔离虚拟环境，可记录 pip freeze；不要用开发环境锁定清单覆盖已有昇腾镜像依赖。
