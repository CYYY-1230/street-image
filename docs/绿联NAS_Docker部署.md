# StreetScope 绿联 NAS Docker 部署

适用设备：绿联 DH4300 Plus，或其他支持 Docker Compose 的 NAS。

这套部署只让 NAS 常驻运行轻量部分：

- `backend`：下载街景、缓存图片、生成 CSV/GeoJSON/Shapefile/ZIP
- `worker`：监听 Supabase 云端任务，把结果上传回 Supabase

语义分割模型仍建议使用云 GPU。NAS 没有 NVIDIA GPU 时，不建议在 NAS 上跑 Mask2Former / DeepLabv3 / PSPNet / FCN。

## 1. 确认 NAS 网络

如果以下命令能返回 HTTP 状态，说明 NAS 可以访问 Supabase：

```bash
curl -I https://zquzxuuoicheutirlxxc.supabase.co
```

返回 `404` 也没关系，说明域名和 HTTPS 网络已经通了。

绿联系统里普通用户 `ping` 可能会出现 `Operation not permitted`，这是权限限制，不影响 StreetScope。

## 2. 上传代码

推荐方式是在 NAS 终端里直接克隆 GitHub 仓库，这样以后 Mac 修改后，NAS 可以一条命令同步更新：

```bash
mkdir -p /volume1/docker
cd /volume1/docker
git clone https://github.com/CYYY-1230/street-image.git
```

如果 NAS 提示没有 `git`，也可以先把 GitHub 下载的 `street-image-main` 文件夹上传到 NAS，例如：

```text
/volume1/docker/street-image-main
```

但 ZIP 上传方式后续不能直接 `git pull`，更新时需要重新上传代码文件夹。不同绿联系统的实际路径可能不同，可以在 NAS 终端里用 `pwd` 查看当前位置。

## 3. 创建 .env

进入 NAS 终端：

```bash
cd /volume1/docker/street-image/deploy/nas
cp .env.example .env
```

编辑 `.env`，至少填入：

```bash
SUPABASE_SERVICE_ROLE_KEY=你的 Supabase service_role key
```

如果云 GPU 已启动，可以保留：

```bash
DEFAULT_SEGMENTATION_SERVICE_URL=http://117.50.216.65:9000/segment
```

如果云 GPU 没启动，也可以先不管。下载和整理任务仍可运行；语义分割任务需要模型服务在线。

## 4. 启动

```bash
docker compose up -d --build
```

查看状态：

```bash
docker compose ps
```

查看 Worker 日志：

```bash
docker compose logs -f worker
```

看到类似内容就说明 Worker 在等任务：

```text
StreetScope cloud worker started: ugreen-dh4300plus
Local API: http://backend:8000
```

## 5. 使用

打开公网网站：

```text
https://street-image.vercel.app
```

登录后提交云端任务。NAS Worker 会自动领取任务、执行、上传最终 ZIP。

## 6. 停止和更新

停止：

```bash
docker compose down
```

如果 NAS 是用 `git clone` 安装的，Mac 上修改并 push 到 GitHub 后，在 NAS 上运行：

```bash
cd /volume1/docker/street-image/deploy/nas
sh update.sh
```

它会自动执行：

- 拉取 GitHub 最新代码
- 保留 `deploy/nas/.env`
- 保留 Docker volume 里的历史缓存和成果
- 重建并重启 `backend` 和 `worker`

如果只是本地改了代码但还没 push，NAS 不会看到这些改动。流程是：

```text
Mac 修改代码 -> git push -> NAS sh update.sh -> 网站/Worker 使用新版本
```

如果 NAS 是 ZIP 上传方式，更新代码后重建：

```bash
docker compose up -d --build
```

## 7. 资源占用

常驻 Worker + 后端通常是低占用。主要消耗来自：

- 街景图片下载：占网络和硬盘
- ZIP 打包：短时间占 CPU
- 语义分割：不建议在 NAS CPU 上跑，建议调用云 GPU

NAS 上的数据会保存在 Docker volume `streetscope-data`，容器重启后不会丢。
