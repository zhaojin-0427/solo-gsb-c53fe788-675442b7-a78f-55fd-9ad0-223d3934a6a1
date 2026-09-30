# 离线矢量瓦片编译 API

基于 **Python + FastAPI + SQLite + Shapely + mapbox-vector-tile** 的离线矢量瓦片服务。
上传 **EPSG:4326 GeoJSON** 图层后，服务以**不可变 revision（修订版本）**保存；
请求瓦片时按 **Web Mercator (EPSG:3857)** 投影、在线编译为 **Mapbox Vector Tile (.mvt)**，
并保证**相同输入永远产生字节级一致的瓦片和 ETag**。

---

## 1. 功能特性

| 能力 | 说明 |
| --- | --- |
| 发布图层 | `POST /datasets` 上传 GeoJSON `FeatureCollection`（multipart 或原始 JSON body），创建数据集并发布 **revision 1** |
| 不可变版本 | 每次发布写入一个新的 revision；历史 revision 永不被覆盖，可通过 `?revision=N` 固定取瓦片 |
| 乐观并发控制 | 发布新版本必须携带 `expected_revision`，与当前 head 不一致时返回 **409 Conflict**；版本比较、修订插入、head 更新在**跨进程事务（`BEGIN IMMEDIATE`）**内原子完成，多个 worker 同时追加时恰好一个成功 |
| 瓦片编译 | `GET /datasets/{id}/tiles/{z}/{x}/{y}.mvt`，支持 **z = 0–14** 的 XYZ 瓦片 |
| 投影 | EPSG:4326 经纬度 → 球面 Web Mercator（R = 6378137），瓦片内坐标系为 4096 网格、Y 轴向下 |
| 8 像素缓冲裁剪 | 点 / 线 / 面先裁剪到瓦片外扩 **8 px（128/4096 单位）**的边界，保证跨瓦片连续 |
| 0.5 px 拓扑简化 | 以 **0.5 px（8/4096 单位）**容差做保拓扑 Douglas–Peucker 简化 |
| 4096 量化 | 坐标四舍五入到整数 4096 网格，并清除退化线 / 零面积环 |
| 稳定排序 | 图层按名称、要素按 ID（数值 → 字符串 → 内容哈希）确定性排序后编码 |
| 字节一致 / ETag | 同一 revision + 同一瓦片坐标，重复请求**字节完全相同、ETag 相同**；已编译瓦片持久化到 SQLite |
| 条件请求 | 支持 `If-None-Match`，命中时返回 **304 Not Modified** |
| 错误处理 | 非法 GeoJSON / 非法几何 / 无效瓦片参数返回 **400**，数据集或版本不存在返回 **404** |
| 一键启动 | 提供 `Dockerfile` 与 `docker-compose.yml`，数据持久化在命名卷中 |

---

## 2. 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由：发布、版本、瓦片、错误处理
│   ├── tiler.py         # 裁剪 / 简化 / 量化 / 稳定排序 / MVT 编码
│   ├── projection.py    # EPSG:4326 -> Web Mercator -> 瓦片 4096 网格
│   ├── geojson.py       # GeoJSON 校验与规范化
│   ├── db.py            # SQLite 存储（datasets / revisions / tile_cache）
│   └── config.py        # 环境变量配置
├── data/sample.geojson  # 示例图层（点 / 线 / 面）
├── tests/               # pytest 测试（55 个用例，含跨进程并发回归）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 3. Docker Compose 一键启动（推荐）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：<http://localhost:8000>
- 交互文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>
- SQLite 数据库保存在 Docker 命名卷 `tile-data`（容器内 `/data/tiles.db`），容器重建后数据不丢
- Compose 默认以 **2 个独立 uvicorn worker 进程**（`--workers 2`）运行，二者共享同一个
  SQLite 库；这正是“并发追加发布”竞态的真实部署形态，可直接用下一节的命令复现

常用命令：

```bash
docker compose ps            # 查看状态与健康检查
docker compose logs -f       # 查看日志
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据卷
```

> 如需把数据库放到宿主机目录，可把 `docker-compose.yml` 中的
> `tile-data:/data` 改为 `./data:/data`。

### 3.1 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export VT_DB_PATH=./data/tiles.db          # SQLite 文件路径
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

不用 Docker 也想复现跨进程竞态时，启动**两个指向同一 SQLite 文件**的 uvicorn，
再分别向两个端口并发 POST（等价于 Compose 的 `--workers 2`）：

