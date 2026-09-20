import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

import bs4
import requests
from tqdm import tqdm

MAX_WORKERS = 5
OUTPUT_FORMAT = "txt"
MAX_RETRIES = 3
TIMEOUT = (10, 20)

# 保留原接口：此代码不会自动修复服务不可达的问题。
CONTENT_API = "http://rehaofan.jingluo.love/content"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
)


def request_with_retry(url, headers, parser, params=None):
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with requests.get(
                url,
                headers=headers,
                params=params,
                timeout=TIMEOUT,
            ) as response:
                response.raise_for_status()
                return parser(response)

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            last_error = exc
            print(f"请求失败（{attempt}/{MAX_RETRIES}）：{exc}")

            if attempt < MAX_RETRIES:
                time.sleep(attempt * 2)

    raise RuntimeError(
        f"请求连续失败 {MAX_RETRIES} 次：{url}"
    ) from last_error


def get_book(book_id):
    # 同一次任务复用一个匿名访客标识，无需账号登录。
    headers = {
        "User-Agent": USER_AGENT,
        "Cookie": (
            "novel_web_id="
            f"{random.randint(6 * 10**18, 8 * 10**18)}"
        ),
    }

    def parse(response):
        soup = bs4.BeautifulSoup(response.text, "html.parser")
        heading = soup.find("h1")

        if not heading or not heading.get_text(strip=True):
            raise ValueError(
                "未取得书名：页面可能异常，或小说 ID 不正确"
            )

        author = soup.select_one(".author-name-text")
        description = soup.select_one(".page-abstract-content")

        chapters = []
        seen = set()

        for item in soup.select("div.chapter-item"):
            link = item.find("a", href=True)

            if link is None:
                raise ValueError("章节目录中存在缺少链接的条目")

            chapter_id = (
                urlsplit(link["href"])
                .path.rstrip("/")
                .split("/")[-1]
            )

            if not re.fullmatch(r"[0-9]+", chapter_id):
                raise ValueError(
                    "章节链接格式异常，停止以避免遗漏章节"
                )

            if chapter_id in seen:
                continue

            seen.add(chapter_id)

            chapters.append({
                "id": chapter_id,
                "title": (
                    link.get_text(strip=True)
                    or f"第{len(chapters) + 1}章"
                ),
            })

        if not chapters:
            raise ValueError("章节列表为空，停止生成文件")

        return {
            "id": book_id,
            "name": heading.get_text(strip=True),
            "author": (
                author.get_text(strip=True)
                if author else "未知作者"
            ),
            "description": (
                description.get_text("\n", strip=True)
                if description else "无简介"
            ),
            "chapters": chapters,
        }

    return request_with_retry(
        f"https://fanqienovel.com/page/{book_id}",
        headers,
        parse,
    )


def down_text(chapter_id):
    def parse(response):
        data = response.json()

        if not isinstance(data, dict) or data.get("code") != 0:
            raise ValueError(
                "正文接口返回失败状态或不兼容的数据格式"
            )

        payload = data.get("data")

        if not isinstance(payload, dict):
            raise ValueError("正文接口缺少 data 对象")

        raw = payload.get("content")

        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("正文接口返回空内容")

        soup = bs4.BeautifulSoup(raw, "html.parser")

        for element in soup.select("header, footer, script, style"):
            element.decompose()

        content = "\n".join(
            "    " + line.strip()
            for line in soup.get_text("\n").splitlines()
            if line.strip()
        )

        if not content.strip():
            raise ValueError("清理 HTML 后正文为空")

        return content

    # 第三方正文接口不携带番茄站点的 Cookie。
    return request_with_retry(
        CONTENT_API,
        {"User-Agent": USER_AGENT},
        parse,
        params={"item_id": chapter_id},
    )


def safe_name(value):
    value = re.sub(
        r'[\\/:*?"<>|\x00-\x1f]',
        "_",
        value,
    ).strip(" .")

    return value[:80] or "未命名小说"


