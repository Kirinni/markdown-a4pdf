"""给导出好的 PDF 补「目录能跳转」：目录行上的链接注记 + 左侧书签（大纲）。

为什么要在打印之后补：Chromium 的 `Page.printToPDF` 只是把 HTML **打印成页面**，
既不在目录文字上生成 /Link 注记，也不写大纲（点目录不跳、左侧没有导航）。

做法（不猜排版，全靠字号 + 文本搜索）：

1. **结构从 Markdown 取**（它才是权威）：`## / ###` 是标题，`## 目录` 下面那些
   `- [标签](#锚点)` 是目录条目；
2. **位置在 PDF 里找**：用 pdfium 搜文本，命中的字符盒是页面坐标、pt、页底为原点——
   正好是 PDF 注记要的坐标系，不用自己做坐标变换；
3. **靠字号区分"目录里的那一次"和"正文标题那一次"**：内置样式下正文 14px、页脚 10.7px，
   而 h3 16.67 / h2 20 / h1 28（Chromium 写进 PDF 的字号，单位是 px）。
   同一条文字出现两次时，小字那次是目录行（在上面盖链接），大字那次是标题（链接跳它）；
4. 最后用 pypdf 写回 PDF：目录行 → `/Link` 注记（整行可点），标题 → 两级书签。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from pypdf import PdfWriter
from pypdf.annotations import Link

HEADING_MIN_SIZE = 16.0  # px：h3 16.67 / h2 20 / h1 28；正文 14、页脚 10.7 不算标题
ROW_MARGIN_PT = 42.5  # 与内置样式的左右留白一致（0.59in = 42.5pt）：目录整行可点
PAD_PT = 2.0

HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
TOC_ENTRY_RE = re.compile(r"^\s*[-*]\s+\[(.+?)\]\(#(.+?)\)\s*$")


@dataclass
class Heading:
    level: int
    text: str
    page: int | None = None


@dataclass
class TocEntry:
    label: str
    heading: Heading | None = None
    page: int | None = None  # 目录行自己所在的页
    rect: tuple[float, float, float, float] | None = None


@dataclass
class Report:
    links: int = 0
    bookmarks: int = 0
    unmatched: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = f"目录可跳转：{self.links} 条链接 · {self.bookmarks} 个书签"
        if self.unmatched:
            text += "（没定位到：" + "、".join(self.unmatched[:3]) + "）"
        return text


def markdown_structure(text: str) -> tuple[list[Heading], list[TocEntry]]:
    """从 Markdown 里读出标题与目录条目（代码块里的不算）。"""
    headings: list[Heading] = []
    toc: list[TocEntry] = []
    in_toc = False
    in_fence = False

    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING_RE.match(line)
        if match:
            level, title = len(match.group(1)), match.group(2).strip()
            in_toc = title in ("目录", "Contents")
            headings.append(Heading(level=level, text=title))
            continue
        if in_toc:
            entry = TOC_ENTRY_RE.match(line)
            if entry:
                toc.append(TocEntry(label=entry.group(1).strip()))
            elif line.strip() and not line.startswith((" ", "\t", "-", "*")):
                in_toc = False  # 目录块结束（后面是正文或分隔线）

    return headings, toc


def _hits(textpage, needle: str) -> list[tuple[float, tuple[float, float, float, float]]]:
    """在某一页里找 needle：返回 [（字号, 字符盒）……]（盒是页面坐标、pt、页底为原点）。"""
    if not needle:
        return []
    searcher = textpage.search(needle)
    found: list[tuple[float, tuple[float, float, float, float]]] = []
    while True:
        hit = searcher.get_next()
        if hit is None:
            return found
        start, count = hit
        left, bottom, right, top = textpage.get_charbox(start)
        end_left, end_bottom, end_right, end_top = textpage.get_charbox(start + count - 1)
        size = float(pdfium_raw.FPDFText_GetFontSize(textpage.raw, start))
        found.append(
            (
                size,
                (
                    min(left, end_left),
                    min(bottom, end_bottom),
                    max(right, end_right),
                    max(top, end_top),
                ),
            )
        )


def _row_rect(
    box: tuple[float, float, float, float], width: float, height: float
) -> tuple[float, float, float, float]:
    """把命中盒扩成「整行可点」的矩形（左右按内容栏宽，上下留一点余量）。"""
    _left, bottom, _right, top = box
    left = max(0.0, min(ROW_MARGIN_PT - PAD_PT, width / 2))
    right = min(width, max(width - ROW_MARGIN_PT + PAD_PT, left + 1.0))
    return (left, max(0.0, bottom - PAD_PT), right, min(height, top + PAD_PT))


def locate(pdf_path: Path, headings: list[Heading], toc: list[TocEntry]) -> None:
    """在 PDF 里给每个标题找页码，给每个目录条目找它自己那一行。"""
    document = pdfium.PdfDocument(str(pdf_path))
    pages = [document[index].get_textpage() for index in range(len(document))]
    sizes = [
        (document[index].get_width(), document[index].get_height()) for index in range(len(document))
    ]

    def scan(needle: str) -> tuple[list[tuple[float, int, tuple]], list[tuple[float, int, tuple]]]:
        """（大字命中, 小字命中），都按页码顺序。"""
        big: list[tuple[float, int, tuple]] = []
        small: list[tuple[float, int, tuple]] = []
        for page_index, textpage in enumerate(pages):
            for size, box in _hits(textpage, needle):
                (big if size >= HEADING_MIN_SIZE else small).append((size, page_index, box))
        return big, small

    for heading in headings:
        big, _small = scan(heading.text)
        if not big and len(heading.text) > 8:  # 标题被折行时退一步：用前半段找
            big, _small = scan(heading.text[:8])
        if big:
            heading.page = big[0][1]

    for entry in toc:
        entry.heading = next(
            (
                item
                for item in headings
                if item.level >= 2 and (item.text == entry.label or item.text.startswith(entry.label))
            ),
            None,
        )
        big, small = scan(entry.label)
        if small:  # 目录里那一次（小字）就是要盖链接的那一行
            page_index = small[0][1]
            entry.page = page_index
            entry.rect = _row_rect(small[0][2], *sizes[page_index])
        elif big:  # 目录文字与标题不完全一致时：退回大字那一页
            page_index = big[0][1]
            entry.page = page_index
            entry.rect = _row_rect(big[0][2], *sizes[page_index])


def add_navigation(pdf_path: Path, markdown_path: Path) -> Report:
    """就地给 PDF 补链接注记与书签；返回这一次补了多少。"""
    headings, toc = markdown_structure(markdown_path.read_text(encoding="utf-8"))
    report = Report()
    if not toc:
        return report

    locate(pdf_path, headings, toc)

    writer = PdfWriter(clone_from=str(pdf_path))
    for entry in toc:
        if entry.rect is None or entry.heading is None or entry.heading.page is None:
            report.unmatched.append(entry.label)
            continue
        writer.add_annotation(
            page_number=entry.page,
            annotation=Link(rect=entry.rect, target_page_index=entry.heading.page),
        )
        report.links += 1

    parents: dict[int, object] = {}
    for heading in headings:
        if heading.level < 2 or heading.page is None:
            continue
        parent = parents.get(2) if heading.level == 3 else None
        item = writer.add_outline_item(heading.text, page_number=heading.page, parent=parent)
        if heading.level == 2:
            parents[2] = item
        report.bookmarks += 1

    with open(pdf_path, "wb") as handle:
        writer.write(handle)
    return report
