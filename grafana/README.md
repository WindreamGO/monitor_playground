# Grafana 可视化系统详解

> 官方文档主页：https://grafana.com/docs/grafana/latest/

---

## 目录

1. [Grafana 是什么（与 Prometheus 的关系）](#1-grafana-是什么与-prometheus-的关系)
2. [Provisioning 机制总览](#2-provisioning-机制总览)
3. [Datasource Provisioning 详解](#3-datasource-provisioning-详解)
4. [Dashboard Provisioning 详解（两阶段）](#4-dashboard-provisioning-详解两阶段)
5. [proxy 访问模式的请求链路](#5-proxy-访问模式的请求链路)
6. [allowUiUpdates 的行为说明](#6-allowuiupdates-的行为说明)
7. [Named Volumes 持久化说明](#7-named-volumes-持久化说明)
8. [本项目文件挂载关系图](#8-本项目文件挂载关系图)
9. [官方文档索引](#9-官方文档索引)

---

## 1. Grafana 是什么（与 Prometheus 的关系）

Grafana 是一个**开源的可视化与分析平台**。它**不存储数据**，而是充当一个"查询代理 + 渲染引擎"：

```
Prometheus（存储数据）  ←→  Grafana（查询 + 可视化）  ←→  用户浏览器（展示）
```

类比：
- Prometheus 是数据库（存时序数据）
- Grafana 是 BI 工具（查询并展示数据）
- 就像 MySQL + Metabase 的关系

Grafana 支持几十种数据源（datasource），包括 Prometheus、InfluxDB、Elasticsearch、PostgreSQL、Loki（日志）、Tempo（链路追踪）等。切换数据源不需要改变 Grafana 自身，只需配置不同的数据源插件。

---

## 2. Provisioning 机制总览

> 官方文档：https://grafana.com/docs/grafana/latest/administration/provisioning/

### 什么是 Provisioning

Provisioning（配置预置）是 Grafana 提供的一种**声明式配置**机制：将原本需要在 UI 中手动点击完成的操作（添加数据源、导入 dashboard）改为**读取 YAML/JSON 文件自动完成**。

### Provisioning 的触发时机

```
docker compose up
      │
      ▼
Grafana 启动
      │
      ├─► 扫描 /etc/grafana/provisioning/datasources/  → 导入数据源
      ├─► 扫描 /etc/grafana/provisioning/dashboards/   → 注册 dashboard provider
      │         │
      │         └─► 根据 provider 配置，扫描指定目录中的 .json 文件 → 导入 dashboard
      │
      ▼
Grafana 就绪，访问 http://localhost:3000 即可看到数据源和 dashboard 已就位
```

### 为什么要用 Provisioning

| 方式 | 优点 | 缺点 |
|------|------|------|
| 手动 UI 配置 | 直观 | 容器重建后配置丢失；无法版本管理 |
| Provisioning 文件 | 可 Git 管理；重建后自动恢复；CI/CD 友好 | 需要了解 YAML 结构 |

---

## 3. Datasource Provisioning 详解

文件位置：`grafana/provisioning/datasources/prometheus.yml`

挂载到容器：`/etc/grafana/provisioning/datasources/prometheus.yml`

```yaml
apiVersion: 1
datasources:
  - name: Prometheus       # UI 显示名
    type: prometheus        # 插件类型
    access: proxy           # 访问模式（见第5节）
    url: http://prometheus:9090   # Prometheus 地址（容器内 DNS）
    uid: prometheus-trading  # 固定 UID，dashboard JSON 用此引用
    isDefault: true          # 设为默认数据源
    editable: false          # 禁止 UI 修改
    version: 1               # 修改配置时需递增，否则 Grafana 不重新应用
```

### 关于 `uid` 字段的重要性

Dashboard JSON 文件中每个面板都需要指定数据源：
```json
"datasource": {
  "type": "prometheus",
  "uid": "prometheus-trading"
}
```

如果不在 datasource provisioning 中显式设置 `uid`，Grafana 会随机生成一个，每次重建容器时 uid 变化，导致 dashboard 中的面板无法找到对应数据源（显示"数据源未找到"错误）。

**固定 uid 是 provisioning 最佳实践之一。**

---

## 4. Dashboard Provisioning 详解（两阶段）

> 官方文档：https://grafana.com/docs/grafana/latest/administration/provisioning/#dashboards

Dashboard 的 provisioning 需要两类文件共同配合：

### 第一阶段：Provider 配置（YAML）

文件：`grafana/provisioning/dashboards/provider.yml`

```yaml
apiVersion: 1
providers:
  - name: "trading"
    type: file
    options:
      path: /var/lib/grafana/dashboards   # 容器内路径
    updateIntervalSeconds: 10
    allowUiUpdates: false
    disableDeletion: false
```

**作用**：告诉 Grafana"去 `/var/lib/grafana/dashboards/` 这个目录扫描 JSON 文件"。

### 第二阶段：Dashboard JSON 文件

文件：`grafana/dashboards/trading.json`

这是 Grafana dashboard 的标准 JSON 格式，包含：
- `uid`：dashboard 的唯一标识符（URL 的一部分，固定后 URL 不变）
- `title`：dashboard 名称
- `panels`：面板数组，每个面板定义查询、可视化类型、布局等
- `time` / `refresh`：默认时间范围和自动刷新间隔

**工作流**：

```
宿主机 ./grafana/dashboards/trading.json
                │
                │  docker-compose volume mount
                ▼
容器内 /var/lib/grafana/dashboards/trading.json
                │
                │  Grafana provider 每 10s 扫描
                ▼
        Grafana 数据库（SQLite）
                │
                │  用户打开 UI
                ▼
        浏览器显示 "Trading Monitor" dashboard
```

---

## 5. proxy 访问模式的请求链路

`access: proxy` 是 Grafana 推荐的数据源访问方式：

```
┌─────────────────────────────────────────────────────────────┐
│  用户浏览器                                                   │
│  执行 PromQL 查询（如点击刷新或时间范围变化）                   │
└─────────────────┬───────────────────────────────────────────┘
                  │  HTTP POST /api/datasources/proxy/...
                  │  目标：Grafana 后端（:3000）
                  ▼
┌─────────────────────────────────────────────────────────────┐
│  Grafana 后端服务器（容器内）                                  │
│  接收查询请求 → 转换为 Prometheus API 调用                     │
└─────────────────┬───────────────────────────────────────────┘
                  │  HTTP GET http://prometheus:9090/api/v1/query_range
                  │  （通过 Docker 内部网络，无需经过宿主机）
                  ▼
┌─────────────────────────────────────────────────────────────┐
│  Prometheus（容器内）                                         │
│  执行 PromQL，返回 JSON 结果                                   │
└─────────────────────────────────────────────────────────────┘
```

**好处**：
- 用户浏览器不需要能访问 Prometheus（9090 端口可以不对外暴露）
- 跨域（CORS）问题由 Grafana 处理
- 可以在 Grafana 层加身份验证，Prometheus 无需自行处理鉴权

---

## 6. allowUiUpdates 的行为说明

这是 provisioning 中最容易让人困惑的字段。

### `allowUiUpdates: false`（本项目使用）

```
用户在 UI 修改 dashboard → 点击保存 → ❌ 报错：Cannot save provisioned dashboard
```

Grafana 拒绝将修改写回数据库。用户只能查看，不能直接保存。

**如何修改 dashboard**：
1. 在 Grafana UI 中进行修改
2. 点击 Dashboard settings → JSON Model，复制完整 JSON
3. 用复制的 JSON 覆盖宿主机的 `grafana/dashboards/trading.json`
4. Grafana 会在 `updateIntervalSeconds` 内检测到文件变化并自动重新加载

### `allowUiUpdates: true`

```
用户在 UI 修改 dashboard → 点击保存 → ✅ 成功写入数据库
                                              │
                                              │（但！）
                                              ▼
                                  Grafana 重启 → 文件覆盖数据库 → UI 修改丢失
```

"文件永远获胜"（File wins）是 Grafana provisioning 的核心原则。

---

## 7. Named Volumes 持久化说明

本项目在 `docker-compose.yml` 中使用两个 named volumes：

### `grafana_data:/var/lib/grafana`

Grafana 的主数据目录，存储：
- `grafana.db`：SQLite 数据库，包含用户、组织、dashboard（通过 UI 创建的）、告警规则、API keys 等
- `plugins/`：已安装的 Grafana 插件
- `png/`：图片渲染缓存

**如果不持久化**：每次 `docker compose down && up`，Grafana 都是全新状态，所有在 UI 中创建的内容（用户密码修改、手动添加的 dashboard 等）都会丢失。

**注意**：Provisioning 文件导入的数据源和 dashboard 会在每次启动时重新加载，即使数据卷被删除也会自动恢复。持久化的意义主要在于保存**通过 UI 手动创建的内容**。

### `prometheus_data:/prometheus`

Prometheus 的 TSDB 数据目录（详见 `prometheus/README.md` 第7节）。

### 操作命令

```bash
# 查看所有 volume
docker volume ls

# 删除项目的 volume（慎用！会丢失历史数据）
docker compose down -v

# 只停止容器，不删除 volume
docker compose down
```

---

## 8. 本项目文件挂载关系图

```
宿主机文件系统                          容器内路径
──────────────────────────────────────────────────────────

./grafana/provisioning/       →    /etc/grafana/provisioning/
  datasources/prometheus.yml  →      datasources/prometheus.yml
  dashboards/provider.yml     →      dashboards/provider.yml

./grafana/dashboards/         →    /var/lib/grafana/dashboards/
  trading.json                →      trading.json

grafana_data (named volume)   →    /var/lib/grafana/
  (grafana.db, plugins, ...)          (grafana.db, plugins, ...)

──────────────────────────────────────────────────────────
Grafana 读取 /etc/grafana/provisioning/ 完成自动配置
Grafana 读取 /var/lib/grafana/dashboards/ 导入 dashboard JSON
Grafana 将运行时数据写入 /var/lib/grafana/（由 named volume 持久化）
```

---

## 9. 官方文档索引

| 主题 | 链接 |
|------|------|
| Grafana 概述 | https://grafana.com/docs/grafana/latest/introduction/ |
| Provisioning 总览 | https://grafana.com/docs/grafana/latest/administration/provisioning/ |
| Datasource Provisioning | https://grafana.com/docs/grafana/latest/administration/provisioning/#data-sources |
| Dashboard Provisioning | https://grafana.com/docs/grafana/latest/administration/provisioning/#dashboards |
| Prometheus 数据源插件 | https://grafana.com/docs/grafana/latest/datasources/prometheus/ |
| Dashboard JSON 模型参考 | https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/view-dashboard-json-model/ |
| 面板可视化类型 | https://grafana.com/docs/grafana/latest/panels-visualizations/ |
| Grafana 环境变量配置 | https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/#override-configuration-with-environment-variables |
