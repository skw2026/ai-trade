# syntax=docker/dockerfile:1.7

ARG BUILD_REVISION=unknown

FROM ubuntu:24.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN cat > /usr/local/bin/apt-install <<'SCRIPT' && \
    chmod +x /usr/local/bin/apt-install
#!/usr/bin/env bash
set -euo pipefail

attempts=5
for attempt in $(seq 1 "${attempts}"); do
  rm -rf /var/lib/apt/lists/*
  apt-get update \
    -o Acquire::Retries=3 \
    -o Acquire::http::No-Cache=true \
    -o Acquire::https::No-Cache=true
  if apt-get install -y --no-install-recommends "$@"; then
    rm -rf /var/lib/apt/lists/*
    exit 0
  fi
  if [[ "${attempt}" -eq "${attempts}" ]]; then
    exit 1
  fi
  echo "apt install failed on attempt ${attempt}/${attempts}; retrying..." >&2
  sleep $((attempt * 5))
done
SCRIPT

RUN /usr/local/bin/apt-install \
      build-essential \
      cmake \
      ninja-build \
      pkg-config \
      ca-certificates \
      curl \
      git \
      libcurl4-openssl-dev \
      libssl-dev \
      libboost-all-dev \
      python3 \
      python3-numpy

# [新增] 下载 CatBoost C++ 推理库 (自动适配 amd64/arm64)
RUN mkdir -p /usr/local/include/model_interface && \
    curl -L https://raw.githubusercontent.com/catboost/catboost/v1.2.7/catboost/libs/model_interface/c_api.h -o /usr/local/include/model_interface/c_api.h && \
    if [ "$(uname -m)" = "x86_64" ]; then \
      curl -L https://github.com/catboost/catboost/releases/download/v1.2.7/libcatboostmodel-linux-x86_64-1.2.7.so -o /usr/local/lib/libcatboostmodel.so; \
    elif [ "$(uname -m)" = "aarch64" ]; then \
      # CatBoost 官方 Release v1.2.7 包含 aarch64 支持
      curl -L https://github.com/catboost/catboost/releases/download/v1.2.7/libcatboostmodel-linux-aarch64-1.2.7.so -o /usr/local/lib/libcatboostmodel.so; \
    else \
      echo "Unsupported architecture: $(uname -m)" && exit 1; \
    fi && \
    chmod +x /usr/local/lib/libcatboostmodel.so

WORKDIR /workspace
COPY . .

# [修改] 增加 -DAI_TRADE_ENABLE_CATBOOST=ON 开关
RUN cmake -S . -B build -G Ninja -DAI_TRADE_USE_BEAST_WEBSOCKET=ON -DAI_TRADE_ENABLE_CATBOOST=ON && \
    cmake --build build -j"$(nproc)" && \
    ctest --test-dir build --output-on-failure

FROM ubuntu:24.04 AS runtime-base

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=build /usr/local/bin/apt-install /usr/local/bin/apt-install

RUN /usr/local/bin/apt-install \
      ca-certificates \
      libcurl4 \
      libssl3 \
      libboost-system1.83.0 \
      python3

WORKDIR /app

# [新增] 将 CatBoost 动态库复制到运行时镜像，并更新动态链接库缓存
COPY --from=build /usr/local/lib/libcatboostmodel.so /usr/local/lib/
RUN ldconfig

RUN mkdir -p /app/data

# Keep large research dependencies below mutable application payload layers.
# Registry inline cache then preserves this ancestry across source-only builds.
FROM runtime-base AS research-dependencies
COPY --from=build /workspace/tools/requirements-research.txt /tmp/requirements-research.txt
RUN /usr/local/bin/apt-install python3-pip binutils && \
    pip3 install --no-cache-dir --no-compile --no-deps --break-system-packages \
      -r /tmp/requirements-research.txt && \
    strip --strip-unneeded \
      /usr/local/lib/python3.12/dist-packages/catboost/_catboost.so && \
    apt-get purge -y --auto-remove python3-pip binutils && \
    apt-get clean && \
    rm -f /tmp/requirements-research.txt && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* && \
    find /usr/local/lib/python3.12/dist-packages \
      -type d \( -name __pycache__ -o -name test -o -name tests \) \
      -prune -print0 \
      | xargs -0 -r rm -rf && \
    find /usr/local/lib/python3.12/dist-packages \
      -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete && \
    python3 -c 'import catboost, numpy, pandas, scipy, websockets; print(catboost.__version__, numpy.__version__, pandas.__version__, scipy.__version__, websockets.__version__)'

FROM runtime-base AS runtime

ARG BUILD_REVISION
LABEL org.opencontainers.image.revision="${BUILD_REVISION}"

COPY --from=build /workspace/build/trade_bot /app/trade_bot
COPY --from=build /workspace/config /app/config
# [新增] 将运维和工具脚本打包进镜像，确保 CD 部署后直接可用
COPY --from=build /workspace/ops /app/ops
COPY --from=build /workspace/tools /app/tools

FROM research-dependencies AS research

ARG BUILD_REVISION
LABEL org.opencontainers.image.revision="${BUILD_REVISION}"

COPY --from=build /workspace/build/trade_bot /app/trade_bot
COPY --from=build /workspace/config /app/config
COPY --from=build /workspace/ops /app/ops
COPY --from=build /workspace/tools /app/tools

ENTRYPOINT ["python3", "/app/tools/integrator_train.py"]
CMD ["--help"]

# [修改] 默认目标恢复为 runtime，确保主服务轻量
FROM runtime
ENTRYPOINT ["/app/trade_bot"]
CMD ["--config=config/bybit.replay.yaml"]
