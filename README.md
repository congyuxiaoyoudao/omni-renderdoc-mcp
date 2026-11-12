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
| 导出 | `export_frame_meshes`、`export_pass_shaders`、`export_blender_scene`、`export_unity_hdrp` |

## 使用方式

> 直接与 Agent 对话即可。

### 样例

在 Codex 或 Claude Code 中输入：

> 调用 Omni RenderDoc MCP 打开 `C:\Captures\current-capture.rdc`，列出 Pass，再把选定 draw 的着色器、网格和纹理导出到 `D:\Exports\current-capture`。

网格会以 ASCII FBX 导出。纹理与 Unity 着色器属性的对应关系记录在清单的 `draw_texture_bindings` 和 `unity_texture_bindings` 中。

### Blender 工程导出

通过 `export_blender_scene` 将选定 draw/pass 导出为包含网格、相机和材质的 `.blend` 工程，并生成预览。默认将支持的片元着色器重建为可编辑节点，未支持的部分会明确报告；同时传入 `geometry_blend` 和 `shader_evidence_directory`，还可为已有工程离线重建材质。

### Unity HDRP 工程导出

通过 `export_unity_hdrp` 将选定 draw/pass 导出为 Unity HDRP 工程，并调用本机 Unity 生成场景和预览。默认使用 HDRP/Lit 近似材质；设置 `shader_mode: "original_fragment"` 并提供 SPIRV-Cross，可转换原始 Vulkan 片元着色器，在 Windows/D3D11 下重放捕获画面。

## 许可证

[MIT](LICENSE)
