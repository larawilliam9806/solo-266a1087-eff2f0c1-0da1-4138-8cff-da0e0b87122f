# 流速断面交接台

测船交回的流速断面缺片时，制图员仍要拿到可用的原文件。这是一个 Flask 交接网页：
先上传清单建包，再逐片上传验收；恰好缺一片时，可用随船带回的**校验片**恢复。
交付记录存 SQLite，原件按片存放，按 `docker compose` 一键启动。

## 交接流程

1. **建包**：上传 JSON 清单（或直接粘贴）。清单逐项列明片号、字节数、SHA-256。
   同一份清单再次上传会**沿用已有交付记录**（按清单内容的规范化摘要去重），
   已验收的片不会重写。
2. **逐片验收**：页面与接口逐片接收，先核验字节数与 SHA-256，通过才计入交付。
   失败只影响该片，已验收的片保持可用。
3. **缺片提示**：页面始终列出缺片号。校验片恢复**只在恰好缺一片时**开放。
4. **校验片恢复**：校验片由原片「尾部补零至最长片长度后逐字节异或」得到。
   服务端用校验片 ⊕ 其余已验收片恢复缺片，**截回清单长度**，再通过 SHA-256 核验。
   恢复与末片实交并发时，只完成一次：写锁内复核状态，后到的一方只读不写。
5. **拼接下载**：集齐所有片后才开放下载，按**片号升序**流式拼接，内存为固定分块。

交付记录始终注明是否使用了恢复数据及其片号（`recovered_index`）。
恢复与下载都是固定大小分块 + 临时文件累加，内存开销不随片大小增长
（实测 1 MiB 与 8 MiB 片的峰值堆内存一致，约 350 KiB；同时打开源文件数上限 32）。

## 启动

```bash
docker compose up --build -d
# 浏览器打开 http://localhost:8000
```

镜像构建**不访问网络**：依赖来自仓库内 `vendor/` 的已验证 wheel（见下）。
数据持久化在命名卷 `handover-data`（SQLite + 分片原件 + 临时区）。

环境变量（可在 `docker-compose.yml` 或 `.env` 中覆盖）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HANDOVER_PORT` | `8000` | 宿主端口 |
| `THREADS` | `8` | waitress 工作线程 |
| `CHUNK_SIZE` | `1048576` | 上传/恢复/下载的固定分块字节数（内存上界） |
| `MAX_UPLOAD_BYTES` | 不限 | 可选，请求体总上限（字节） |
| `SECRET_KEY` | 随机 | 固定后容器重建仍保留提示消息 |
| `DATA_DIR` | `/srv/data` | 数据目录（镜像内） |

## 清单格式

```json
{
  "label": "2026-11-24 长江口断面",
  "shards": [
    {"index": 0, "size": 1048576, "sha256": "…64 位十六进制…"},
    {"index": 1, "size": 1048576, "sha256": "…"}
  ]
}
```

也接受顶层数组；键名兼容 `index/片号/序号`、`size/bytes/字节数/长度`、
`sha256/hash/摘要` 等写法。片号可重复检查、允许不连续，但下载严格按片号升序拼接。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/packages` | 建包（multipart `manifest` 或表单 `manifest_text`）。已存在返回 200 + `reused: true` |
| `GET` | `/api/packages` | 交付记录列表（含 `recovered_index`） |
| `GET` | `/api/packages/<id>` | 完整状态：片表、缺片号、是否可恢复、下载地址 |
| `PUT`/`POST` | `/api/packages/<id>/shards/<index>` | 逐片验收（multipart `file`）。重传返回 200 + `reused: true` |
| `POST` | `/api/packages/<id>/recover` | 校验片恢复（`index` + `checkshard`）。仅缺一片时可用 |
| `GET` | `/api/packages/<id>/download` | 集齐后按片号升序拼接下载 |
| `GET` | `/api/packages/<id>/manifest` | 下载规范化清单 |
| `GET` | `/healthz` | 健康检查 |

状态码约定：`201` 新建/新写入，`200` 沿用已有记录，`409` 状态不允许（缺多片、
未集齐下载），`422` 校验失败（长度/哈希不符），`507` 磁盘写入失败。

## 随船工具（生成清单与校验片）

`tools/handover.py` 与网页同语义，可在测船侧离线使用：

```bash
python3 tools/handover.py manifest   ./shards --out manifest.json --label "断面 A"
python3 tools/handover.py checkshard manifest.json ./shards --out checkshard.bin
python3 tools/handover.py verify     manifest.json ./shards
```

片文件按 `名称.<片号>` 命名（如 `section.1`、`section.2`）。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install --no-index --find-links=vendor -r requirements.txt
python3 wsgi.py                 # DATA_DIR=./data
python3 -m unittest tests.test_handover -v
```

## 目录结构

```
app/
  __init__.py   应用工厂、配置、模板过滤器、错误页
  db.py         SQLite 结构（deliveries / shards）与 IMMEDIATE 写事务
  manifest.py   清单解析、片号/长度/哈希校验、规范化摘要
  codec.py      XOR 补零校验片、缺片恢复、流式分块拼接
  service.py    建包去重、逐片验收、恢复、并发一次性提交、下载
  views.py      页面与 JSON 接口
templates/      建包、交付记录、包详情（缺片号 / 验收 / 恢复 / 下载）
static/         样式
tools/handover.py  离线生成清单与校验片
tests/          21 项端到端测试（含并发实交/恢复竞态）
vendor/         离线依赖 wheel（构建时校验 RECORD 哈希）
```

## 依赖与供应链

`vendor/` 内的 9 个 wheel 已逐个通过自身 `RECORD` 的 SHA-256 校验，版本固定
（Flask 3.1.2 / Werkzeug 3.1.8 / Jinja2 3.1.6 / MarkupSafe 3.0.4 / click 8.5.1 /
itsdangerous 2.2.0 / blinker 1.9.0 / waitress 3.0.2，含 x86_64 与 aarch64 的
MarkupSafe 轮子）。`Dockerfile` 以 `pip install --no-index --find-links=vendor`
安装，构建过程无外部网络访问。
