# vLLM EngineArgs 全量参数清单（自动生成）

> 从 `vllm/vllm/engine/arg_utils.py` 的 `EngineArgs` 数据类自动提取，共 **243** 个字段。
> 高频性能参数的深度解读见主文档 `vllm-params-pd-tuning.md`。
> 默认值列为源码表达式：`<-Class.field` 表示继承自对应 Config 类字段；截断以 `...` 标注。
> 分类为启发式归组，个别参数跨类别。

---

## 并行与分布式（43）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `distributed_executor_backend` | `(` | `` |
| 2 | `pipeline_parallel_size` | `int` | `ParallelConfig.pipeline_parallel_size` |
| 3 | `master_addr` | `str` | `ParallelConfig.master_addr` |
| 4 | `master_port` | `int` | `ParallelConfig.master_port` |
| 5 | `nnodes` | `int` | `ParallelConfig.nnodes` |
| 6 | `node_rank` | `int` | `ParallelConfig.node_rank` |
| 7 | `distributed_timeout_seconds` | `int | None` | `ParallelConfig.distributed_timeout_seconds` |
| 8 | `numa_bind` | `bool` | `ParallelConfig.numa_bind` |
| 9 | `numa_bind_nodes` | `list[int] | None` | `ParallelConfig.numa_bind_nodes` |
| 10 | `numa_bind_cpus` | `list[str] | None` | `ParallelConfig.numa_bind_cpus` |
| 11 | `device_ids` | `list[int | str] | None` | `None` |
| 12 | `tensor_parallel_size` | `int` | `ParallelConfig.tensor_parallel_size` |
| 13 | `prefill_context_parallel_size` | `int` | `ParallelConfig.prefill_context_parallel_size` |
| 14 | `decode_context_parallel_size` | `int` | `ParallelConfig.decode_context_parallel_size` |
| 15 | `dcp_comm_backend` | `DCPCommBackend | None` | `ParallelConfig.dcp_comm_backend` |
| 16 | `dcp_q_replicate` | `bool | None` | `ParallelConfig.dcp_q_replicate` |
| 17 | `dcp_kv_cache_interleave_size` | `int` | `ParallelConfig.dcp_kv_cache_interleave_size` |
| 18 | `cp_kv_cache_interleave_size` | `int | None` | `None` |
| 19 | `data_parallel_size` | `int` | `ParallelConfig.data_parallel_size` |
| 20 | `data_parallel_rank` | `int | None` | `None` |
| 21 | `data_parallel_start_rank` | `int | None` | `None` |
| 22 | `data_parallel_size_local` | `int | None` | `None` |
| 23 | `data_parallel_address` | `str | None` | `None` |
| 24 | `data_parallel_rpc_port` | `int | None` | `None` |
| 25 | `data_parallel_hybrid_lb` | `bool` | `False` |
| 26 | `data_parallel_external_lb` | `bool` | `False` |
| 27 | `data_parallel_multi_port_external_lb` | `bool` | `False` |
| 28 | `data_parallel_backend` | `DataParallelBackend` | `ParallelConfig.data_parallel_backend` |
| 29 | `enable_expert_parallel` | `bool` | `ParallelConfig.enable_expert_parallel` |
| 30 | `enable_batch_sharded_sampling` | `bool | None` | `(` |
| 31 | `enable_ep_weight_filter` | `bool` | `ParallelConfig.enable_ep_weight_filter` |
| 32 | `moe_backend` | `MoEBackend` | `KernelConfig.moe_backend` |
| 33 | `linear_backend` | `LinearBackend` | `KernelConfig.linear_backend` |
| 34 | `all2all_backend` | `All2AllBackend` | `ParallelConfig.all2all_backend` |
| 35 | `enable_elastic_ep` | `bool` | `ParallelConfig.enable_elastic_ep` |
| 36 | `elastic_ep_max_dp_size` | `int` | `ParallelConfig.elastic_ep_max_dp_size` |
| 37 | `enable_dbo` | `bool` | `ParallelConfig.enable_dbo` |
| 38 | `dbo_decode_token_threshold` | `int` | `ParallelConfig.dbo_decode_token_threshold` |
| 39 | `dp_sync_interval` | `int` | `ParallelConfig.dp_sync_interval` |
| 40 | `dbo_prefill_token_threshold` | `int` | `ParallelConfig.dbo_prefill_token_threshold` |
| 41 | `disable_nccl_for_dp_synchronization` | `bool | None` | `(` |
| 42 | `eplb_config` | `EPLBConfig` | `<-ParallelConfig.eplb_config` |
| 43 | `expert_placement_strategy` | `ExpertPlacementStrategy` | `(` |

