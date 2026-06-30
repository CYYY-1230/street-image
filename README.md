# StreetScope Research MVP

街景研究数据生成器的本地 MVP。它把 PRD 中最核心的科研工作流先跑通：

- 地图按钮绘制矩形研究区，绘制后可拖动中心点移动、拖动角点/边点改大小
- 本地自动保存项目草稿
- 项目中心展示当前项目、近期项目、近期任务和最近导出
- 导入/导出项目配置 JSON
- 自动生成可采样路网与采样点
- 通过 OSM Overpass 加载真实路网并采样
- 支持 25-500m 自定义采样间隔
- 上传 GeoJSON / KML / zipped Shapefile 边界
- 上传 CSV 或 zipped Shapefile 采样点
- 上传 GeoJSON 路网并按间隔生成采样点
- 上传 zipped Shapefile 路网并按间隔生成采样点
- 估算点位、图像量、API 调用量
- 配置百度街景 API 参数
- 支持四方向图、全景图、四方向图 + 全景图下载模式
- 支持三种下载来源：演示模式、官方 API Key、已授权 Web 无 AK 模式
- 支持跳过已下载图片、设置并发数和失败自动重试次数
- 支持本地街景图像归档复用，相同采样点和图像参数会从 `backend/data/archive/streetview_images` 复用
- 测试百度地图 API Key
- 创建街景图像下载任务
- 暂停、继续、取消任务
- 查看任务记录、按状态/质量/方向/道路筛选、标记图片质量、批量排除、重试失败下载
- 创建语义分割与指标计算任务
- 支持演示推理或外部分割服务推理
- 上传已有街景图片或图片 ZIP 后直接分割和计算指标
- 勾选绿视率、蓝视率、天空开阔度、建筑占比、道路占比、人行空间、车行空间、人车密度、视觉熵、色彩丰富度等指标
- 导出采样点 CSV/GeoJSON
- 导出 CSV/GeoJSON/SHP/ZIP 数据包

## 启动

Windows 本地使用请看：[docs/Windows本地使用与Mac同步.md](/Users/cy/Desktop/Codex-Workspaces/2026-06-28 street /docs/Windows本地使用与Mac同步.md)。

GitHub + Vercel + Supabase + Windows Worker 公网协作方案请看：[docs/公网协作方案_GitHub_Vercel_Supabase_Worker.md](/Users/cy/Desktop/Codex-Workspaces/2026-06-28 street /docs/公网协作方案_GitHub_Vercel_Supabase_Worker.md)。

后端：

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

前端：

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 `http://localhost:5173`。

## 说明

当前版本默认使用演示下载任务，不消耗百度 API 额度。勾选“真实百度 API 下载”并填写 API Key 后，后端会请求百度全景静态图接口。

若百度测试返回 `status=240` / `APP 服务被禁用`，通常表示该 AK 在百度开发者控制台未启用对应全景/街景服务，或应用服务状态被禁用；这不是坐标或图像参数错误。

本地教程中的“无需 AK”方案使用百度 Web 侧接口 `mapsv0.bdimg.com` 获取 `panoid` 和 `pr3d` 图像。本系统已将其做成“授权 Web 无 AK”下载来源；请仅在已取得授权的情况下使用。该模式会在导出日志中记录 `panoid`、`panoid_url`、`request_url`、状态码、字节数和耗时。

语义分割默认使用演示推理，便于无 GPU 环境跑通流程。选择“外部分割服务”后，后端会以 `multipart/form-data` 向服务地址 POST：

- `image`: 图片文件
- `model_name`: 模型名称

外部服务应返回 JSON，可直接返回 `gvi, bvi, sky_ratio, water_ratio, building_ratio, road_ratio, sidewalk_ratio, vehicle_ratio, person_ratio, enclosure_ratio, natural_ratio, vehicle_space_ratio, hardscape_ratio, human_vehicle_density, visual_entropy, cvi` 等字段，也可以放在 `metrics` 对象中。这样可以接本地或云端 Mask2Former、DeepLabV3+ 等推理服务。

项目配置会自动保存到浏览器 `localStorage`。手动导出的项目 JSON 包含研究区、采样设置、图像参数、模型选择、采样点和路网，不包含百度 API Key。

后端会把近期任务和项目摘要持久化到 `backend/data/tasks_index.json`，服务重启后仍可在页面顶部“项目中心”查看历史任务与最近导出。也可调用：

- `GET /api/tasks`
- `GET /api/projects`

如果服务重启时有未完成任务，系统会将其标记为已取消，避免误认为后台仍在运行。

## 已支持的数据入口

