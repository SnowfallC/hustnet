# HUST校园网助手

每隔 N 分钟（默认 10 分钟）检测一次网络，断网自动重连。

## 📥 下载使用

普通用户直接下载打包好的 exe，无需安装 Python：

1. 前往 [Releases](../../releases) 下载 `HUST校园网助手.exe` 和 `config.example.ini`
2. 把它们放在**同一个文件夹**
3. 把 `config.example.ini` 改名为 `config.ini`，填入账号密码
4. 双击 `HUST校园网助手.exe`

## ⚙️ 配置

编辑 `config.ini`：

```ini
[account]
username = U2023xxxxx     ; 你的学号（首字母大写）
password = your_password  ; 你的密码

[service]
service = education       ; 默认。如选运营商改为 cmcc/telecom/unicom
```

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `server.host` | `192.168.170.168` | 认证服务器，一般无需修改 |
| `check.interval_minutes` | `10` | 检查间隔（分钟） |
| `service.service` | `education` | 运营商，可选 `cmcc/telecom/unicom` |

## 🖱 日常使用

| 操作 | 做法 |
|---|---|
| 启动 | 双击 `HUST校园网助手.exe` |
| 立即检查 | 窗口里点「立即检查并登录」 |
| 启停保活 | 窗口里点「启动/停止自动保活」 |
| 开机自启 | 窗口里勾选「开机自启动」 |
| 最小化到后台 | 点窗口右上角 X，或点「最小化到托盘」 |
| 完全退出 | 托盘图标右键 → 「退出」 |

托盘图标颜色：🟢 绿 = 已联网 / 🔴 红 = 断网 / ⚪ 灰 = 未知

## 🛠 从源码构建

```bash
# 安装依赖
pip install pywebview pystray pillow requests pycryptodome

# 运行
python webview_app.py

# 打包成 exe（自动创建虚拟环境）
打包.bat
```

## 📁 项目结构

| 文件 | 说明 |
|---|---|
| `webview_app.py` | 主程序（窗口 + 托盘 + 保活） |
| `hust_login.py` | 登录核心逻辑 |
| `webview_index.html` | 窗口界面 |
| `make_icon.py` | 生成应用图标 |
| `config.example.ini` | 配置文件模板（复制为 config.ini 使用） |
| `打包.bat` | 一键打包成 exe |

## ❓ 常见问题

- **双击 exe 没反应**：检查同目录有没有 `config.ini`；看右下角托盘的「显示隐藏的图标」。
- **账号密码对但提示失败**：检查 `service` 是否对应登录页下拉框里你选的运营商。
- **点 X 后程序还在跑**：这是正常的，点 X 是最小化到托盘，后台保活继续。要彻底退出用托盘右键 → 退出。
- **想完全卸载**：窗口里取消勾选「开机自启动」→ 删除整个文件夹。

## 免责声明

仅供个人校园网自动认证使用。请使用**自己的**校园网账号，遵守学校网络使用规定。