## 多模态（30）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `enable_prompt_embeds` | `bool` | `ModelConfig.enable_prompt_embeds` |
| 2 | `allowed_local_media_path` | `str` | `ModelConfig.allowed_local_media_path` |
| 3 | `allowed_media_domains` | `list[str] | None` | `ModelConfig.allowed_media_domains` |
| 4 | `language_model_only` | `bool` | `MultiModalConfig.language_model_only` |
| 5 | `limit_mm_per_prompt` | `dict[str, int | dict[str, i...` | `get_field(` |
| 6 | `enable_mm_embeds` | `bool` | `MultiModalConfig.enable_mm_embeds` |
| 7 | `interleave_mm_strings` | `bool` | `MultiModalConfig.interleave_mm_strings` |
| 8 | `mm_processor_kwargs` | `dict[str, Any] | None` | `MultiModalConfig.mm_processor_kwargs` |
| 9 | `mm_processor_cache_gb` | `float` | `MultiModalConfig.mm_processor_cache_gb` |
| 10 | `mm_processor_cache_type` | `MMCacheType | None` | `(` |
| 11 | `mm_hasher_algorithm` | `MMHasherAlgorithm` | `get_field(` |
| 12 | `mm_shm_cache_max_object_size_mb` | `int` | `(` |
| 13 | `mm_encoder_only` | `bool` | `MultiModalConfig.mm_encoder_only` |
| 14 | `mm_encoder_tp_mode` | `MMEncoderTPMode` | `MultiModalConfig.mm_encoder_tp_mode` |
| 15 | `mm_encoder_attn_backend` | `AttentionBackendEnum | str ...` | `(` |
| 16 | `mm_encoder_attn_dtype` | `str | None` | `MultiModalConfig.mm_encoder_attn_dtype` |
| 17 | `mm_encoder_fp8_scale_path` | `str | None` | `MultiModalConfig.mm_encoder_fp8_scale_path` |
| 18 | `mm_encoder_fp8_scale_save_path` | `str | None` | `(` |
| 19 | `mm_encoder_fp8_scale_save_margin` | `float` | `(` |
| 20 | `io_processor_plugin` | `str | None` | `None` |
| 21 | `renderer_num_workers` | `int` | `1` |
| 22 | `skip_mm_profiling` | `bool` | `MultiModalConfig.skip_mm_profiling` |
| 23 | `video_pruning_rate` | `float | None` | `MultiModalConfig.video_pruning_rate` |
| 24 | `video_pruning_method` | `str` | `MultiModalConfig.video_pruning_method` |
| 25 | `mm_tensor_ipc` | `MMTensorIPC` | `MultiModalConfig.mm_tensor_ipc` |
| 26 | `mm_processor_device` | `MMProcessorDevice` | `"auto"` |
| 27 | `mm_ipc_gpu_memory_gb` | `float` | `MultiModalConfig.mm_ipc_gpu_memory_gb` |
| 28 | `mm_device_do_normalize` | `bool | None` | `MultiModalConfig.mm_device_do_normalize` |
| 29 | `default_mm_loras` | `dict[str, str] | None` | `LoRAConfig.default_mm_loras` |
| 30 | `enable_mm_processor_stats` | `bool` | `ObservabilityConfig.enable_mm_processor_stats` |

