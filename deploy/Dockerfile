ARG VLLM_BASE_IMAGE=vllm/vllm-openai:v0.29.0
FROM ${VLLM_BASE_IMAGE}

WORKDIR /opt/vllm-observer
COPY pyproject.toml ./
COPY src ./src
COPY bootstrap/sitecustomize.py ./bootstrap/sitecustomize.py
# Use the same Python environment as the base image's vllm executable.
# pip check is omitted: the CUDA 13 base image ships an upstream
# nvidia-nccl-cu13 metadata conflict unrelated to this package.
RUN python3 -m pip install --no-cache-dir '.[langfuse]'

# At runtime set PYTHONPATH=/opt/vllm-observer/bootstrap and OBSERVER_ENABLED=1.
# Python's normal site initialization loads the optional instrumentation.
ENTRYPOINT ["vllm", "serve"]
