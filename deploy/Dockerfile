ARG VLLM_BASE_IMAGE=vllm/vllm-openai:v0.29.0
FROM ${VLLM_BASE_IMAGE}

WORKDIR /opt/vllm-observer
COPY pyproject.toml ./
COPY src ./src
COPY bootstrap/sitecustomize.py ./bootstrap/sitecustomize.py
# Use the same Python environment as the base image's vllm executable.
RUN python3 -m pip install --no-cache-dir '.[langfuse]' \
    && python3 -m pip check

# At runtime set PYTHONPATH=/opt/vllm-observer/bootstrap and OBSERVER_ENABLED=1.
# Python's normal site initialization loads the optional instrumentation.
ENTRYPOINT ["vllm", "serve"]