## 内存与KV Cache（29）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `kv_cache_dtype` | `CacheDType` | `CacheConfig.cache_dtype` |
| 2 | `block_size` | `int | None` | `None` |
| 3 | `enable_prefix_caching` | `bool | None` | `None` |
| 4 | `prefix_caching_hash_algo` | `PrefixCachingHashAlgo` | `(` |
| 5 | `prefix_cache_retention_interval` | `int | None` | `get_field(` |
| 6 | `offload_backend` | `str` | `OffloadConfig.offload_backend` |
| 7 | `cpu_offload_gb` | `float` | `UVAOffloadConfig.cpu_offload_gb` |
| 8 | `cpu_offload_params` | `set[str]` | `<-UVAOffloadConfig.cpu_offload_params` |
| 9 | `offload_group_size` | `int` | `PrefetchOffloadConfig.offload_group_size` |
| 10 | `offload_num_in_group` | `int` | `PrefetchOffloadConfig.offload_num_in_group` |
| 11 | `offload_prefetch_step` | `int` | `PrefetchOffloadConfig.offload_prefetch_step` |
| 12 | `offload_params` | `set[str]` | `<-PrefetchOffloadConfig.offload_params` |
| 13 | `gpu_memory_utilization` | `float` | `CacheConfig.gpu_memory_utilization` |
| 14 | `kv_cache_memory_bytes` | `int | None` | `CacheConfig.kv_cache_memory_bytes` |
| 15 | `num_gpu_blocks_override` | `int | None` | `CacheConfig.num_gpu_blocks_override` |
| 16 | `kv_cache_metrics` | `bool` | `ObservabilityConfig.kv_cache_metrics` |
| 17 | `kv_cache_metrics_sample` | `float` | `get_field(` |
| 18 | `enable_sleep_mode` | `bool` | `ModelConfig.enable_sleep_mode` |
| 19 | `enable_cumem_allocator` | `bool` | `ModelConfig.enable_cumem_allocator` |
| 20 | `kv_cache_dtype_skip_layers` | `list[str]` | `get_field(` |
| 21 | `mamba_cache_dtype` | `MambaDType` | `CacheConfig.mamba_cache_dtype` |
| 22 | `mamba_block_size` | `int | None` | `<-CacheConfig.mamba_block_size` |
| 23 | `prefix_match_unit` | `int | None` | `<-CacheConfig.prefix_match_unit` |
| 24 | `mamba_cache_mode` | `MambaCacheMode` | `CacheConfig.mamba_cache_mode` |
| 25 | `replayssm_buffer_len` | `int` | `CacheConfig.replayssm_buffer_len` |
| 26 | `use_replayssm` | `bool` | `CacheConfig.use_replayssm` |
| 27 | `mamba_cache_philox_rounds` | `int` | `MambaConfig.stochastic_rounding_philox_rounds` |
| 28 | `kv_offloading_size` | `float | None` | `CacheConfig.kv_offloading_size` |
| 29 | `kv_offloading_backend` | `KVOffloadingBackend` | `CacheConfig.kv_offloading_backend` |

## 模型加载（28）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `model` | `str` | `ModelConfig.model` |
| 2 | `model_weights` | `str` | `ModelConfig.model_weights` |
| 3 | `served_model_name` | `str | list[str] | None` | `ModelConfig.served_model_name` |
| 4 | `tokenizer` | `str | None` | `ModelConfig.tokenizer` |
| 5 | `hf_config_path` | `str | None` | `ModelConfig.hf_config_path` |
| 6 | `convert` | `ConvertOption` | `ModelConfig.convert` |
| 7 | `skip_tokenizer_init` | `bool` | `ModelConfig.skip_tokenizer_init` |
| 8 | `tokenizer_mode` | `TokenizerMode | str` | `ModelConfig.tokenizer_mode` |
| 9 | `trust_remote_code` | `bool` | `ModelConfig.trust_remote_code` |
| 10 | `download_dir` | `str | None` | `LoadConfig.download_dir` |
| 11 | `safetensors_load_strategy` | `SafetensorsLoadStrategy | None` | `(` |
| 12 | `safetensors_prefetch_num_threads` | `int` | `LoadConfig.safetensors_prefetch_num_threads` |
| 13 | `safetensors_prefetch_block_size` | `int` | `LoadConfig.safetensors_prefetch_block_size` |
| 14 | `load_format` | `str | LoadFormats` | `LoadConfig.load_format` |
| 15 | `config_format` | `str` | `ModelConfig.config_format` |
| 16 | `dtype` | `ModelDType` | `ModelConfig.dtype` |
| 17 | `seed` | `int` | `ModelConfig.seed` |
| 18 | `revision` | `str | None` | `ModelConfig.revision` |
| 19 | `code_revision` | `str | None` | `ModelConfig.code_revision` |
| 20 | `hf_token` | `bool | str | None` | `ModelConfig.hf_token` |
| 21 | `hf_overrides` | `HfOverrides` | `<-ModelConfig.hf_overrides` |
| 22 | `model_class_overrides` | `dict[str, str]` | `get_field(` |
| 23 | `tokenizer_revision` | `str | None` | `ModelConfig.tokenizer_revision` |
| 24 | `model_loader_extra_config` | `dict` | `<-LoadConfig.model_loader_extra_config` |
| 25 | `ignore_patterns` | `str | list[str]` | `<-LoadConfig.ignore_patterns` |
| 26 | `model_impl` | `str` | `ModelConfig.model_impl` |
| 27 | `use_tqdm_on_load` | `bool` | `LoadConfig.use_tqdm_on_load` |
| 28 | `pt_load_map_location` | `str | dict[str, str]` | `LoadConfig.pt_load_map_location` |

