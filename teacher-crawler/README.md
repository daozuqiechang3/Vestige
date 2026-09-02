# Teacher Crawler MVP

一个面向静态学校网站、由 YAML 配置驱动的教师信息采集工具。它从教师目录发现个人主页，提取姓名、职称、邮箱、研究方向、正文和照片，并输出原始 JSON、每人一份 Word 和汇总 CSV。

## 安装

PowerShell：

```powershell
py -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -e ".[test]"
```

## 配置

复制 `configs/bit-cs.yaml`（推荐格式）或 `configs/example.yaml`（通用选择器格式），为目标学校调整：

- `school` / `college`：写入每条统一教师记录的学校和学院。
- `start_urls`：一个或多个教师目录页，必填且不能为空。
- `allowed_domains`：允许访问的域名白名单，必填且不能为空。
- `discovery.link_selectors`：目录页中个人主页链接的 CSS 选择器。
- `include_patterns` / `exclude_patterns`：作用于完整 URL 的正则表达式。
- `selectors`：个人页各字段的 CSS 选择器，按顺序回退；`sections` 可配置教育经历、工作经历、论文等命名分区。
- `profile`：学校专用个人页结构，可根据 URL 片段映射教师类别。
- `request`：限速、超时、重试次数和 User-Agent。

建议先在浏览器开发者工具中确认选择器，每次只接入一所学校。请设置能表明用途和联系方式的 User-Agent，并遵守目标站点的使用条款与 robots.txt。

## 运行

```powershell
teacher-crawler --config configs/my-school.yaml
```

也可以不安装命令入口直接运行：

```powershell
py -m crawler.main --config configs/my-school.yaml
```

## Web 工具

启动本地服务：

```powershell
.\.venv\Scripts\Activate.ps1
py -m crawler.web
```

浏览器打开 `http://127.0.0.1:8000`。页面支持提交教师列表页 URL、查看任务进度和日志，并把本地 HTML 分析为可搜索、筛选、排序和人工标记的导师数据。支持直接下载 `teachers.csv`、单个教师 Word，以及将该任务的 HTML、Word、JSON、照片和 CSV 打包下载。页面使用全部可用宽度，宽表在自身区域内横向滚动。

导师筛选支持：

- 按姓名、研究方向、实验室和招生原文搜索。
- 筛选实习、硕博/推免、欢迎联系三类招生信号。
- 仅显示有招生信息、有邮箱或已收藏的教师。
- 按招生信号、姓名、职称和研究方向排序。
- 保存收藏、联系状态和人工备注；重新解析不会覆盖这些人工字段。
- 任务结束后单独显示失败教师的姓名、详情页 URL 和错误原因。
- 可以重试单个失败教师或顺序重试全部失败项；成功项会自动进入导师表格。

Web 工具只接受 `configs/*.yaml` 中 `allowed_domains` 明确允许的域名。新增学校时先复制 YAML 配置并验证选择器；它不会把任意 URL 当作通用爬取入口。默认采集上限为 2，可在页面中调整。重试同样使用配置的请求延时、HTTP 重试、超时和域名限制，批量重试会顺序处理失败项。任务结果保存在 `output/web-tasks/<task-id>/`。

局域网部署时可运行：

```powershell
py -m uvicorn crawler.web:app --host 0.0.0.0 --port 8000
```

不要直接暴露到公网。采集公开页面时应保留合理延时、设置有效联系方式的 User-Agent，并遵守目标网站的 robots.txt、使用条款和访问频率要求。

首次接入学校时先限制抓取两位教师：

```powershell
py -m crawler.main --config configs/bit-cs.yaml --limit 2
```

默认启用断点续跑。`output/state.json` 记录已完成 URL、内容重复项和失败信息；已完成页面会跳过，失败页面下次会再次尝试。需要全部重新抓取时使用：

```powershell
teacher-crawler --config configs/my-school.yaml --no-resume
```

输出结构：

```text
output/
  cache/         以 URL SHA-256 命名的原始 HTML 缓存
  html/          每位教师一份完整原始网页源码
  json/          原始结构化记录
  documents/     每位教师一份 .docx
  photos/        下载成功的照片
  teachers.csv   全部成功及失败教师汇总（失败项除姓名和主页外留空）
  failures.csv   抓取失败的个人页及错误信息
  teacher_data.db 筛选、排序和人工标记使用的 SQLite 数据库
  state.json     断点状态
```

每个 JSON 都使用 `crawler.models.Teacher` 的统一结构，包括学校、学院、姓名、类别、职称、邮箱、电话、院系、研究方向、招生信息、命名分区、全文、照片 URL、个人页 URL 和采集时间。JSON 是主数据源；CSV 和 Word 可以直接从 JSON 重建。断点与去重信息只保存在 `state.json`，不会混入教师记录。

第一版仅处理无需 JavaScript 渲染的公开静态页面，不处理登录、验证码、自动邮件或跨域无限爬取。

## 测试

```powershell
pytest
```
