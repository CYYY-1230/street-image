# StreetScope Windows 本地使用与 Mac 同步

这套方案适合你的当前阶段：Windows 负责本地使用和保存历史，Mac 继续修 bug、改功能。网站不需要上线，也不需要便宜 Web 服务器。

## 一、整体结构

- Windows 本地运行前端和后端，浏览器打开 `http://127.0.0.1:5173/`。
- Windows 的历史项目、任务、下载图片、导出结果保存在 `backend/data/`。
- Mac 修代码后，只同步代码，不覆盖 Windows 的 `backend/data/`。
- 语义分割仍然使用云端 GPU 服务。GPU 关机时，路网、采样、下载、历史查看仍可用；只有“语义分割/指标计算”需要打开 GPU。

## 二、Windows 第一次安装

1. 安装 Python 3.11 或 3.12。
   安装时勾选 `Add python.exe to PATH`。

2. 安装 Node.js LTS。

3. 把整个项目文件夹复制到 Windows，例如：
   `D:\StreetScope`

4. 右键开始菜单，打开 PowerShell，进入项目目录：

   ```powershell
   cd D:\StreetScope
   ```

5. 首次安装依赖：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\windows_install.ps1
   ```

   如果你想把模型服务地址预填好，可以这样：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\windows_install.ps1 -DefaultSegmentationUrl "http://117.50.216.65:9000/segment"
   ```

## 三、Windows 日常启动

每次使用前运行：

```powershell
cd D:\StreetScope
powershell -ExecutionPolicy Bypass -File .\scripts\windows_start.ps1
```

脚本会打开两个命令窗口，并自动打开浏览器。两个命令窗口不要关，关掉就等于停止系统。

停止系统：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_stop.ps1
```

## 四、Mac 修 bug 后如何同步到 Windows

推荐方式是用一个 GitHub 私有仓库保存代码。

原则很简单：

- 同步代码：同步。
- 同步 `backend/data/`：不要同步。
- 同步 `frontend/node_modules/`、`backend/.venv/`：不要同步，这些在 Windows 本机安装。

本项目已经提供 `.gitignore`，默认会排除这些本地运行数据。

如果暂时不用 GitHub，也可以在 Mac 上把项目压缩后发到 Windows，但压缩前不要带这些目录：

- `backend/data/`
- `backend/.venv/`
- `frontend/node_modules/`
- `frontend/dist/`
- `SVC 街景爬取视频教程/`

Windows 收到新代码后，覆盖项目文件，但保留旧的 `backend/data/`。覆盖后再运行一次：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_install.ps1
```

然后正常启动即可。

## 五、如果想在同一个局域网里让别的电脑访问

默认只给 Windows 自己用，地址是：

```text
http://127.0.0.1:5173/
```

如果要让同一 Wi-Fi 下的其他电脑访问，需要把启动 host 改成 `0.0.0.0`，并配置前端 API 地址。这个属于下一步增强，当前先建议只在 Windows 本机使用，稳定性最高。