def save_book(book, contents, save_path, output_format):
    from html import escape

    folder = Path(save_path)
    folder.mkdir(parents=True, exist_ok=True)

    name = safe_name(book["name"])
    intro = (
        f"小说名：{book['name']}\n"
        f"作者：{book['author']}\n\n"
        f"简介：\n{book['description']}\n\n"
    )
    chapters = book["chapters"]

    if output_format == "chapter":
        folder = folder / name
        folder.mkdir(parents=True, exist_ok=True)

        (folder / "书籍信息.txt").write_text(
            intro,
            encoding="utf-8",
        )

        for index, (chapter, content) in enumerate(
            zip(chapters, contents),
            1,
        ):
            target = folder / (
                f"{index:04d}_{safe_name(chapter['title'])}.txt"
            )
            target.write_text(
                f"{chapter['title']}\n\n{content}\n",
                encoding="utf-8",
            )

        return folder

    target = folder / f"{name}.{output_format}"
    temporary = target.with_name(target.name + ".tmp")

    try:
        if output_format == "txt":
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(intro)

                for chapter, content in zip(chapters, contents):
                    stream.write(
                        f"{chapter['title']}\n\n{content}\n\n"
                    )

        else:
            from ebooklib import epub

            document = epub.EpubBook()
            document.set_identifier(book["id"])
            document.set_title(book["name"])
            document.set_language("zh-CN")
            document.add_author(book["author"])

            pages = []
            sections = [
                ("简介", book["description"])
            ] + [
                (chapter["title"], content)
                for chapter, content in zip(chapters, contents)
            ]

            for index, (title, text) in enumerate(sections):
                page = epub.EpubHtml(
                    title=title,
                    file_name=f"part_{index}.xhtml",
                    lang="zh-CN",
                )

                page.content = (
                    f"<h1>{escape(title)}</h1>"
                    + "".join(
                        f"<p>{escape(line.strip())}</p>"
                        for line in text.splitlines()
                        if line.strip()
                    )
                )

                document.add_item(page)
                pages.append(page)

            document.toc = tuple(pages)
            document.spine = ["nav"] + pages
            document.add_item(epub.EpubNcx())
            document.add_item(epub.EpubNav())

            epub.write_epub(str(temporary), document, {})

        # 完整写入后才替换正式文件。
        os.replace(temporary, target)

    finally:
        if temporary.exists():
            temporary.unlink()

    return target


def Run(book_id, save_path):
    book_id = str(book_id).strip()

    if not re.fullmatch(r"[0-9]+", book_id):
        raise ValueError(
            "小说 ID 只能填写数字，不要填写 page/ 或完整网址"
        )

    output_format = str(OUTPUT_FORMAT).lower()

    if output_format not in {"txt", "epub", "chapter"}:
        raise ValueError(
            "输出格式只能是 txt、epub 或 chapter"
        )

    workers = int(MAX_WORKERS)

    if not 1 <= workers <= 10:
        raise ValueError("线程数必须在 1 到 10 之间")

    if output_format == "epub":
        try:
            from ebooklib import epub
        except ImportError as exc:
            raise RuntimeError(
                "缺少 ebooklib，"
                "请先安装 requirements.txt 中的依赖"
            ) from exc

    book = get_book(book_id)
    chapters = book["chapters"]
    total = len(chapters)

    contents = [None] * total

    print(f"书名：{book['name']}；目录共 {total} 章")
    print("先检查第一章正文；失败后不会继续提交整本任务。")

    # 第一章失败会直接抛出异常，Actions 标记为失败。
    contents[0] = down_text(chapters[0]["id"])

    success_count = 1
    failed = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                down_text,
                chapters[index]["id"],
            ): index
            for index in range(1, total)
        }

        with tqdm(
            total=total,
            initial=1,
            desc="已处理章节",
        ) as progress:
            for future in as_completed(futures):
                index = futures[future]

                try:
                    # 按原目录位置存储，避免完成顺序影响章节顺序。
                    contents[index] = future.result()
                    success_count += 1

                except Exception as exc:
                    failed.append(index)
                    print(f"第 {index + 1} 章失败：{exc}")

                progress.set_postfix(
                    成功=success_count,
                    失败=len(failed),
                )
                progress.update(1)

    print(
        f"结果：成功 {success_count}/{total} 章，"
        f"失败 {len(failed)} 章"
    )

    if failed:
        numbers = ", ".join(
            str(index + 1)
            for index in sorted(failed)
        )

        raise RuntimeError(
            f"下载不完整，失败章节：{numbers}。"
            "本次不生成成品文件。"
        )

    target = save_book(
        book,
        contents,
        save_path,
        output_format,
    )

    print(f"下载完成：{target}")
    return True


def main():
    book_id = input("请输入小说数字 ID：").strip()
    save_path = (
        input("保存目录（默认 novel_output）：").strip()
        or "novel_output"
    )
    Run(book_id, save_path)


if __name__ == "__main__":
    main()