- 研究区：行政区预设、矩形边界、GeoJSON / KML / zipped Shapefile 边界文件。
- 采样点：前端自动生成、CSV 上传、zipped Shapefile 点要素上传。CSV/SHP 属性可包含 `point_id,lng,lat,road_name,road_id,admin_name` 等字段。
- 路网：GeoJSON `LineString` / `MultiLineString` / `FeatureCollection`，或 zipped Shapefile 上传，系统按当前采样间隔插值生成点。
- 自动路网：可使用本地模拟路网快速验证流程，也可调用 OSM Overpass 获取当前矩形范围内真实 `highway` 路网；为避免请求过重，单次 OSM 加载限制约 80 km²。
- 路网清洗：OSM/上传路网可启用清洗，系统会去除重复点、剔除过短路段、简化折点、去除近似重复路段，并在预估区显示清洗前后路段数。
- 已有图片：支持多张 `jpg/jpeg/png/webp` 或图片 ZIP 上传，文件名中的点号、方向、经纬度会尽量自动识别。
- 坐标转换：WGS84 输入会按 `WGS84 -> GCJ02 -> BD09` 转换，用于百度街景请求点位；采样点导出同时包含 WGS84、GCJ02、BD09 字段。

## 当前导出内容

下载任务 ZIP：

- `01_boundary/boundary.geojson`
- `02_road_network/roads.geojson`
- `02_road_network/shp/roads.shp`
- `02_road_network/shp/roads.shx`
- `02_road_network/shp/roads.dbf`
- `02_road_network/shp/roads.prj`
- `03_sample_points/sample_points.csv`
- `03_sample_points/sample_points.geojson`
- `03_sample_points/shp/sample_points.shp`
- `03_sample_points/shp/sample_points.shx`
- `03_sample_points/shp/sample_points.dbf`
- `03_sample_points/shp/sample_points.prj`
- `04_streetview_images/image_manifest.csv`
- `04_streetview_images/api_request_log.csv`
- `04_streetview_images/images/*.jpg`
- `07_failed_records/failed_downloads.csv`
- `metadata.json`
- `08_method_description/method.md`

指标任务 ZIP：

- 若任务来自上传图片，原图会写入 `04_streetview_images/uploaded_images/`
- `01_boundary/boundary.geojson`
- `02_road_network/roads.geojson`
- `02_road_network/shp/roads.shp`
- `02_road_network/shp/roads.shx`
- `02_road_network/shp/roads.dbf`
- `02_road_network/shp/roads.prj`
- `03_sample_points/sample_points.csv`
- `03_sample_points/sample_points.geojson`
- `03_sample_points/shp/sample_points.shp`
- `03_sample_points/shp/sample_points.shx`
- `03_sample_points/shp/sample_points.dbf`
- `03_sample_points/shp/sample_points.prj`
- `06_metrics/image_metrics.csv`
- `06_metrics/segmentation_class_ratio.csv`
- `06_metrics/point_metrics.csv`
- `06_metrics/point_metrics.geojson`
- `06_metrics/shp/point_metrics.shp`
- `06_metrics/shp/point_metrics.shx`
- `06_metrics/shp/point_metrics.dbf`
- `06_metrics/shp/point_metrics.prj`
- `06_metrics/road_metrics.csv`
- `06_metrics/road_metrics.geojson`
- `06_metrics/shp/road_metrics.shp`
- `06_metrics/shp/road_metrics.shx`
- `06_metrics/shp/road_metrics.dbf`
- `06_metrics/shp/road_metrics.prj`
- `06_metrics/admin_metrics.csv`
- `06_metrics/admin_metrics.geojson`
- `06_metrics/shp/admin_metrics.shp`
- `06_metrics/shp/admin_metrics.shx`
- `06_metrics/shp/admin_metrics.dbf`
- `06_metrics/shp/admin_metrics.prj`
- `06_metrics/grid_metrics.csv`
- `06_metrics/grid_metrics.geojson`
- `06_metrics/shp/grid_metrics.shp`
- `06_metrics/shp/grid_metrics.shx`
- `06_metrics/shp/grid_metrics.dbf`
- `06_metrics/shp/grid_metrics.prj`
- `05_segmentation_masks/masks/*.png`
- `05_segmentation_masks/overlays/*.png`
- `metadata.json`
- `08_method_description/method.md`

## 仍需继续产品化的部分

- 当前语义分割默认是模拟推理结果，但已支持外部分割服务；真实生产建议接入稳定的 Mask2Former/DeepLabV3+ 推理服务、模型版本管理和抽样质检流程。
- 当前支持本地任务运行；后续若产品化为云服务，还需要任务队列、用户账号、额度控制、合规提示和更完整的权限体系。
