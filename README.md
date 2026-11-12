# Omni RenderDoc MCP

Omni RenderDoc MCP 是一款用于 RenderDoc 扩展的 MCP 服务器，可以接入 Agent 辅助图形调试和逆向分析。在 [RenderDocMCP](https://github.com/halby24/RenderDocMCP) 的基础上增加了一些功能。

## 安装

### 前置准备

- RenderDoc 1.47+
- Python 3.11
- [uv](https://docs.astral.sh/uv/getting-started/installation/)。

### RenderDoc 扩展

在本仓库根目录运行以下 PowerShell 命令，安装 RenderDoc 扩展。

```powershell
uv sync --python 3.11
uv run omni-renderdoc-mcp install --extensions-dir "$env:APPDATA\qrenderdoc\extensions"
```

`--extensions-dir` 应指向 RenderDoc 的 `extensions` 目录。若使用默认 `qrenderdoc` 目录，第二行可简化为 `uv run omni-renderdoc-mcp install`。自定义配置目录的示例如下：

```powershell
uv run omni-renderdoc-mcp install --extensions-dir "D:\RenderDocConfig\extensions"
```

重启 RenderDoc，在扩展管理器中启用 **Omni RenderDoc MCP**。

### Codex 桌面版

在本仓库根目录运行下面的命令，复制输出的绝对路径：

```powershell
(Resolve-Path ".\.venv\Scripts\omni-renderdoc-mcp.exe").Path
```

打开 Codex 桌面版的 **Settings → MCP servers → Add server**，填写名称 `omni-renderdoc-mcp`，类型选择 **STDIO**，将复制的路径设为启动命令，并将 `serve` 设为启动参数。保存并点击 **Restart**。在对话输入 `/mcp` 检查连接。

### Codex CLI 与 Claude Code

在本仓库根目录执行对应命令：

```powershell
$server = (Resolve-Path ".\.venv\Scripts\omni-renderdoc-mcp.exe").Path
codex mcp add omni-renderdoc-mcp -- $server serve
# 或使用 Claude Code
claude mcp add --transport stdio --scope user omni-renderdoc-mcp -- $server serve
```

分别用 `codex mcp list` 或 `claude mcp list` 查看配置，重开对话后调用 `get_capture_status` 检查实际连接。

## 工具列表

| 用途 | MCP 工具 |
| --- | --- |
| 抓帧与连接 | `list_captures`、`open_capture`、`get_capture_status` |
| 事件与 GPU 耗时 | `get_frame_summary`、`get_draw_calls`、`get_draw_call_details`、`get_action_timings` |
| 查找绘制 | `find_draws_by_shader`、`find_draws_by_texture`、`find_draws_by_resource` |
| 资源与管线 | `get_buffer_contents`、`get_texture_info`、`get_texture_data`、`get_shader_info`、`get_pipeline_state` |
| 导出 | `export_frame_meshes`、`export_pass_shaders`、`export_blender_scene`、`rebuild_blender_shaders`、`export_unity_hdrp` |

## 使用方式

> 直接与 Agent 对话即可。

### 样例

在 Codex 或 Claude Code 中输入：

> 调用 Omni RenderDoc MCP 打开 `C:\Captures\current-capture.rdc`，列出 Pass，再把选定 draw 的着色器、网格和纹理导出到 `D:\Exports\current-capture`。

网格会以 ASCII FBX 导出。纹理与 Unity 着色器属性的对应关系记录在清单的 `draw_texture_bindings` 和 `unity_texture_bindings` 中。

### Unity HDRP 工程导出

`export_unity_hdrp` 把选定 draw/pass 的抓帧姿态网格、贴图和相机写入独立 Unity HDRP 工程，并可调用本机 Unity 批处理生成 `Assets/CaptureScene.unity` 和 `preview.png`。每次导出创建新的 `unity-hdrp-*` 目录，保留 `provenance.json`、`result.json`、`export_result.json` 与 Unity 日志。

```json
{
  "output_directory": "D:/Exports/capture",
  "event_ids": [100, 105],
  "unity_executable": "C:/Program Files/Unity/Hub/Editor/6000.3.7f1/Editor/Unity.exe",
  "editor_version": "6000.3.7f1",
  "hdrp_version": "17.3.0",
  "render_scale": 50
}
```

通过 `get_draw_calls` 选择实际可见的场景 draw，或以 `pass_event_id` 代替 `event_ids`。不要把阴影、深度预通道、全屏或 UI 绘制混入场景。`material_bindings` 可按 event ID 显式指定 `base_color`/`normal` 的 `{ "set": 1, "binding": 4 }`，`uv_attributes` 可指定 UV varying。贴图名称不可用时自动绑定仅使用启发式判断，需检查预览。

可选 `lighting` 支持 `directional_lights` 数组（每项包含相机局部坐标下的光线发射 `direction`、`color_linear`、`lux`、`provenance` 和 `intensity_is_approximation`），以及 `exposure_ev100`、`tonemapping`（`None`/`ACES`/`Neutral`）。未提供时使用标记为近似的双方向灯；从 shader 常量解释的光源方向和颜色也应记录证据来源，不能把未知照度作为准确还原。

已有几何 staging bundle 可用绝对 `staged_manifest` 路径离线重建；它与所有抓帧选择参数互斥，不需要 Blender 或活动抓帧。默认目标为 Unity 6000.3 / HDRP 17.3，需要可用的本机 Unity 许可证与对应包缓存或包下载连接。`run_unity: false` 只写入工程，`build_command.json` 保存后续构建命令；此时还没有生成场景或验证渲染。打开项目后也可运行 **Tools → Omni RenderDoc → Rebuild captured HDRP scene**。

默认 `shader_mode: "hdrp_lit"` 是相机空间静态姿态与 HDRP/Lit 材质的可编辑重建，自定义 shader、灯光、后处理和法线属于近似。

导出工具只根据调用参数、当前抓帧元数据和 shader 反射工作，不包含指定游戏或指定帧的事件列表、资源表、相机参数、材质特判或导出预设。具体抓帧的复现配置属于用户工程，不随 MCP 工具包分发。尺寸和投影来自捕获数据或显式输入；缺少必要数据时不会套用示例帧参数。

需要运行抓帧中的真实片元着色器时，使用 `shader_mode: "original_fragment"`，并通过 `spirv_cross_executable` 指定 SPIRV-Cross，或将它加入 PATH。该模式将 Vulkan SPIR-V 转为 ShaderLab/HLSL，使用原始 post-VS 插值数据、常量缓冲、存储缓冲和原生纹理，保留 MRT、深度、模板、混合及片元 UAV 写入。选择时应包含相关的深度预通道、表面、光照和全屏 draw；已选择的前置输出会实时连接到后续输入。未选择的生产者仍使用明确记录的抓帧快照，compute 阶段尚不重放。运行目标目前是 Windows/D3D11，不支持的状态或缺失的绑定会报错。

```json
{
  "output_directory": "D:/Exports/original-fragments",
  "event_ids": [100, 105, 110],
  "shader_mode": "original_fragment",
  "spirv_cross_executable": "D:/Tools/spirv-cross.exe",
  "run_unity": true
}
```

原始片元模式生成 `Assets/RecoveredCaptureScene.unity`、`recovered-preview.png` 和 `recovered-result.json`。shader 始终按抓帧分辨率运行，`render_scale` 仅缩放预览。默认显示最后一个 draw 的第一个颜色附件，可用 `final_target` 指定已生成的资源 ID；只有 shader 自行输出 sRGB 编码到线性纹理时才设置 `final_is_srgb: true`。离线 `staged_manifest` 必须包含原始 shader evidence；`supplementary_buffers` 可补充已抓取的原生缓冲索引。工程保存捕获到的 render-pass 清除值，并明确报告缺少初始化数据的附件。

`final_flip_y` 默认为 `false`，仅用于最终显示方向转换，保留原始 shader 和中间纹理坐标。最终目标、颜色编码和显示方向均应根据当前抓帧显式设置，不按帧号或资源编号推断。

`timeout_seconds` 默认 1200 秒，分别用于抓帧数据导出和 Unity 构建，每个阶段拥有独立的等待时限，并非两阶段的总时限。导出大量原生 shader/纹理时应增加此值；抓帧导出还会保留已经配置的更长 bridge 超时，且不会修改其他并发请求的超时。使用 `staged_manifest` 时跳过抓帧导出阶段。

原始片元模式可通过 `shader_evidence_limits` 调整抓帧数据预算：`max_total_bytes` 默认 2 GiB、`max_resource_bytes` 默认 256 MiB、`max_subresources_per_view` 默认 4096，值必须为正整数。较大抓帧可显式设置 `"shader_evidence_limits": {"max_total_bytes": 12884901888, "max_resource_bytes": 536870912, "max_subresources_per_view": 8192}`（12 GiB / 512 MiB / 8192）。预算不足会记录未导出的资源，原始 shader 工程随后会明确报错；不会用替代材质补齐。此参数仅适用于直接抓帧，不能与离线 `staged_manifest` 合用。

两种模式均不会恢复原 Unity 工程层级、动画或导入设置。原始片元模式保留编译后方程，生成的 ShaderLab/HLSL 可以直接编辑；丢失的原始源码命名和材质编辑界面尚未恢复。`status: complete` 表示场景与文件验证通过，并不表示画面逐像素一致。

## 许可证

[MIT](LICENSE)
