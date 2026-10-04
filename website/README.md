# MClaude 官网

以纸质杂志为灵感的中文官网：衬线标题、细线分栏、暖白底色和原创 SVG 插画。
纯 HTML、CSS 与 JavaScript，无构建步骤或第三方运行依赖。

## 本地预览

在仓库根目录运行（需要 Node.js）：

```powershell
node website/serve.mjs
```

打开 http://127.0.0.1:4173 。按 Ctrl+C 停止服务。

## 文件

- `dist/index.html`：页面内容和语义结构。
- `dist/style.css`：响应式样式，适配移动设备与减少动态效果偏好。
- `dist/app.js`：键盘可访问的演示/系统选项卡、复制安装命令及反馈。
- `dist/assets/`：原创工作室插画与 favicon。

当前仅提供项目源码与本地预览，不进行线上发布。
终端内容为工作流示意，不会执行命令或请求模型 API。所有产品描述依据项目 README。
