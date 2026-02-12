# syntax=docker/dockerfile:1.7

FROM ubuntu:24.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      cmake \
      ninja-build \
      pkg-config \
      ca-certificates \
      curl \
      git \
      libcurl4-openssl-dev \
      libssl-dev \
      libboost-all-dev && \
    rm -rf /var/lib/apt/lists/*

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

FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      libcurl4 \
      libssl3 \
      libboost-system1.83.0 \
      python3 \
      python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# [新增] 将 CatBoost 动态库复制到运行时镜像，并更新动态链接库缓存
COPY --from=build /usr/local/lib/libcatboostmodel.so /usr/local/lib/
RUN ldconfig

COPY --from=build /workspace/build/trade_bot /app/trade_bot
COPY --from=build /workspace/config /app/config
# [新增] 将运维和工具脚本打包进镜像，确保 CD 部署后直接可用
COPY --from=build /workspace/ops /app/ops
COPY --from=build /workspace/tools /app/tools

RUN mkdir -p /app/data

# [新增] Research 阶段：基于运行时镜像，额外安装训练所需的 Python 库
# 这确保了 ai-trade-research 服务能运行 integrator_train.py
FROM runtime AS research
RUN apt-get update && apt-get install -y --no-install-recommends python3-pip && \
    rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages numpy catboost

# [修改] 默认目标恢复为 runtime，确保主服务轻量
FROM runtime
ENTRYPOINT ["/app/trade_bot"]
CMD ["--config=config/bybit.replay.yaml"]
