# StreetScope 公网协作方案

目标效果：

- Mac 继续开发，代码推到 GitHub。
- Vercel 自动部署公网网页。
- Supabase 保存账号、项目历史、任务队列和最终 ZIP。
- Windows Worker 在你需要生产数据时打开，领取 Supabase 任务并在本地执行。
- 云端 GPU 只在语义分割时开，Worker 调用模型服务。

## 一、你需要提供给我的东西

1. GitHub 仓库地址。
2. Supabase 项目信息：
   - `SUPABASE_URL`
   - `SUPABASE_ANON_KEY`
   - `SUPABASE_SERVICE_ROLE_KEY`
3. Vercel 项目是否已经连上 GitHub。
4. 如果要发给别人测试：测试账号邮箱。

注意：`SUPABASE_SERVICE_ROLE_KEY` 只能放在 Windows Worker 或服务器环境里，不能写进前端、GitHub 公开仓库或 Vercel 前端环境变量。

## 二、Supabase 初始化

1. 打开 Supabase 项目。
2. 进入 SQL Editor。
3. 把 `supabase/schema.sql` 的全部内容复制进去运行。
4. 确认生成：
   - `streetscope_projects`
   - `streetscope_tasks`
   - Storage bucket: `streetscope-artifacts`

## 三、Windows Worker 第一次配置

在 Windows 项目目录里：

```powershell
copy .\worker\.env.example .\worker\.env
notepad .\worker\.env
```

填入：

```text
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
STREETSCOPE_LOCAL_API_BASE=http://127.0.0.1:8000
```

然后启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_worker_start.ps1 -StartLocalApp
```

`-StartLocalApp` 会顺手启动本地前后端。后续如果本地前后端已经开着，可以不加它。

## 四、任务 payload 设计

下载任务：

```json
{
  "kind": "download",
  "payload": {
    "download_request": {
      "...": "这里放原 /api/download-task 的请求体"
    }
  }
}
```

下载后继续语义分割：

```json
{
  "kind": "download_then_metrics",
  "payload": {
    "download_request": {
      "...": "这里放原 /api/download-task 的请求体"
    },
    "metrics_request": {
      "...": "这里放原 /api/metrics-task 的请求体，但不用填 source_download_task_id"
    }
  }
}
```

Worker 会自动：

1. 领取 queued 任务。
2. 调用本地 FastAPI 创建下载任务。
3. 等待下载完成。
4. 如果需要，继续创建语义分割任务。
5. 导出最终 ZIP。
6. 上传到 Supabase Storage。
7. 把任务状态改为 completed。

## 五、还需要继续实现的部分

当前已经完成云端队列和 Worker 底座。下一步需要把前端接入 Supabase：

- Supabase 登录/退出。
- 项目保存到 `streetscope_projects`。
- 点击“开始任务”时，公网模式写入 `streetscope_tasks`，而不是直接请求本地后端。
- 云端任务中心读取 Supabase 状态和最终 ZIP。

这一步完成后，体验就是：

```text
Mac 改代码 -> GitHub -> Vercel 自动更新网站
Windows 双击 Worker -> 自动领取公网网页创建的任务 -> 生产数据 -> 上传结果
```

