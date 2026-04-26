# Prometheus 监控系统详解

> 官方文档主页：https://prometheus.io/docs/introduction/overview/

---

## 目录

1. [Prometheus 是什么](#1-prometheus-是什么)
2. [Pull 模型 vs Push 模型](#2-pull-模型-vs-push-模型)
3. [数据模型：时序、标签、样本](#3-数据模型时序标签样本)
4. [Metric 类型](#4-metric-类型)
5. [Prometheus 文本格式（/metrics 端点）](#5-prometheus-文本格式metrics-端点)
6. [prometheus.yml 配置逐字段解析](#6-prometheusyml-配置逐字段解析)
7. [TSDB 时序数据库与持久化](#7-tsdb-时序数据库与持久化)
8. [PromQL 速查](#8-promql-速查)
9. [本项目架构说明](#9-本项目架构说明)
10. [官方文档索引](#10-官方文档索引)

---

## 1. Prometheus 是什么

Prometheus 是一个**开源的系统监控和告警工具包**，由 SoundCloud 于 2012 年开发，2016 年成为 CNCF（云原生计算基金会）的第二个毕业项目（第一个是 Kubernetes）。

它的核心职责有两个：
1. **定期从被监控目标拉取（scrape）指标数据**，并存入内置的时序数据库（TSDB）
2. **对存储的数据进行查询和告警规则评估**，支持通过 Alertmanager 发出告警通知

Prometheus **不是日志系统**（那是 Loki 做的事），也**不是链路追踪系统**（那是 Jaeger/Tempo 做的事）。它专注于**数值型时序指标**（metrics）。

---

## 2. Pull 模型 vs Push 模型

### Pull 模型（Prometheus 采用）

```
被监控应用                    Prometheus
┌─────────────┐              ┌────────────────┐
│  /metrics   │◄─── HTTP ────│  scraper       │
│  端点       │   GET 请求   │  每隔 N 秒拉取  │
└─────────────┘              └────────────────┘
```

- Prometheus **主动**定期访问应用暴露的 `/metrics` HTTP 端点
- 应用只需"被动"维护一个 HTTP 端点，不需要知道 Prometheus 在哪里
- **优点**：
  - 控制权在 Prometheus，抓取时间点一致，便于对齐时序
  - 可以通过访问 `/metrics` 手动验证应用暴露了什么数据
  - 应用宕机时 Prometheus 立即知道（因为抓取失败）
- **缺点**：
  - 短生命周期任务（如 batch job）可能在被抓取前就结束了
  - 解决方案：使用 Pushgateway，批处理任务完成后主动推送到 Pushgateway，Prometheus 再从 Pushgateway 拉取

### Push 模型（如 InfluxDB、Graphite、StatsD）

```
被监控应用                    监控服务
┌─────────────┐              ┌────────────────┐
│  客户端     │──── UDP/TCP ─►│  接收端        │
│             │   主动推送   │                │
└─────────────┘              └────────────────┘
```

- 应用主动将数据推送给监控服务端
- 需要在应用侧配置监控服务的地址

---

## 3. 数据模型：时序、标签、样本

### 时序（Time Series）

Prometheus 中每一条时序由**指标名**加**一组标签键值对**唯一确定：

```
<metric_name>{<label_name>=<label_value>, ...}
```

例子：
```
trading_price{job="trading", instance="trading:8000"}
http_requests_total{job="api", method="GET", status="200"}
```

同一指标名，不同标签组合 → 不同的时序（在存储中独立存储）

### 样本（Sample）

每次抓取，Prometheus 为每条时序记录一个**样本**，由两部分组成：
- `timestamp`：Unix 毫秒时间戳（Prometheus 自动填写）
- `value`：64-bit 浮点数

```
trading_price{job="trading"} 102.4512  1745000000000
trading_price{job="trading"} 102.6831  1745000005000   ← 5秒后
```

### 标签来源

| 标签 | 来源 |
|------|------|
| `job` | prometheus.yml 中 `job_name` 自动附加 |
| `instance` | 从 target 地址（host:port）自动附加 |
| 其他标签 | 应用自己在 `/metrics` 中暴露的 label，或通过 `relabel_configs` 添加 |

---

## 4. Metric 类型

> 官方文档：https://prometheus.io/docs/concepts/metric_types/

### Counter（计数器）

- **单调递增**，只能增加，不能减少（重启后归零）
- 适合：请求总数、错误总数、成交次数
- **命名惯例**：以 `_total` 结尾
- **查询技巧**：用 `rate()` 或 `increase()` 计算增长速率

```
trading_trades_total        → 本次启动后的累计成交笔数
rate(trading_trades_total[1m])  → 每秒成交速率（1分钟窗口的平均值）
```

### Gauge（仪表盘）

- 可以任意升降的数值，反映"当前状态"
- 适合：当前价格、持仓数量、内存使用量、CPU 温度
- **直接使用**，无需 `rate()`

```
trading_price     → 当前资产价格
trading_position  → 当前持仓手数
trading_pnl       → 当前未实现盈亏
```

### Histogram（直方图）

- 将观测值按预设的 bucket（桶）分类计数，同时记录总和与计数
- 适合：请求耗时、响应体积分布
- 自动生成三个指标：`_bucket`、`_sum`、`_count`
- 支持用 `histogram_quantile()` 计算近似百分位数

### Summary（摘要）

- 类似 Histogram，但在客户端侧直接计算精确分位数
- 缺点：不支持跨实例聚合，通常优先选 Histogram

---

## 5. Prometheus 文本格式（/metrics 端点）

应用暴露的 `/metrics` 端点返回纯文本，格式如下：

```
# HELP trading_price 当前模拟资产价格（随机游走）
# TYPE trading_price gauge
trading_price 102.4512

# HELP trading_trades_total 累计成交笔数
# TYPE trading_trades_total counter
trading_trades_total 7.0

# HELP process_cpu_seconds_total Total user and system CPU time spent in seconds.
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 0.12
```

- `# HELP`：指标的描述文字
- `# TYPE`：指标类型声明（counter/gauge/histogram/summary/untyped）
- `python:3.13 + prometheus_client` 还会**自动暴露进程级指标**，如 CPU、内存、文件描述符数量，无需额外代码

---

## 6. prometheus.yml 配置逐字段解析

> 完整配置参考：https://prometheus.io/docs/prometheus/latest/configuration/configuration/

```yaml
global:
  scrape_interval: 5s        # 全局抓取间隔，所有 job 的默认值
  scrape_timeout: 4s         # 单次抓取超时，必须 ≤ scrape_interval
  evaluation_interval: 15s   # 告警/recording rules 的评估频率

scrape_configs:
  - job_name: "trading"      # job 名称 → 自动成为 {job="trading"} 标签
    static_configs:
      - targets: ["trading:8000"]
        # "trading" 是 Docker Compose 服务名，由 Docker 内嵌 DNS 解析
        # Prometheus 访问：http://trading:8000/metrics
```

### 本项目未使用但常用的字段

| 字段 | 作用 |
|------|------|
| `metrics_path` | 修改抓取路径，默认 `/metrics` |
| `scheme` | `http`（默认）或 `https` |
| `basic_auth` | 目标需要 HTTP Basic Auth 时配置 |
| `tls_config` | TLS 客户端证书配置 |
| `relabel_configs` | 在抓取前对标签进行重写、过滤 |
| `rule_files` | 加载告警/recording 规则文件 |
| `alerting` | 配置 Alertmanager 地址 |

---

## 7. TSDB 时序数据库与持久化

Prometheus 内置了自己的时序数据库（TSDB），数据存储在 `--storage.tsdb.path` 指定的目录（默认 `/prometheus`）。

### 存储结构

```
/prometheus/
├── 01ABCDEF.../        ← 已封存的 block（2小时一个）
│   ├── chunks/         ← 压缩后的样本数据
│   ├── index           ← 倒排索引（按标签快速查找时序）
│   └── meta.json       ← block 元信息
├── chunks_head/        ← 当前写入中的内存 block（WAL 保护）
├── wal/                ← Write-Ahead Log，防止宕机数据丢失
└── lock
```

### 为什么需要 Named Volume

在本项目的 `docker-compose.yml` 中：

```yaml
volumes:
  - prometheus_data:/prometheus
```

- **不持久化**：容器删除（`docker compose down`）后，`/prometheus` 目录消失，所有历史数据丢失
- **持久化**：使用 named volume `prometheus_data`，数据存储在 Docker 管理的卷中，容器删除后卷依然存在，重新 `up` 后历史数据恢复

默认数据保留时间为 **15 天**（`--storage.tsdb.retention.time=15d`），可通过 Prometheus 启动参数调整。

---

## 8. PromQL 速查

> 官方文档：https://prometheus.io/docs/prometheus/latest/querying/basics/

| 场景 | PromQL |
|------|--------|
| 查询当前价格 | `trading_price` |
| 查询当前持仓 | `trading_position` |
| 查询当前盈亏 | `trading_pnl` |
| 累计成交次数 | `trading_trades_total` |
| 过去1分钟成交速率 | `rate(trading_trades_total[1m])` |
| 5分钟内成交增量 | `increase(trading_trades_total[5m])` |
| 查看所有 trading job 的指标 | `{job="trading"}` |

---

## 9. 本项目架构说明

```
┌─────────────────────────────────────────────────────────┐
│  Docker Compose 私有网络（bridge）                        │
│                                                          │
│  ┌──────────────┐    HTTP GET /metrics    ┌───────────┐ │
│  │   trading    │◄────────────────────────│prometheus │ │
│  │  :8000       │      每 5 秒一次        │  :9090    │ │
│  └──────────────┘                         └─────┬─────┘ │
│                                                 │       │
│                                          HTTP /api/v1   │
│                                          PromQL 查询     │
│                                                 │       │
│                                          ┌──────▼─────┐ │
│                                          │  grafana   │ │
│                                          │  :3000     │ │
│                                          └────────────┘ │
└─────────────────────────────────────────────────────────┘
         ▲                        ▲
   宿主机 :8000             宿主机 :3000 / :9090
   (验证指标用)              (访问 UI 用)
```

---

## 10. 官方文档索引

| 主题 | 链接 |
|------|------|
| 概念介绍 | https://prometheus.io/docs/introduction/overview/ |
| 数据模型 | https://prometheus.io/docs/concepts/data_model/ |
| Metric 类型 | https://prometheus.io/docs/concepts/metric_types/ |
| prometheus.yml 配置参考 | https://prometheus.io/docs/prometheus/latest/configuration/configuration/ |
| PromQL 查询基础 | https://prometheus.io/docs/prometheus/latest/querying/basics/ |
| PromQL 函数列表 | https://prometheus.io/docs/prometheus/latest/querying/functions/ |
| TSDB 存储原理 | https://prometheus.io/docs/prometheus/latest/storage/ |
| Python client 库（prometheus_client） | https://github.com/prometheus/client_python |
| Pushgateway（批处理场景） | https://prometheus.io/docs/instrumenting/pushing/ |
