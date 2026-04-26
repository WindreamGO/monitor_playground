"""
textual-serve 启动入口
---------------------
将 TradingMonitorApp 通过 WebSocket 暴露为 Web 应用。

监听地址：0.0.0.0:8080
  - 0.0.0.0 允许 Docker 外部流量进入容器
  - 宿主机访问地址由环境变量 PUBLIC_URL 决定

textual-serve 会将 public_url 嵌入 HTML 页面，用于构造 WebSocket URL。
若 public_url 与浏览器实际访问的地址不一致，WebSocket 将无法建立连接。
通过环境变量注入外部访问地址，使 Docker 端口映射下也能正常工作。
"""

import os

from textual_serve.server import Server

# PUBLIC_URL：浏览器实际访问的地址，需与 docker-compose 端口映射一致
# 例：宿主机映射 10000:8080 → PUBLIC_URL=http://localhost:10000
public_url = os.environ["PUBLIC_URL"]

server = Server(
    # 子进程命令：每次有浏览器连接时执行，产生一个 Textual app 实例
    ".venv/bin/python app.py",
    # 绑定全部网络接口，使 Docker 端口映射生效
    host="0.0.0.0",
    port=8080,
    title="Trading Monitor",
    # 告知 textual-serve 用哪个 URL 构建 WebSocket 地址
    public_url=public_url,
)

server.serve()
