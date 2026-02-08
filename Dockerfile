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

WORKDIR /workspace
COPY . .

RUN cmake -S . -B build -G Ninja -DAI_TRADE_USE_BEAST_WEBSOCKET=ON && \
    cmake --build build -j"$(nproc)" && \
    ctest --test-dir build --output-on-failure

FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      libcurl4 \
      libssl3 \
      libboost-system1.83.0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=build /workspace/build/trade_bot /app/trade_bot
COPY --from=build /workspace/config /app/config

RUN mkdir -p /app/data

ENTRYPOINT ["/app/trade_bot"]
CMD ["--config=config/bybit.replay.yaml"]