## 调度器（17）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `max_model_len` | `int` | `ModelConfig.max_model_len` |
| 2 | `max_num_batched_tokens` | `int | None` | `None` |
| 3 | `max_num_scheduled_tokens` | `int | None` | `None` |
| 4 | `long_prefill_token_threshold` | `int` | `SchedulerConfig.long_prefill_token_threshold` |
| 5 | `max_num_seqs` | `int | None` | `None` |
| 6 | `max_num_queued_reqs` | `int | None` | `None` |
| 7 | `max_num_queued_tokens` | `int | None` | `None` |
| 8 | `enable_chunked_prefill` | `bool | None` | `None` |
| 9 | `scheduler_reserve_full_isl` | `bool` | `SchedulerConfig.scheduler_reserve_full_isl` |
| 10 | `prefill_schedule_interval` | `int` | `SchedulerConfig.prefill_schedule_interval` |
| 11 | `watermark` | `float` | `SchedulerConfig.watermark` |
| 12 | `watermark_config` | `dict[str, Any] | None` | `None` |
| 13 | `scheduling_policy` | `SchedulerPolicy` | `SchedulerConfig.policy` |
| 14 | `scheduler_cls` | `str | type[object] | None` | `SchedulerConfig.scheduler_cls` |
| 15 | `async_scheduling` | `bool | None` | `SchedulerConfig.async_scheduling` |
| 16 | `stream_interval` | `int` | `SchedulerConfig.stream_interval` |
| 17 | `performance_mode` | `PerformanceMode` | `VllmConfig.performance_mode` |

## 计算内核与加速（16）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `cudagraph_capture_sizes` | `list[int] | None` | `(` |
| 2 | `ir_op_priority` | `IrOpPriorityConfig` | `<-KernelConfig.ir_op_priority` |
| 3 | `disable_sliding_window` | `bool` | `ModelConfig.disable_sliding_window` |
| 4 | `disable_cascade_attn` | `bool` | `ModelConfig.disable_cascade_attn` |
| 5 | `enforce_eager` | `bool` | `ModelConfig.enforce_eager` |
| 6 | `disable_custom_all_reduce` | `bool` | `ParallelConfig.disable_custom_all_reduce` |
| 7 | `cudagraph_metrics` | `bool` | `ObservabilityConfig.cudagraph_metrics` |
| 8 | `compilation_config` | `CompilationConfig` | `<-VllmConfig.compilation_config` |
| 9 | `attention_config` | `AttentionConfig` | `<-VllmConfig.attention_config` |
| 10 | `kernel_config` | `KernelConfig` | `<-VllmConfig.kernel_config` |
| 11 | `enable_flashinfer_autotune` | `bool` | `get_field(` |
| 12 | `attention_backend` | `AttentionBackendEnum | None` | `AttentionConfig.backend` |
| 13 | `optimization_level` | `OptimizationLevel` | `VllmConfig.optimization_level` |
| 14 | `gdn_prefill_backend` | `Literal["flashinfer", "trit...` | `None` |
| 15 | `kda_prefill_backend` | `(` | `` |
| 16 | `kda_decode_backend` | `Literal["auto", "native", "...` | `None` |

