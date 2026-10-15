"""给导出好的 PDF 补上「Markdown 里写的链接都能点」：目录整行、正文/表格里的行内链接、
以及外链，外加左侧书签（大纲）。

为什么要在打印之后补：Chromium 的 `Page.printToPDF` 只是把 HTML **打印成页面**，
既不生成 /Link 注记，也不写大纲（点目录不跳、左侧没有导航）。

做法（结构从 Markdown 取，位置在 PDF 里找，两边都不靠猜）：

1. **结构从 Markdown 取**（它才是权威）：
   - `## / ###` 是标题，并算出它的锚点（GitHub 规则：去掉标点、空格换连字符）；
   - `## 目录` 里那些 `- [标签](#锚点)` 是目录条目（整行做成链接）；
   - 其余位置出现的 `[文字](#锚点)` 是行内链接，`[文字](http…)` 是外链。
2. **位置在 PDF 里搜**：pdfium 的文本搜索给出命中的**字符盒**（页面坐标、pt、页底为原点），
   正好是 PDF 注记要的坐标系，不用自己做坐标变换。
3. **靠字号区分"目录里那一次"与"正文标题那一次"**（同一句话在 PDF 里出现两次）：
   内置样式下正文 14px、页脚 10.7px，而 h3 16.67 / h2 20 / h1 28。
4. **行内链接靠上下文定位**：可见文字常常很短（比如表格里的 `[6.3](#…)` 只渲染成 `6.3`），
   所以搜的是「同一行紧挨着的文字 + 可见文字」（如 `看 6.3`）；pdfium 会把
   `看 6.3` 与 `看6.3` 视为同一命中，命中后再在匹配文本里切出可见文字那一段取盒。
5. 最后用 pypdf 写回：目录行/行内链接 → `/Link` 注记（外链写成 URI），标题 → 两级书签。
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
CONTEXT_CHARS = 8  # 行内链接定位用的上下文长度

HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
TOC_ENTRY_RE = re.compile(r"^\s*[-*]\s+\[(.+?)\]\(#(.+?)\)\s*$")
INLINE_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")
STRIP_FOR_CONTEXT = str.maketrans({char: "" for char in "*`|>"})


@dataclass
class Heading:
    level: int
    text: str
    anchor: str
    page: int | None = None


@dataclass
class Entry:
    """一条要做成可点的东西：目录条目、行内链接或外链。"""

    label: str  # 可见文字
    target: str  # 锚点（# 开头）或 URL
    context: str = ""  # 用来在 PDF 里唯一定位它的上下文
    page: int | None = None
    rect: tuple[float, float, float, float] | None = None
    target_page: int | None = None
    whole_row: bool = False  # 目录条目：整行可点


@dataclass
class Report:
    toc_links: int = 0
    inline_links: int = 0
    external_links: int = 0
    bookmarks: int = 0
    unmatched: list[str] = field(default_factory=list)

    def summary(self) -> str:
        text = (
            f"链接：目录 {self.toc_links} · 正文/表格 {self.inline_links}"
            f" · 外链 {self.external_links} · 书签 {self.bookmarks}"
        )
        if self.unmatched:
            text += "（没定位到：" + "、".join(self.unmatched[:3]) + "）"
        return text


def anchor_of(title: str) -> str:
    """标题 → 锚点（跟 GitHub 一样：小写、去标点、空格换连字符）。"""
    text = title.strip().lower()
    text = re.sub(r"[^\w\s\-]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


def _plain(line: str) -> str:
    return line.translate(STRIP_FOR_CONTEXT)


def markdown_structure(text: str) -> tuple[list[Heading], list[Entry]]:
    """从 Markdown 里读出标题、目录条目、行内链接与外链（代码块里的都不算）。"""
    headings: list[Heading] = []
    entries: list[Entry] = []
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
            headings.append(Heading(level=level, text=title, anchor=anchor_of(title)))
            continue

        if in_toc:
            entry = TOC_ENTRY_RE.match(line)
            if entry:
                entries.append(
                    Entry(
                        label=entry.group(1).strip(),
                        target="#" + entry.group(2).strip(),
                        whole_row=True,
                    )
                )
            elif line.strip() and not line.startswith((" ", "\t", "-", "*")):
                in_toc = False  # 目录块结束
            continue

        for link in INLINE_RE.finditer(line):
            label, target = link.group(1).strip(), link.group(2).strip()
            if label.startswith("!"):
                continue
            before = _plain(line[: link.start()]).rstrip()
            after = _plain(line[link.end() :]).lstrip()
            entries.append(
                Entry(
                    label=label,
                    target=target,
                    context=(before[-CONTEXT_CHARS:] if before else "") or after[:CONTEXT_CHARS],
                )
            )

    return headings, entries


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
        found.append(
            (
                float(pdfium_raw.FPDFText_GetFontSize(textpage.raw, start)),
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
    """把命中盒扩成「整行可点」的矩形（目录条目用）。"""
    _left, bottom, _right, top = box
    left = max(0.0, min(ROW_MARGIN_PT - PAD_PT, width / 2))
    right = min(width, max(width - ROW_MARGIN_PT + PAD_PT, left + 1.0))
    return (left, max(0.0, bottom - PAD_PT), right, min(height, top + PAD_PT))


def _link_rect(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    left, bottom, right, top = box
    return (left - PAD_PT, bottom - PAD_PT, right + PAD_PT, top + PAD_PT)


class Locator:
    """拿着 pdfium 的页面，按关键字找页码与字符盒。"""

    def __init__(self, pdf_path: Path) -> None:
        self.document = pdfium.PdfDocument(str(pdf_path))
        self.textpages = [self.document[i].get_textpage() for i in range(len(self.document))]
        self.sizes = [
            (self.document[i].get_width(), self.document[i].get_height())
            for i in range(len(self.document))
        ]

    def scan(self, needle: str) -> tuple[list[tuple[int, float, tuple]], list[tuple[int, float, tuple]]]:
        """（大字命中, 小字命中）：[(页, 字号, 盒)……]。"""
        big: list[tuple[int, float, tuple]] = []
        small: list[tuple[int, float, tuple]] = []
        for page_index, textpage in enumerate(self.textpages):
            for size, box in _hits(textpage, needle):
                (big if size >= HEADING_MIN_SIZE else small).append((page_index, size, box))
        return big, small

    def locate_substring(
        self, key: str, label: str
    ) -> tuple[int, tuple[float, float, float, float]] | None:
        """搜 key，再在命中文本里切出 label 那一段，返回它的页码与盒。"""
        for page_index, textpage in enumerate(self.textpages):
            searcher = textpage.search(key)
            hit = searcher.get_next()
            if hit is None:
                continue
            start, count = hit
            matched = textpage.get_text_range(start, count)
            offset = matched.find(label)
            if offset < 0:
                offset = 0
            first = start + offset
            last = first + max(len(label) - 1, 0)
            left, bottom, right, top = textpage.get_charbox(first)
            end_left, end_bottom, end_right, end_top = textpage.get_charbox(last)
            return (
                page_index,
                (
                    min(left, end_left),
                    min(bottom, end_bottom),
                    max(right, end_right),
                    max(top, end_top),
                ),
            )
        return None

    def close(self) -> None:
        self.document.close()


def locate(pdf: Path, headings: list[Heading], entries: list[Entry]) -> None:
    locator = Locator(pdf)
    try:
        for heading in headings:
            big, _small = locator.scan(heading.text)
            if not big and len(heading.text) > 8:  # 标题折行时退一步：用前半段
                big, _small = locator.scan(heading.text[:8])
            if big:
                heading.page = big[0][0]

        by_anchor = {heading.anchor: heading for heading in headings}
        for entry in entries:
            if entry.target.startswith("#"):
                target = by_anchor.get(entry.target.lstrip("#").lower())
                if target is not None:
                    entry.target_page = target.page

            candidates = [] if entry.whole_row else [entry.context + entry.label, entry.label]
            if entry.whole_row:
                found = locator.scan(entry.label)
                if found[1]:  # 目录里那一次（小字）
                    page_index = found[1][0][0]
                    entry.page = page_index
                    entry.rect = _row_rect(found[1][0][2], *locator.sizes[page_index])
                    continue
                candidates = [entry.label]

            for key in candidates:
                if not key:
                    continue
                spot = locator.locate_substring(key, entry.label)
                if spot is None:
                    continue
                page_index, box = spot
                entry.page = page_index
                entry.rect = _link_rect(box)
                break
    finally:
        locator.close()


def add_navigation(pdf_path: Path, markdown_path: Path) -> Report:
    """就地给 PDF 补链接注记与书签；返回这一次补了多少。"""
    headings, entries = markdown_structure(markdown_path.read_text(encoding="utf-8"))
    report = Report()
    if not headings:
        return report

    locate(pdf_path, headings, entries)

    writer = PdfWriter(clone_from=str(pdf_path))
    for entry in entries:
        if entry.rect is None:
            report.unmatched.append(entry.label or entry.target)
            continue
        if entry.target.startswith("#"):
            if entry.target_page is None:
                report.unmatched.append(entry.label)
                continue
            writer.add_annotation(
                page_number=entry.page,
                annotation=Link(rect=entry.rect, target_page_index=entry.target_page),
            )
            if entry.whole_row:
                report.toc_links += 1
            else:
                report.inline_links += 1
        else:
            writer.add_annotation(
                page_number=entry.page,
                annotation=Link(rect=entry.rect, url=entry.target),
            )
            report.external_links += 1

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
