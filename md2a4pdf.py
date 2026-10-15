"""Markdown → A4 PDF：**图片一律按 URL 取**，跟在线转换服务同一套做法。

为什么要这样：像 markdowntoword.io 那类在线服务是在它自己的服务器上渲染的，它只能
**下载 Markdown 里引用的图片 URL**（"请使用无需登录即可访问的完整图片网址"），
所以本地文件路径、相对路径它一概读不到。这个工具把同一套做法搬到本地：

1. 把 Markdown 里的相对图片路径改写成 **URL**——要么指向内置的本地静态服务
   （默认，文件夹里的图片立刻就能用），要么指向 ``--base-url``（例如仓库的 raw 地址）；
2. 用 **HTTP 下载**这些图片（不是读本地文件），取不到就明确报错；
3. Markdown → HTML → A4 PDF：标题层级、列表、代码块、表格、引用都保留，
   图片按页宽缩放，分页交给渲染器按页面长度自动处理。

用法::

    python md2a4pdf.py 说明.md -o 说明.pdf                 # 本地图片：内置静态服务 + 出 PDF
    python md2a4pdf.py 说明.md --rewrite-md 说明.urls.md    # 只把图片改写成 URL（给在线服务用）
    python md2a4pdf.py 说明.md --base-url https://raw.githubusercontent.com/u/r/main/docs/
    python md2a4pdf.py 说明.md --check-images               # 只检查每个图片 URL 能不能取到

依赖：``pip install markdown-it-py aiohttp``，本机装 Chrome 或 Edge。全程离线（除非
``--base-url`` 指向了外网）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import http.server
import json
import mimetypes
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from navigation import add_navigation

BROWSERS = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp")

# 页面设置：# 图片按页宽缩放、按页面长度自动分页
CSS = """
@page { size: A4; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
  font-size: 10.5pt; line-height: 1.75; color: #1f2933; margin: 0;
  orphans: 3; widows: 3;
}
h1 { font-size: 21pt; margin: 0 0 6px; padding-bottom: 8px; border-bottom: 3px solid #d93025; }
h2 { font-size: 15pt; margin: 24px 0 10px; padding-bottom: 5px;
     border-bottom: 1px solid #d7dde5; break-after: avoid; page-break-after: avoid; }
h3 { font-size: 12.5pt; color: #14416e; margin: 17px 0 6px;
     break-after: avoid; page-break-after: avoid; }
h4 { font-size: 11pt; margin: 13px 0 4px; break-after: avoid; page-break-after: avoid; }
p { margin: 7px 0; }
strong { color: #10243c; }
a {
  color: #0b57d0; text-decoration: underline;
  text-decoration-color: #a9c0e0; text-decoration-thickness: 0.5pt; text-underline-offset: 2px;
}
hr { border: 0; border-top: 1px solid #e3e8ee; margin: 20px 0; }
ul, ol { margin: 7px 0 7px 22px; padding: 0; }
li { margin: 3px 0; }
li > ul, li > ol { margin: 3px 0 3px 18px; }
blockquote {
  margin: 10px 0; padding: 7px 13px; color: #33414f; background: #f6f9fc;
  border-left: 4px solid #9db4cd; border-radius: 0 4px 4px 0;
  break-inside: avoid; page-break-inside: avoid;
}
blockquote p { margin: 3px 0; }
table { border-collapse: collapse; width: 100%; margin: 9px 0 14px; font-size: 9.5pt; line-height: 1.6; }
th, td { border: 1px solid #ccd4de; padding: 4px 7px; vertical-align: top; text-align: left; }
th { background: #eef2f7; font-weight: 600; }
tr { break-inside: avoid; page-break-inside: avoid; }
table p { margin: 2px 0; }
td img { border: 0; margin: 2px; max-width: 100%; }
img { max-width: 100%; height: auto; display: block; margin: 12px auto;
      border: 1px solid #dfe4ea; border-radius: 4px; }
pre, code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9.5pt; }
code { background: #f2f4f7; padding: 1px 4px; border-radius: 3px; }
pre { background: #f7f9fb; border: 1px solid #e1e6ec; border-radius: 5px; padding: 8px 11px;
      white-space: pre-wrap; word-break: break-word;
      break-inside: avoid; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
h2 + p, h2 + table, h2 + blockquote, h3 + p, h3 + table { margin-top: 6px; }
"""

FOOTER = (
    '<div style="width:100%;font-family:sans-serif;font-size:8pt;color:#8a939c;'
    'text-align:center;padding:0 10mm;">'
    '第 <span class="pageNumber"></span> / 共 <span class="totalPages"></span> 页'
    "</div>"
)

MARGIN_IN = {"top": 0.63, "bottom": 0.71, "left": 0.59, "right": 0.59}

MD_IMAGE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)((?:\s+\"[^\"]*\")?\))")
HTML_IMAGE = re.compile(r'(<img\s[^>]*?src=")([^"]+)(")', re.IGNORECASE)


# ============================================================
# 图片引用：找出来、改写成 URL
# ============================================================

def is_remote(url: str) -> bool:
    return url.lower().startswith(("http://", "https://", "data:"))


def html_image_urls(html_text: str) -> list[str]:
    """渲染后的 HTML 里真正引用的图片 URL（去重、保序、去掉 data:）。

    从 HTML 取而不是从 Markdown 源码取：源码里代码块/行内 code 举例写的
    ``![](images/x.png)`` 不算图片；而且这样拿到的 URL 与渲染器要请求的完全一致。
    """
    import html as html_module

    urls: list[str] = []
    for _before, src, _after in HTML_IMAGE.findall(html_text):
        url = html_module.unescape(src)
        if not url.startswith("data:") and url not in urls:
            urls.append(url)
    return urls


INLINE_CODE = re.compile(r"`[^`]*`")


def rewrite(markdown_text: str, to_url) -> tuple[str, dict[str, str]]:
    """把**正文里**的图片引用交给 ``to_url(原始引用)`` 改写，返回（新文本, {原始 → URL}）。

    代码块（``` / ~~~ 围栏）与行内 ``code`` 原样保留——文档里举例写的
    ``![](images/x.png)`` 不该被当成真图片改写。
    """
    mapping: dict[str, str] = {}

    def convert(url: str) -> str:
        new = url if is_remote(url) else to_url(url)
        mapping[url] = new
        return new

    def convert_chunk(chunk: str) -> str:
        def md_repl(match: re.Match[str]) -> str:
            return match.group(1) + convert(match.group(2)) + match.group(3)

        def html_repl(match: re.Match[str]) -> str:
            return match.group(1) + convert(match.group(2)) + match.group(3)

        return HTML_IMAGE.sub(html_repl, MD_IMAGE.sub(md_repl, chunk))

    out: list[str] = []
    in_fence = False
    for line in markdown_text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        pieces: list[str] = []
        cursor = 0
        for match in INLINE_CODE.finditer(line):
            pieces.append(convert_chunk(line[cursor : match.start()]))
            pieces.append(match.group(0))
            cursor = match.end()
        pieces.append(convert_chunk(line[cursor:]))
        out.append("".join(pieces))
    return "".join(out), mapping


# ============================================================
# 本地静态服务：让「文件夹里的图片」也有 URL
# ============================================================

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """按目录提供文件的静态服务；不往终端刷访问日志。"""

    def log_message(self, *args, **kwargs) -> None:  # noqa: D102 - 静音
        pass


class LocalImages:
    """把 md 所在目录挂到 127.0.0.1 的一个随机端口上，图片就有了免登录的 URL。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        handler = partial(_QuietHandler, directory=str(self.root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self.port}/"

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.server.server_close()

    def url_for(self, relative: str) -> str:
        quoted = urllib.parse.quote(relative.replace("\\", "/"), safe="/")
        return f"http://127.0.0.1:{self.port}/{quoted}"


def port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


# ============================================================
# 取图片：走 HTTP（跟在线服务一样），取不到就报错
# ============================================================

@dataclass
class Fetched:
    url: str
    size: int
    data_uri: str


async def fetch_images(urls: list[str], *, timeout_s: float = 25.0, attempts: int = 3) -> list[Fetched]:
    """并发下载图片并转成 data URI；任何一张失败都报出来（PDF 里不留白）。"""
    import aiohttp

    results: list[Fetched] = []
    failures: list[str] = []
    if not urls:
        return results

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    semaphore = asyncio.Semaphore(6)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def one(url: str) -> None:
            if url.startswith("data:"):
                return
            async with semaphore:
                for attempt in range(1, attempts + 1):
                    try:
                        async with session.get(url, allow_redirects=True) as response:
                            if response.status != 200:
                                raise RuntimeError(f"HTTP {response.status}")
                            body = await response.read()
                        mime = response.headers.get("Content-Type", "").split(";")[0].strip()
                        if not mime.startswith("image/"):
                            mime = mimetypes.guess_type(url)[0] or "image/png"
                        encoded = base64.b64encode(body).decode("ascii")
                        results.append(Fetched(url, len(body), f"data:{mime};base64,{encoded}"))
                        print(f"  ✓ {len(body) // 1024:>5} KB  {url}")
                        return
                    except Exception as exc:  # noqa: BLE001 - 逐个重试，最后统一报
                        if attempt == attempts:
                            failures.append(f"{url}（{exc}）")
                            print(f"  ✗ 取不到：{url}（{exc}）", file=sys.stderr)
                        else:
                            await asyncio.sleep(0.6 * attempt)

        await asyncio.gather(*(one(url) for url in urls))

    if failures:
        raise SystemExit(
            "这些图片取不到，先修好再导（在线服务遇到同样的情况只会给你一个缺图的 PDF）：\n  "
            + "\n  ".join(failures)
        )
    return results


# ============================================================
# 渲染与打印
# ============================================================

def find_browser() -> Path:
    for path in BROWSERS:
        if path.exists():
            return path
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return Path(found)
    raise SystemExit("没找到 Chrome / Edge：装一个，或用 --browser 指定路径")


def to_html(markdown_text: str, title: str, css_path: Path | None) -> str:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    body = md.render(markdown_text)
    # --css 是**追加**在内置样式之后的小补丁（后写的优先），不用把整份样式抄一遍
    css = CSS
    if css_path:
        css = CSS + "\n/* ---- 自定义：%s ---- */\n" % css_path.name + css_path.read_text(
            encoding="utf-8"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>{css}</style></head><body>
{body}
</body></html>
"""


def swap_data_uris(html_text: str, data_uris: dict[str, str]) -> str:
    """把下载好的图片换成 data URI：渲染阶段就不再依赖网络了。

    同时把**原始 URL** 记在 ``data-md-src`` 上——样式表可以据此选中某几张图
    （``src`` 已经是 data URI，按文件名选不中了），例如：::

        img[data-md-src$="-states-flow.png"] { width: 82%; }
    """
    import html as html_module

    def swap(match: re.Match[str]) -> str:
        raw = html_module.unescape(match.group(2))
        data_uri = data_uris.get(raw)
        if not data_uri:
            return match.group(0)
        original = html_module.escape(raw, quote=True)
        return f'{match.group(1)}{data_uri}" data-md-src="{original}{match.group(3)}'

    return HTML_IMAGE.sub(swap, html_text)


async def print_pdf(browser: Path, html_path: Path, pdf_path: Path) -> None:
    """CDP 打印：A4、按页面长度自动分页、页脚页码。"""
    import aiohttp

    port = 9345
    while not port_is_free(port):
        port += 1
    profile = tempfile.mkdtemp(prefix="md2a4pdf-profile-")
    process = subprocess.Popen(
        [
            str(browser), "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
            f"--remote-debugging-port={port}", "--user-data-dir=" + profile,
            html_path.resolve().as_uri(),
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # 只认我们自己那个页面：端口上如果有别的（残留的）浏览器实例，别把它的页面打印出来
        ws_url = await wait_for_page(port, marker=html_path.parent.name)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, max_msg_size=0) as ws:
                await cdp(ws, 1, "Page.enable")
                await cdp(ws, 2, "Runtime.evaluate", {
                    "awaitPromise": True,
                    "expression": (
                        "Promise.all(Array.from(document.images).map(img => img.complete ? 1 :"
                        " new Promise(r => { img.onload = img.onerror = r; })))"
                        ".then(() => document.fonts.ready).then(() => true)"
                    ),
                })
                result = await cdp(ws, 3, "Page.printToPDF", {
                    "printBackground": True,
                    "preferCSSPageSize": True,
                    "displayHeaderFooter": True,
                    "headerTemplate": "<div></div>",
                    "footerTemplate": FOOTER,
                    **{f"margin{key.title()}": value for key, value in MARGIN_IN.items()},
                })
                try:  # 让浏览器自己退；退不掉下面还会强杀
                    await asyncio.wait_for(cdp(ws, 4, "Browser.close"), timeout=5)
                except Exception:  # noqa: BLE001 - 关不掉不影响结果
                    pass
        pdf_path.write_bytes(base64.b64decode(result["data"]))
    finally:
        _kill_browser(process)
        shutil.rmtree(profile, ignore_errors=True)


def _kill_browser(process: subprocess.Popen) -> None:
    """连子进程一起收掉：Chrome 的主进程常常早就退出了，光 terminate() 会留下实例。"""
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:  # pragma: no cover - 非 Windows 兜底
        process.kill()


async def wait_for_page(port: int, *, marker: str, timeout_s: float = 30.0) -> str:
    import aiohttp

    deadline = time.monotonic() + timeout_s
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(f"http://127.0.0.1:{port}/json/list") as response:
                    for target in await response.json():
                        if target.get("type") != "page":
                            continue
                        if marker not in urllib.parse.unquote(target.get("url", "")):
                            continue
                        if target.get("webSocketDebuggerUrl"):
                            return target["webSocketDebuggerUrl"]
            except Exception:  # noqa: BLE001 - 浏览器还没起来
                pass
            await asyncio.sleep(0.2)
    raise SystemExit("等不到浏览器页面（CDP 没起来，或者那个端口被别的浏览器实例占着）")


async def cdp(ws, message_id: int, method: str, params: dict | None = None) -> dict:
    await ws.send_json({"id": message_id, "method": method, "params": params or {}})
    while True:
        message = json.loads(await ws.receive_str())
        if message.get("id") == message_id:
            if "error" in message:
                raise SystemExit(f"{method} 失败：{message['error']}")
            return message.get("result", {})


# ============================================================
# 命令行
# ============================================================

def join_base(base: str, relative: str) -> str:
    return base.rstrip("/") + "/" + urllib.parse.quote(relative.lstrip("./").replace("\\", "/"), safe="/")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Markdown → A4 PDF（图片按 URL 下载，跟在线转换服务同一套做法）"
    )
    parser.add_argument("md", type=Path, help="Markdown 文件")
    parser.add_argument("-o", "--out", type=Path, default=None, help="输出 PDF（默认同名 .pdf）")
    parser.add_argument("--base-url", default=None,
                        help="把相对图片路径改写成这个前缀下的 URL（如仓库 raw 地址）")
    parser.add_argument("--rewrite-md", type=Path, default=None,
                        help="把改写好图片 URL 的 Markdown 另存一份（给在线服务粘贴用），不导 PDF")
    parser.add_argument("--check-images", action="store_true", help="只检查图片 URL 能不能取到")
    parser.add_argument("--css", type=Path, default=None,
                        help="自定义样式：**追加**在内置样式之后（后写的优先）")
    parser.add_argument("--keep-html", type=Path, default=None, help="保留中间 HTML")
    parser.add_argument("--browser", type=Path, default=None, help="Chrome/Edge 可执行文件")
    parser.add_argument(
        "--no-nav",
        action="store_true",
        help="不补目录跳转与书签（默认补：目录行可点 + 左侧两级大纲）",
    )
    args = parser.parse_args()

    md_path: Path = args.md.resolve()
    if not md_path.exists():
        raise SystemExit(f"没有这个文件：{md_path}")
    text = md_path.read_text(encoding="utf-8")

    local: LocalImages | None = None
    try:
        if args.base_url:
            rewritten, mapping = rewrite(text, lambda url: join_base(args.base_url, url))
            print(f"图片改写成 {args.base_url} 下的 URL（{len(mapping)} 条）")
        else:
            local = LocalImages(md_path.parent)
            base = local.__enter__()
            rewritten, mapping = rewrite(text, local.url_for)
            print(f"本地静态服务：{base}（{len(mapping)} 条图片引用）")

        if args.rewrite_md:
            args.rewrite_md.write_text(rewritten, encoding="utf-8")
            print(f"OK  {args.rewrite_md}（可直接粘进在线转换器；图片是 URL，服务自己会去下载）")
            return 0

        html = to_html(rewritten, md_path.stem, args.css)
        urls = html_image_urls(html)
        print(f"取图片（{len(urls)} 张）：")
        fetched = asyncio.run(fetch_images(urls))
        if args.check_images:
            print(f"OK  全部 {len(fetched)} 张都能取到")
            return 0

        html = swap_data_uris(html, {item.url: item.data_uri for item in fetched})
        pdf_path: Path = (args.out or md_path.with_suffix(".pdf")).resolve()
        browser = args.browser or find_browser()
        with tempfile.TemporaryDirectory(prefix="md2a4pdf-") as tmp:
            html_path = Path(tmp) / f"{md_path.stem}.html"
            html_path.write_text(html, encoding="utf-8")
            if args.keep_html:
                args.keep_html.write_text(html, encoding="utf-8")
            asyncio.run(print_pdf(browser, html_path, pdf_path))
        print(f"OK  {pdf_path}（{pdf_path.stat().st_size / 1024 / 1024:.2f} MB，A4，页脚有页码）")
        if not args.no_nav:
            print("  " + add_navigation(pdf_path, md_path).summary())
        return 0
    finally:
        if local is not None:
            local.__exit__()


if __name__ == "__main__":
    raise SystemExit(main())