## 生成与采样（15）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `enable_return_routed_experts` | `bool` | `ModelConfig.enable_return_routed_experts` |
| 2 | `return_sampling_mask` | `bool` | `ModelConfig.return_sampling_mask` |
| 3 | `runner` | `RunnerOption` | `ModelConfig.runner` |
| 4 | `max_logprobs` | `int` | `ModelConfig.max_logprobs` |
| 5 | `logprobs_mode` | `LogprobsMode` | `ModelConfig.logprobs_mode` |
| 6 | `use_fp64_gumbel` | `bool` | `ModelConfig.use_fp64_gumbel` |
| 7 | `structured_outputs_config` | `StructuredOutputsConfig` | `get_field(` |
| 8 | `reasoning_parser` | `str` | `StructuredOutputsConfig.reasoning_parser` |
| 9 | `reasoning_parser_plugin` | `str | None` | `None` |
| 10 | `diffusion_config` | `dict[str, Any] | None` | `None` |
| 11 | `pooler_config` | `PoolerConfig | None` | `ModelConfig.pooler_config` |
| 12 | `reasoning_config` | `ReasoningConfig` | `<-VllmConfig.reasoning_config` |
| 13 | `generation_config` | `str` | `ModelConfig.generation_config` |
| 14 | `override_generation_config` | `dict[str, Any]` | `get_field(` |
| 15 | `logits_processors` | `list[str | type[LogitsProce...` | `(` |

## 其他（15）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `max_cudagraph_capture_size` | `int | None` | `get_field(` |
| 2 | `cpu_distributed_timeout_seconds` | `int | None` | `(` |
| 3 | `sparse_indexer_topk_backend` | `SparseIndexerTopkBackend` | `(` |
| 4 | `ubatch_size` | `int` | `ParallelConfig.ubatch_size` |
| 5 | `enable_eplb` | `bool` | `ParallelConfig.enable_eplb` |
| 6 | `media_io_kwargs` | `dict[str, dict[str, Any]]` | `get_field(` |
| 7 | `disable_chunked_mm_input` | `bool` | `SchedulerConfig.disable_chunked_mm_input` |
| 8 | `disable_hybrid_kv_cache_manager` | `bool | None` | `(` |
| 9 | `mamba_config` | `MambaConfig` | `<-VllmConfig.mamba_config` |
| 10 | `mamba_ssm_cache_dtype` | `MambaDType` | `CacheConfig.mamba_ssm_cache_dtype` |
| 11 | `enable_mamba_fine_grained_prefix_cache` | `bool` | `(` |
| 12 | `mamba_backend` | `MambaBackendEnum` | `MambaBackendEnum.TRITON` |
| 13 | `mamba_ssu_algorithm` | `MambaSSUAlgorithm | None` | `None` |
| 14 | `enable_mamba_cache_stochastic_rounding` | `bool` | `(` |
| 15 | `kv_sharing_fast_prefill` | `bool` | `CacheConfig.kv_sharing_fast_prefill` |

## 观测与诊断（13）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `enable_trace_replay` | `bool` | `ModelConfig.enable_trace_replay` |
| 2 | `disable_log_stats` | `bool` | `False` |
| 3 | `aggregate_engine_logging` | `bool` | `False` |
| 4 | `show_hidden_metrics_for_version` | `str | None` | `(` |
| 5 | `otlp_traces_endpoint` | `str | None` | `ObservabilityConfig.otlp_traces_endpoint` |
| 6 | `collect_detailed_traces` | `list[DetailedTraceModules] ...` | `(` |
| 7 | `per_request_spec_decode_metrics` | `Literal["none", "summary", ...` | `(` |
| 8 | `enable_layerwise_nvtx_tracing` | `bool` | `(` |
| 9 | `enable_mfu_metrics` | `bool` | `ObservabilityConfig.enable_mfu_metrics` |
| 10 | `enable_logging_iteration_details` | `bool` | `(` |
| 11 | `jit_monitor_mode` | `Literal["warn", "error"]` | `ObservabilityConfig.jit_monitor_mode` |
| 12 | `jit_monitor_verbose` | `bool` | `ObservabilityConfig.jit_monitor_verbose` |
| 13 | `profiler_config` | `ProfilerConfig` | `<-VllmConfig.profiler_config` |