```bash
export VT_DB_PATH=./data/tiles.db
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001 &
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8002 &
# 之后让两个请求分别打到 8001 与 8002（把 4.2.1 示例中的 8000 换成对应端口）
```

---

## 4. HTTP API 与 curl 示例

以下示例假设服务运行在 `http://localhost:8000`。

### 4.1 发布图层（创建数据集，revision 1）

使用 `multipart/form-data` 上传（字段 `file` 为 GeoJSON，`layer` 为 MVT 图层名）：

```bash
curl -s -X POST http://localhost:8000/datasets \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "layer=demo"
```

返回 `201 Created`：

```json
{
  "id": "32dca29ff4a24c869d9aa4b4686c26fa",
  "layer": "demo",
  "revision": 1,
  "content_hash": "96b7b15130feb9a517c348cf985e007217e29c456f4baa80b728ee7186408",
  "feature_count": 4,
  "published_at": "2026-09-29T14:21:52Z"
}
```

也可以直接发送原始 GeoJSON body（图层名用 query 参数，省略时为 `default`）：

```bash
curl -s -X POST "http://localhost:8000/datasets?layer=demo" \
  -H "Content-Type: application/geo+json" \
  --data-binary @data/sample.geojson
```

把数据集 ID 存到变量，后续示例复用：

```bash
DS=32dca29ff4a24c869d9aa4b4686c26fa
```

### 4.2 发布新版本（乐观并发控制）

新版本必须携带 `expected_revision`（当前 head 版本号）。成功后 revision 前进为 2：

```bash
curl -s -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1"
```

如果 `expected_revision` 已过期（head 已被别人推进），返回 **409 Conflict**：

