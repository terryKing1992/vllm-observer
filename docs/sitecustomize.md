# sitecustomize 可选打点

目标：不编辑 vLLM/vLLM-Ascend 文件，不注册 vLLM 插件，不要求业务代码调用 SDK，保留原有 vllm serve 参数。通过 Python 标准启动机制加载本工程的可选运行时包装。这种“无源码侵入”仍会在启用时包装进程内的方法；不宣称零运行时开销。

## Linux / 昇腾

安装本工程及所需依赖，将工程 bootstrap 目录随部署挂载。然后在模型启动环境中加入：

```bash
export PYTHONPATH="/opt/vllm-observer/bootstrap${PYTHONPATH:+:$PYTHONPATH}"
export OBSERVER_ENABLED=1
export OBSERVER_REMOTE_WRITE_URL=http://prometheus:9090/api/v1/write
export OBSERVER_LANGFUSE_ENABLED=1
# LANGFUSE_BASE_URL / PUBLIC_KEY / SECRET_KEY 通过既有 Secret 配置
vllm serve /path/to/model # 保留原有模型、TP等参数
```

只启用日志/指标时不设置 Langfuse 开关；开启 Langfuse 时引擎时钟校准 hook 同时生效。API 与 EngineCore 的所有 Python 子进程都必须继承上述环境，并能访问 bootstrap 与已安装包。

## Windows

在 vllm-observer 目录：

```powershell
$observerBootstrap = (Resolve-Path './bootstrap').Path
$env:PYTHONPATH = $observerBootstrap + [IO.Path]::PathSeparator + $env:PYTHONPATH
$env:OBSERVER_ENABLED = '1'
.venv/Scripts/python -c "import sys; print(any(getattr(x, '_observer_finder', False) for x in sys.meta_path))"
```

结果 True 表示启动 hook 安装成功，不意味着真实 vLLM/NPU 可在 Windows 运行。demo 使用相同采集/导出链，但不模拟真实设备兼容性。

## 机制

1. Python 从 PYTHONPATH 找到 bootstrap/sitecustomize.py。默认开关为0，关闭时不导入观察器。
2. 启用时仅注册窄范围 import hook，不提前导入 vLLM/torch、不启动后台线程。
3. vLLM 自己加载模块后，观察器尝试包装 AsyncLLM、请求统计和 EngineCore 两种 step 路径。
4. 包装 build_app 自动加入可选 ASGI 中间件。已显式配置中间件时不重复加入。
5. 模型应用启动成功后，API进程才运行指标发送/trace导出；worker的校准只附加时间元数据，无网络或SDK线程。

观察器依赖缺失、版本不匹配或安装失败仅告警并保留原应用。vLLM 自己的 import/模型初始化异常仍正常传播，不隐藏上游问题。失败降级意味着观测可能缺失，应监控启动告警与 timeline_unavailable。

## 开关与边界

| 开关 | 默认 | 作用 |
|---|---|---|
| OBSERVER_ENABLED | 0 | 总开关，仅 sitecustomize 使用 |
| OBSERVER_HTTP_ENABLED | 1 | 是否自动添加请求中间件 |
| OBSERVER_ENGINE_ENABLED | 1 | 是否包装引擎采集及时间校准 |
| OBSERVER_LANGFUSE_ENABLED | 0 | 是否导出 Langfuse，并启用引擎时钟校准 |
| OBSERVER_ENVIRONMENT | production | Langfuse环境；demo验收用development |

开关应在进程启动前设置。关闭总开关后重启即可恢复原启动行为，无需回滚上游文件。显式手动使用 ObserverMiddleware 或demo不受总开关控制，因为它们属于调用方主动加载。

Python 默认仅自动加载一个 sitecustomize。若部署已存在同名文件，不要用本shim遮住它：保留已有文件，并由部署维护者在其中以 try/except 调用 `vllm_observer.bootstrap.install()`，或合并两者启动逻辑。本工程不会修改用户全局 sitecustomize。仅装 wheel 时也需单独部署 bootstrap/sitecustomize.py。

`python -S`/隔离启动导致未加载 site 或忽略 PYTHONPATH 时，本方案不生效。支持当前 V1 `vllm serve` 与 launcher build_app 路径；旧版直接 `python -m ...api_server` 且 build_app 在 __main__ 内定义，不承诺自动接入。自定义 engine/scheduler替换这些方法时须重新验证。

仍需每副本一个API进程。导入/生命周期行为已用真实 Python 子进程验证；真实昇腾设备、多节点时钟同步与性能开销需现场验收。