## 基础设施（12）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `max_parallel_loading_workers` | `int | None` | `(` |
| 2 | `ray_workers_use_nsight` | `bool` | `ParallelConfig.ray_workers_use_nsight` |
| 3 | `engram_config` | `EngramConfig | None` | `VllmConfig.engram_config` |
| 4 | `worker_cls` | `str` | `ParallelConfig.worker_cls` |
| 5 | `worker_extension_cls` | `str` | `ParallelConfig.worker_extension_cls` |
| 6 | `enable_nccl_comm_suspend` | `bool` | `ModelConfig.enable_nccl_comm_suspend` |
| 7 | `additional_config` | `dict[str, Any]` | `<-VllmConfig.additional_config` |
| 8 | `fault_tolerance_config` | `FaultToleranceConfig` | `get_field(` |
| 9 | `enable_fault_tolerance` | `bool` | `ParallelConfig.enable_fault_tolerance` |
| 10 | `tokens_only` | `bool` | `False` |
| 11 | `shutdown_timeout` | `int` | `0` |
| 12 | `fail_on_environ_validation` | `bool` | `False` |

## LoRA（11）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `enable_lora` | `bool` | `False` |
| 2 | `max_loras` | `int` | `LoRAConfig.max_loras` |
| 3 | `max_lora_rank` | `MaxLoRARanks` | `LoRAConfig.max_lora_rank` |
| 4 | `fully_sharded_loras` | `bool` | `LoRAConfig.fully_sharded_loras` |
| 5 | `max_cpu_loras` | `int | None` | `LoRAConfig.max_cpu_loras` |
| 6 | `lora_dtype` | `str | torch.dtype | None` | `LoRAConfig.lora_dtype` |
| 7 | `lora_target_modules` | `list[str] | None` | `LoRAConfig.target_modules` |
| 8 | `enable_tower_connector_lora` | `bool` | `LoRAConfig.enable_tower_connector_lora` |
| 9 | `specialize_active_lora` | `bool` | `LoRAConfig.specialize_active_lora` |
| 10 | `enable_mixed_moe_lora_format` | `bool` | `LoRAConfig.enable_mixed_moe_lora_format` |
| 11 | `enable_moe_shared_loras` | `bool` | `LoRAConfig.enable_moe_shared_loras` |

## PD分离/KV传输（5）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `kv_transfer_config` | `KVTransferConfig | None` | `None` |
| 2 | `kv_events_config` | `KVEventsConfig | None` | `None` |
| 3 | `ec_transfer_config` | `ECTransferConfig | None` | `None` |
| 4 | `ec_manager_config` | `EncoderCacheManagerConfig` | `get_field(` |
| 5 | `weight_transfer_config` | `WeightTransferConfig | None` | `get_field(` |

## 投机解码（4）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `speculative_config` | `dict[str, Any] | None` | `None` |
| 2 | `spec_method` | `str | None` | `None` |
| 3 | `spec_model` | `str | None` | `None` |
| 4 | `spec_tokens` | `int | None` | `None` |

## 量化与精度（3）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `quantization` | `QuantizationMethods | str |...` | `ModelConfig.quantization` |
| 2 | `quantization_config` | `"dict[str, Any] | Quantizat...` | `None` |
| 3 | `allow_deprecated_quantization` | `bool` | `ModelConfig.allow_deprecated_quantization` |

## 内部/下划线（2）

| # | 参数 | 类型 | 默认值 |
|---|---|---|---|
| 1 | `_api_process_count` | `int` | `ParallelConfig._api_process_count` |
| 2 | `_api_process_rank` | `int` | `ParallelConfig._api_process_rank` |