```bash
curl -i -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1"
```

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": "revision conflict: expected_revision does not match current head",
  "dataset_id": "32dca29ff4a24c869d9aa4b4686c26fa",
  "expected_revision": 1,
  "current_revision": 2
}
```

客户端应重新拉取 head、合并变更后，再用新的 `expected_revision` 重试。

### 4.2.1 并发追加发布复现（两个 worker 同时提交同一 expected_revision）

Compose 已按 **2 个 worker 进程**启动，下面用一段 shell 把两个请求**同时**发往服务
（`curl ... &` 后统一 `wait`）。二者都读到 head=1、都带 `expected_revision=1`，
但版本比较、修订插入与 head 更新在跨进程的 `BEGIN IMMEDIATE` 事务内串行完成：
**恰好一方得到 201，另一方得到 409，且 409 的 `current_revision` 指向新 head。**

先发布 revision 1 并取出数据集 ID：

```bash
DS=$(curl -s -X POST "http://localhost:8000/datasets?layer=demo" \
  -H "Content-Type: application/json" \
  --data-binary @data/sample.geojson | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "$DS"
# 55e7b06cf2d748a7b38b135c63592142
```

同时发两个追加请求（multipart 形式，与单请求示例等价）：

```bash
curl -s -o /tmp/racer1.json -w "racer1 -> HTTP %{http_code}\n" \
  -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1" &
curl -s -o /tmp/racer2.json -w "racer2 -> HTTP %{http_code}\n" \
  -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1" &
wait
```

真实输出（每次运行只有一方赢，顺序可能互换）：

```text
racer1 -> HTTP 201
racer2 -> HTTP 409
```

胜者 `201 Created`（`/tmp/racer1.json`）：

```json
{
  "id": "55e7b06cf2d748a7b38b135c63592142",
  "layer": "demo",
  "revision": 2,
  "content_hash": "96b7b15130feb9a517c348cf985e007217e29c456f4baa80b728ee7186408",
  "feature_count": 4,
  "published_at": "2026-09-30T13:00:37Z"
}
```

败者 `409 Conflict`（`/tmp/racer2.json`），`current_revision` 即新 head：

```json
{
  "error": "revision conflict: expected_revision does not match current head",
  "dataset_id": "55e7b06cf2d748a7b38b135c63592142",
  "expected_revision": 1,
  "current_revision": 2
}
```

验证“没有额外版本、没有半提交”，且**历史 revision 1 的瓦片仍可读取**：

```bash
curl -s "http://localhost:8000/datasets/$DS" | python3 -c 'import sys,json;d=json.load(sys.stdin);print("latest:",d["latest_revision"],"revisions:",[r["revision"] for r in d["revisions"]])'
# latest: 2 revisions: [1, 2]

# 固定取历史 revision 1（注意响应头 x-revision: 1，缓存策略为 immutable）
curl -s -D - -o /dev/null "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt?revision=1" | grep -iE "^HTTP|x-revision"
# HTTP/1.1 200 OK
# x-revision: 1

# 不带 revision 时取当前 head（x-revision: 2）
curl -s -D - -o /dev/null "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt" | grep -iE "^HTTP|x-revision"
# HTTP/1.1 200 OK
# x-revision: 2
```

> 压测可把并发数提高，例如 `for i in $(seq 1 10); do curl ... & done; wait`：
> 结果始终是 **1 × 201，9 × 409**，head 只前进一版，服务端不会出现 500。

### 4.3 查看数据集与版本列表

```bash
curl -s http://localhost:8000/datasets
curl -s http://localhost:8000/datasets/$DS | python3 -m json.tool
```

### 4.4 获取矢量瓦片

瓦片路径为标准 XYZ 编号（`y=0` 在最北），缩放级别 **0–14**：

```bash
# 0 级世界瓦片
curl -s -o tile-0-0-0.mvt \
  -D - \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
```

响应头：

```http
HTTP/1.1 200 OK
content-type: application/vnd.mapbox-vector-tile
etag: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"
x-revision: 3
vary: If-None-Match
cache-control: public, no-cache
```

取一个高级别瓦片（例如北京附近的 z=14 瓦片）：

```bash
curl -s -o beijing-z14.mvt \
  "http://localhost:8000/datasets/$DS/tiles/14/13489/6208.mvt"
```

**固定版本取瓦片**（适合长缓存，响应为 `Cache-Control: …, immutable`）：

```bash
curl -s "http://localhost:8000/datasets/$DS/tiles/10/807/401.mvt?revision=1" -o r1.mvt
```

### 4.5 条件请求（If-None-Match → 304）

先用 `-c` 把 ETag 存进 cookie jar，再带上 `If-None-Match`：

```bash
curl -s -o tile.mvt -c cookies.txt \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"

ETAG=$(grep -i etag cookies.txt | awk '{print $NF}')

curl -s -o /dev/null -w "%{http_code}\n" \
  -H "If-None-Match: $ETAG" \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
# 304
```

或者手工复制 ETag：

```bash
curl -i -H 'If-None-Match: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"' \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
# HTTP/1.1 304 Not Modified（无响应体）
```

### 4.6 错误示例

| 场景 | 状态码 |
| --- | --- |
| GeoJSON 不是 FeatureCollection / 空集合 / JSON 解析失败 / 坐标越界 / 自交多边形 / null 几何 | `400` |
| 瓦片 `z` 超出 0–14、`x/y` 为负或超出该级别范围、非整数 | `400` |
| 数据集或 `revision` 不存在 | `404` |
| 发布版本时 `expected_revision` 与 head 不一致（含跨进程并发落败，`current_revision` 指向新 head） | `409` |
| 上传体积超过限制（默认 50 MB） | `413` |

```bash
# 非法缩放级别
curl -i "http://localhost:8000/datasets/$DS/tiles/15/0/0.mvt"
# HTTP/1.1 400 Bad Request
# {"error":"zoom z must be between 0 and 14"}

# x 超出范围（z=2 时合法范围为 0..3）
curl -i "http://localhost:8000/datasets/$DS/tiles/2/9/0.mvt"
# {"error":"tile coordinates out of range for z=2: 0 <= x,y < 4"}

# 非法 GeoJSON
curl -i -X POST http://localhost:8000/datasets \
  -H "Content-Type: application/json" \
  -d '{"type":"FeatureCollection","features":[]}'
# HTTP/1.1 400 Bad Request
# {"error":"FeatureCollection contains no features"}
```

---

## 5. 瓦片规格（编译管线）

对每个瓦片坐标与每个源要素依次执行：

1. **投影**：经度/纬度（EPSG:4326）→ 球面 Web Mercator 米（EPSG:3857，
   纬度在 ±85.05112878° 处截断），再线性映射到当前瓦片的 **4096 × 4096**
   局部整数网格（原点左上、X 向右、Y 向下）。
2. **裁剪**：与外扩 **8 像素**（8/256 × 4096 = **128 单位**）的矩形求交；
   裁剪后维度降级的部分被丢弃（线不允许退化成点，面不允许退化成线）。
3. **简化**：`preserve_topology=True` 的 Douglas–Peucker 简化，容差
   **0.5 像素 = 8 单位**。
4. **量化与清洗**：四舍五入到整数网格并夹在缓冲窗口内；删除重合点、
   零长度线段与零面积环，保证几何有效。
5. **稳定排序编码**：图层名升序，要素按 ID 分类确定性排序
   （非负整数 ID → 字符串 ID → 无 ID 者按内容 SHA-256），属性键排序，
   再交给 `mapbox-vector-tile` 编码为 MVT v2（`extent=4096`）。

**确定性**：管线中不含任何随机量或无序字典遍历；瓦片首次编译后，字节体与
ETag 会持久化在 SQLite `tile_cache` 表中，因此重复请求乃至服务进程重启后，
同一 `(dataset, revision, z, x, y)` 的字节和 ETag 始终一致。

> 说明：MVT 要素 ID 按协议仅支持非负 uint64 整数；字符串 ID 的要素仍会被
> 确定性排序，但其 ID 不写入 protobuf（属性中若需要可自行保留）。

---

## 6. 并发与事务语义（跨进程）

追加发布 `POST /datasets/{id}/revisions` 必须是一次跨进程原子的
**比较并设置（compare-and-set）**：

```text
比较 latest_revision == expected_revision
  → 插入 revisions(next)
  → 更新 datasets.latest_revision = next
```

实现要点（见 `app/db.py`）：

1. 连接以 `isolation_level=None`（autocommit）打开，Python 不会在背后隐式开启
   延迟事务；写操作统一走显式的 `write_transaction()`。
2. 追加发布以 **`BEGIN IMMEDIATE`** 开启事务：进入时即取得 SQLite（WAL 模式）唯一的
   **数据库级写锁**。因此无论请求来自同一进程的线程，还是**不同 worker / 容器进程**，
   这段“比较 → 插入 → 更新”都被该写锁串行化。
3. 落败方在拿到锁后重新读到的已是新 head，比较失败 → 抛 `RevisionConflict`
   （HTTP **409**，响应体 `current_revision` 指向新 head），事务回滚，
   **不会**再去撞 `revisions` 的主键而冒出未映射的 500。
4. 插入与 head 更新在同一事务内提交：要么都成功（201，仅一个新 revision），
   要么都回滚（409），不存在“插了修订但 head 没动”或多出一个版本的半提交状态。
   `UPDATE datasets ... WHERE latest_revision = <读到的值>` 再加一道行级条件，
   并把任何残留的 `IntegrityError` 统一映射为 409 作为兜底。
5. 修订内容不可变，旧 revision 行从不更新或删除，因此发布新版本后
   `?revision=N` 仍能读到历史瓦片；历史瓦片缓存也按 `(dataset, revision, z, x, y)`
   独立存放，不受 head 前进影响。

冷启动方面：多个 worker 可能在全新数据卷上**并发初始化**。连接在切换
`journal_mode=WAL`（一次性、需要排他锁）**之前**先设置 `busy_timeout`，
并对初始化做了重试，因此两个 worker 同时首启也不会因 `database is locked` 而启动失败。

并发回归测试见 `tests/test_concurrency.py`：它用 `multiprocessing` 拉起 **4 个真实
操作系统进程**、各自持有独立连接并在屏障处同时提交，连续 10 轮断言
“恰 1 个 created、其余 3 个 conflict 且都指向新 head、版本数与 head 一致”。

---

## 7. API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/datasets` | 上传 GeoJSON，创建数据集并发布 revision 1 |
| `POST` | `/datasets/{id}/revisions` | 追加不可变 revision（需 `expected_revision`，冲突返回 409） |
| `GET` | `/datasets` | 列出全部数据集 |
| `GET` | `/datasets/{id}` | 数据集元数据与全部 revision |
| `GET/HEAD` | `/datasets/{id}/tiles/{z}/{x}/{y}.mvt` | 获取瓦片（支持 `?revision=N` 与 `If-None-Match`） |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/docs` | Swagger UI 交互文档 |

---

## 8. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VT_DB_PATH` | `/data/tiles.db` | SQLite 数据库路径 |
| `VT_HOST` / `VT_PORT` | `0.0.0.0` / `8000` | 监听地址与端口 |
| `VT_MIN_ZOOM` / `VT_MAX_ZOOM` | `0` / `14` | 允许编译的缩放级别范围 |
| `VT_MAX_UPLOAD_BYTES` | `52428800` | 单次上传大小上限（50 MB） |
| `VT_TILE_CACHE_SIZE` | `4096` | 进程内瓦片 LRU 缓存条目数 |
