# 知识星球批量文档下载器

从知识星球批量下载课件、文档、图片，支持断点续传和限流自动重试。

## 功能

- 🚀 批量下载知识星球所有附件（文件 + 图片）
- ⏸️ 断点续传 —— 中断后自动从上次位置继续
- 🛡️ 限流保护 —— 遇到 1059 限流自动等待重试
- 📊 磁盘索引 —— 启动时扫描本地文件，已下载的直接跳过，零 API 消耗
- 📝 同时保存话题内容为 Markdown

## 安装

```bash
pip install curl_cffi
```

## 配置

设置环境变量（或直接写在脚本中）：

```powershell
$env:ZSXQ_TOKEN = "你的知识星球 token"
```

获取 token：浏览器登录知识星球网页版 → F12 → Application → Cookies → 复制 `zsxq_access_token` 的值。

## 用法

```bash
# 全流程（获取话题 + 下载）
python zsxq_downloader.py

# 仅获取话题元数据
python zsxq_downloader.py --fetch

# 仅下载（使用缓存的话题）
python zsxq_downloader.py --download

# 从磁盘重建进度文件（丢失 progress.json 时使用）
python zsxq_downloader.py --rebuild-progress
```

## 目录结构

```
zsxq_downloads/
└── 星球名称/
    ├── files/          # 附件（按话题分文件夹）
    ├── images/         # 图片
    └── markdown/       # 话题文本
```

## 配置项

脚本顶部的 `CONFIG` 区可调整：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PREFERRED_GROUP_NAME` | `"精益智造资料库"` | 要下载的星球名称 |
| `REQUEST_DELAY` | `5.0` | 分页请求间隔（秒） |
| `COOLDOWN_PAGES` | `8` | 每 N 页休息一次 |
| `COOLDOWN_SECONDS` | `120` | 休息时长（秒） |
| `FILE_RESOLVE_DELAY` | `3.0` | 文件 URL 解析间隔 |

## 注意事项

- 知识星球对 API 有每日调用限额，脚本遇到限额会自动保存进度退出
- 下载大星球可能需要多天，每天运行 `--download` 即可继续
- 首次运行建议先 `--fetch` 获取话题列表，再 `--download` 分批下载
