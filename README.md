# markdown-a4pdf

**一句话**：Markdown → A4 PDF，**图片一律按 URL 下载**再嵌进 PDF——跟
[markdowntoword.io](https://markdowntoword.io/zh/tools/markdown-to-pdf) 那类在线服务同一套做法，
差别是它跑在你本机，所以**本地图片也能用**，而且不上传任何内容。

```powershell
python md2a4pdf.py 说明.md -o 说明.pdf
```

## 为什么这么做

在线服务是在**它自己的服务器上**渲染的，它只能去下载 Markdown 里引用的**图片 URL**：

> PDF 服务会下载 Markdown 中引用的图片，并按页面宽度缩放。请使用无需登录即可访问的完整图片网址；
> 本地文件路径和相对路径无法用于导出。

所以「图片是本地文件」这件事本身，在线服务办不到。这个工具把同一套流程搬到本地：

| 步骤 | 做什么 |
|---|---|
| 1. 改写 | 把相对图片路径改写成 **URL**：默认指向内置的静态服务（`http://127.0.0.1:<随机端口>/…`，md 所在目录就是网站根），也可以用 `--base-url` 指向公网地址（例如仓库的 raw 地址） |
| 2. 取图 | **用 HTTP 下载**这些图片（不是读本地文件），取不到就**明确报错**，不会悄悄给你一个缺图的 PDF |
| 3. 渲染 | Markdown → HTML → **A4 PDF**：标题层级、列表、代码块、表格、引用都保留；图片按页宽缩放；按页面长度自动分页；页脚有页码 |

顺带有个 `--rewrite-md`：把图片路径改写成完整 URL 后另存一份 Markdown——
**直接粘进在线服务**就能出图（前提是那些 URL 无需登录即可访问）。

## 安装

```powershell
pip install -r requirements.txt      # markdown-it-py + aiohttp
```

还需要本机装了 **Chrome 或 Edge**（用它的无头模式打印，工具会自动找；也可以用 `--browser` 指定）。

## 用法

```powershell
# ① 最常用：本地图片 + 出 PDF（图片经内置静态服务按 URL 取）
python md2a4pdf.py 说明.md -o 说明.pdf

# ② 只把图片改写成 URL 另存一份，粘到在线转换器里用
python md2a4pdf.py 说明.md --rewrite-md 说明.urls.md

# ③ 图片已经放在公网（例如仓库 raw）：直接按 URL 取，导 PDF
python md2a4pdf.py 说明.md --base-url https://raw.githubusercontent.com/<user>/<repo>/main/docs/

# ④ 只体检：逐个检查图片 URL 能不能取到（上在线服务之前先跑一遍）
python md2a4pdf.py 说明.md --check-images

# ⑤ 微调样式 / 留中间产物排查
python md2a4pdf.py 说明.md -o 说明.pdf --css styles\tight-galleries.css --keep-html out\说明.html
```

其他参数：`--browser <chrome.exe>`。

## 样式微调

`--css` 指定的是**追加**在内置样式之后的小补丁（后写的优先），不用把整份样式抄一遍。
`styles/tight-galleries.css` 是个例子：

```css
/* 一张图里竖排了好几张卡片、整张接近两页高时，缩到 82% 宽，跨页留白会小很多 */
img[data-md-src$="-states-flow.png"],
img[data-md-src$="-states-attention.png"] { width: 82%; }
```

注意这里用的是 `data-md-src` 而不是 `src`：图片下载后已经被换成 data URI，按文件名选不中了，
工具会把**原始 URL** 记在 `data-md-src` 上供样式表挑选。

## 实测

一份 24 页、16 张图（约 1.5 MB 图片）的中文使用说明：

- 本地模式出 PDF：约 10 秒，2.1 MB，图片全部嵌入、页脚「第 N / 共 24 页」；
- 同一份内容 `--rewrite-md` + 在线服务：只要图片 URL 公网可访问，出图效果与本地一致。

## 已知边界

- **URL 必须无需登录可访问**：内网地址、要带 token 的私有 raw 链接，在线服务那侧同样取不到；
  本地模式没有这个限制（静态服务只监听 `127.0.0.1`）。
- 代码块与行内 `code` 里写的 `![](images/x.png)` **不会**被改写（那是举例，不是真图片）。
- 中文字体用系统的「Microsoft YaHei UI / 微软雅黑」，换机器请确认已装。
- 走 CDP 打印，会在后台起一个无窗口的 Chrome 进程；跑完自动关掉。
