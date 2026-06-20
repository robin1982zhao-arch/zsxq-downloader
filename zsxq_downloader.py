#!/usr/bin/env python3
"""
知识星球批量文档下载器 — 支持断点续传 + 磁盘索引优化

用法:
    python zsxq_downloader.py              # 全流程
    python zsxq_downloader.py --fetch      # 仅获取话题元数据
    python zsxq_downloader.py --download   # 仅下载（使用缓存的话题）
    python zsxq_downloader.py --rebuild-progress  # 从磁盘重建进度文件

安装:
    pip install curl_cffi
"""

import io
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
    sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, "reconfigure") else None

from curl_cffi import requests as cffi_requests

# ============================================================
#  CONFIG
# ============================================================

TOKEN = os.environ.get("ZSXQ_TOKEN", "")
PREFERRED_GROUP_NAME = "精益智造资料库"
SAVE_DIR = Path("C:/Users/admin/Desktop/zsxq_downloads")
PAGE_SIZE = 30

DOWNLOAD_IMAGES = True
DOWNLOAD_FILES = True
SAVE_MARKDOWN = True

# 请求间隔
REQUEST_DELAY = 5.0
FILE_RESOLVE_DELAY = 3.0
FILE_DOWNLOAD_DELAY = 1.0

# 每 N 页冷却
COOLDOWN_PAGES = 8
COOLDOWN_SECONDS = 120

TOPICS_CACHE = "zsxq_topics_cache.json"
PROGRESS_FILE = "download_progress.json"
FETCH_STATE_FILE = "zsxq_fetch_state.json"

API_BASE = "https://api.zsxq.com/v2"

# ============================================================
#  工具函数
# ============================================================


def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" ._")[:120]


def load_json(path: str, default=None):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default if default is not None else {}


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_headers(token: str) -> dict:
    return {
        "Cookie": f"zsxq_access_token={token}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Referer": "https://wx.zsxq.com/dweb2/index/",
        "X-Request-Id": str(uuid.uuid4()),
    }


# ============================================================
#  磁盘索引 — 节省 API 额度
# ============================================================


def build_disk_index(group_dir: Path) -> set:
    """
    扫描下载目录，返回所有已下载文件的 (topic_folder_name, filename) 集合。
    启动时调用，用于跳过 API 解析。
    """
    index = set()
    files_dir = group_dir / "files"
    if not files_dir.exists():
        return index

    for topic_dir in files_dir.iterdir():
        if not topic_dir.is_dir():
            continue
        for f in topic_dir.iterdir():
            if f.is_file():
                # 标准化：去掉扩展名差异、safe_filename 截断差异
                fname = f.name
                fname_no_ext = os.path.splitext(fname)[0].lower().strip()
                index.add((topic_dir.name, fname))
                index.add((topic_dir.name, fname_no_ext))  # 无扩展名 key
    return index


def reconcile_progress_from_disk(topics: list, group_dir: Path, progress: dict) -> int:
    """
    扫描磁盘，将磁盘上已有但不在 progress 中的文件标记为已下载。
    返回新标记的数量。
    """
    disk_index = build_disk_index(group_dir)
    if not disk_index:
        return 0

    new_marked = 0
    for t in topics:
        items = extract_items(t)
        for item in items:
            file_id = item.get("file_id", "")
            url = item.get("url", "")
            progress_key = file_id or url
            if not progress_key:
                continue
            if progress.get(progress_key):
                continue  # 已标记

            # 计算预期路径
            topic_name = safe_filename(item.get("title", item.get("topic_id", "")))
            name = item.get("name", "")
            ext = item.get("ext", "")
            if name:
                fname = safe_filename(name)
                if ext and not fname.endswith(ext):
                    fname += ext
            else:
                fname = str(item.get("topic_id") or "unknown") + ext

            # 检查磁盘索引：精确匹配或模糊匹配
            found = False
            if topic_name in {d[0] for d in disk_index}:
                # 精确匹配
                if (topic_name, fname) in disk_index:
                    found = True
                else:
                    # 模糊匹配（文件名截断差异）
                    fname_no_ext = os.path.splitext(fname)[0].lower()
                    for disk_topic, disk_fname in disk_index:
                        if disk_topic == topic_name:
                            disk_no_ext = os.path.splitext(disk_fname)[0].lower()
                            # 比较前 60 字符（safe_filename 截断）
                            if fname_no_ext[:60] == disk_no_ext[:60]:
                                found = True
                                break

            if found:
                progress[progress_key] = True
                new_marked += 1

    return new_marked


# ============================================================
#  API（带重试）
# ============================================================


def api_get(path: str, token: str, params: dict = None, max_retries: int = 8) -> dict:
    """API GET 请求，带指数退避重试"""
    params = params or {}
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    url = f"{API_BASE}{path}?{qs}" if qs else f"{API_BASE}{path}"

    for attempt in range(max_retries):
        try:
            resp = cffi_requests.get(url, headers=make_headers(token), timeout=30, impersonate="chrome120")
            data = resp.json()
            code = data.get("code")
            if code and code != 0:
                raise Exception(f"API 错误 (code={code}): {data.get('info', '')}")
            return data.get("resp_data", data)
        except Exception as e:
            err = str(e)
            if "13607" in err:
                raise Exception(f"DAILY_LIMIT: {err}") from None
            is_1059 = "1059" in err
            if attempt == max_retries - 1:
                raise
            if is_1059:
                wait = min(30 * (2 ** attempt), 600)
                print(f"      ⚠ 限流(1059)，等待 {wait}s 后重试 ({attempt + 1}/{max_retries})...")
            else:
                wait = min(5 * (2 ** attempt), 60)
                print(f"      ⚠ 请求失败: {err[:60]}，{wait}s 后重试...")
            time.sleep(wait)
    raise Exception("unreachable")


def get_groups(token: str) -> list[dict]:
    data = api_get("/groups", token)
    return (data or {}).get("groups", [])


# ============================================================
#  话题获取（断点续传）
# ============================================================


def fetch_all_topics(group_id: str, token: str) -> list[dict]:
    """获取星球所有话题（分页 + 断点续传 + 限流重试）"""
    cache = load_json(TOPICS_CACHE, {})
    cache_key = str(group_id)
    cached = cache.get(cache_key, [])
    seen_ids = {t["topic_id"] for t in cached}
    all_topics = list(cached)

    state = load_json(FETCH_STATE_FILE, {})
    gs = state.get(cache_key, {})
    end_time = gs.get("last_end_time", None)
    pages_done = gs.get("pages", 0)

    if not end_time and cached:
        oldest = min((t.get("create_time", "") for t in cached if t.get("create_time")), default="")
        if oldest:
            end_time = oldest
            pages_done = 0

    if cached:
        print(f"\n📋 缓存: {len(cached)} 个话题 | 已翻 {pages_done} 页")
        if end_time:
            print(f"   从上次位置继续...")

    page = pages_done
    while True:
        page += 1
        params = {"count": PAGE_SIZE, "scope": "all"}
        if end_time:
            params["end_time"] = end_time

        try:
            data = api_get(f"/groups/{group_id}/topics", token, params)
        except Exception as e:
            print(f"    ❌ 第 {page} 页失败: {e}")
            print(f"    已保存进度，稍后可重新运行继续")
            break

        topics = data.get("topics", [])
        if not topics:
            print(f"    第 {page} 页无数据，获取完毕")
            break

        new = 0
        for t in topics:
            tid = t.get("topic_id")
            if tid and tid not in seen_ids:
                seen_ids.add(tid)
                all_topics.append(t)
                new += 1

        print(f"    第 {page} 页: {new} 新增 (累计 {len(all_topics)})")

        cache[cache_key] = all_topics
        save_json(TOPICS_CACHE, cache)

        end_time = topics[-1].get("create_time", "")
        state[cache_key] = {"last_end_time": end_time, "pages": page}
        save_json(FETCH_STATE_FILE, state)

        if new == 0:
            break
        if not end_time:
            break

        if page % COOLDOWN_PAGES == 0:
            print(f"    🧊 {page} 页完成，休息 {COOLDOWN_SECONDS}s...")
            time.sleep(COOLDOWN_SECONDS)
        else:
            time.sleep(REQUEST_DELAY)

    return all_topics


# ============================================================
#  文件提取 & 下载
# ============================================================


def extract_items(topic: dict) -> list[dict]:
    """提取话题中的图片和文件"""
    items = []
    tid = topic.get("topic_id", "")
    talk = topic.get("talk") or topic.get("question") or {}
    text = talk.get("text", "")
    title = talk.get("title", "") or (text[:40].strip() if text else tid)

    # 图片
    for img in talk.get("images", []):
        for size in ("large", "original", "thumbnail"):
            url = (img.get(size, {}) or {}).get("url", "")
            if url:
                url = url.replace("\\u0026", "&")
                ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
                items.append({"url": url, "type": "image", "topic_id": tid, "title": title, "ext": ext})
                break

    # 文件
    for f in talk.get("files", []):
        file_id = f.get("file_id")
        name = f.get("name", "")
        if file_id and name:
            ext = os.path.splitext(name)[1] or ""
            items.append({
                "type": "file", "topic_id": tid, "title": title,
                "name": name, "ext": ext, "file_id": file_id, "url": None,
            })

    # <e> 标签附件
    for href, href_title in re.findall(
        r'<e[^>]*type="(?:file|attachment)"[^>]*href="([^"]*)"[^>]*title="([^"]*)"[^>]*/?>', text
    ):
        href = unquote(href).replace("\\u0026", "&")
        parsed = urlparse(href)
        items.append({
            "url": href, "type": "file", "topic_id": tid, "title": title,
            "name": unquote(href_title), "ext": os.path.splitext(parsed.path)[1] or "",
        })

    return items


def resolve_one_file_url(file_id: str, token: str) -> str | None:
    """解析单个文件的下载 URL（带重试 + 退避）。返回 None 表示失败，返回 '__DAILY_LIMIT__' 表示日限额。"""
    for retry in range(5):
        try:
            data = api_get(f"/files/{file_id}/download_url", token, max_retries=3)
            url = (data.get("download_url") or "").replace("\\u0026", "&")
            return url if url else None
        except Exception as e:
            err = str(e)
            if "DAILY_LIMIT" in err:
                return "__DAILY_LIMIT__"
            if "1059" in err:
                wait = min(15 * (2 ** retry), 120)
                if retry < 4:
                    time.sleep(wait)
            elif retry < 4:
                time.sleep(5 * (retry + 1))
    return None


def download_file(url: str, save_path: Path, token: str) -> bool:
    """下载文件到本地"""
    if save_path.exists():
        return True
    save_path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            resp = cffi_requests.get(url, headers=make_headers(token), timeout=120, stream=True, impersonate="chrome120")
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return True
        except Exception:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    return False


def save_markdown(topic: dict, folder: Path):
    """保存话题为 Markdown"""
    talk = topic.get("talk") or topic.get("question") or {}
    text = talk.get("text", "")
    if not text:
        return
    title = talk.get("title", "") or text[:40].strip()
    author = (talk.get("owner", {}) or {}).get("name", "")
    create_time = topic.get("create_time", "")

    cleaned = re.sub(r"<e[^>]*>", "", text)
    cleaned = re.sub(r"</e>", "", cleaned)

    md = f"# {safe_filename(title)}\n\n- 作者: {author}\n- 时间: {create_time}\n\n{cleaned}"
    filepath = folder / f"{safe_filename(title)}.md"
    folder.mkdir(parents=True, exist_ok=True)
    filepath.write_text(md, encoding="utf-8")


# ============================================================
#  Main
# ============================================================


def main():
    mode_fetch_only = "--fetch" in sys.argv
    mode_download_only = "--download" in sys.argv
    mode_rebuild_progress = "--rebuild-progress" in sys.argv

    print("=" * 60)
    print("  知识星球批量下载器")
    if mode_fetch_only:
        print("  模式: 仅获取话题")
    elif mode_download_only:
        print("  模式: 仅下载")
    elif mode_rebuild_progress:
        print("  模式: 从磁盘重建进度文件")
    print("=" * 60)

    token = TOKEN.strip()

    # ── 验证 token（rebuild-progress 模式不需要联网）──
    if not mode_rebuild_progress:
        print("\n验证登录状态...")
        try:
            groups = get_groups(token)
        except Exception as e:
            print(f"❌ 登录失败: {e}")
            return

        if not groups:
            print("❌ 未找到任何星球")
            return

        groups = [
            {"group_id": g["group_id"], "name": g["name"],
             "owner": (g.get("owner", {}) or {}).get("name", "")}
            for g in groups
        ]
        print(f"成功！已加入 {len(groups)} 个星球\n")

        for i, g in enumerate(groups):
            print(f"  [{i+1}] {g['name']} (星主: {g['owner']})")

        choice = 0
        if PREFERRED_GROUP_NAME:
            for i, g in enumerate(groups):
                if PREFERRED_GROUP_NAME in g["name"]:
                    choice = i
                    break

        group = groups[choice]
        group_id = group["group_id"]
        group_name = group["name"]
    else:
        # rebuild-progress: 从缓存读取星球信息
        cache = load_json(TOPICS_CACHE, {})
        if not cache:
            print("❌ 无话题缓存，请先运行 --fetch")
            return
        group_id = list(cache.keys())[0]
        group_name = PREFERRED_GROUP_NAME
        groups = [{"group_id": group_id, "name": group_name}]
        group = groups[0]

    print(f"\n已选择: {group_name}")

    # ── Phase 1: 获取话题 ──
    if not mode_download_only and not mode_rebuild_progress:
        topics = fetch_all_topics(group_id, token)
        cache = load_json(TOPICS_CACHE, {})
        cache[str(group_id)] = topics
        save_json(TOPICS_CACHE, cache)
        print(f"\n✅ 话题获取完成: 共 {len(topics)} 个")
    else:
        cache = load_json(TOPICS_CACHE, {})
        topics = cache.get(str(group_id), [])
        if not topics:
            print("❌ 无缓存话题，请先运行 --fetch")
            return
        print(f"\n📋 使用缓存话题: {len(topics)} 个")

    if mode_fetch_only:
        print("   --fetch 模式完成，话题已缓存")
        return

    # ── Phase 2: 提取 & 下载 ──
    print("\n📦 提取文件信息...")
    all_items = []
    for t in topics:
        all_items.extend(extract_items(t))

    direct_items = [i for i in all_items if i.get("url")]
    file_id_items = [i for i in all_items if i["type"] == "file" and i.get("file_id") and not i.get("url")]

    images = [i for i in all_items if i["type"] == "image"]
    print(f"   🖼  图片: {len(images)} 个")
    print(f"   📎 附件: {len(direct_items) + len(file_id_items)} 个 (直链: {len(direct_items)}, 需解析: {len(file_id_items)})")

    # ── 进度（用 file_id 或 url 作为 key）──
    progress = load_json(PROGRESS_FILE, {})
    if isinstance(progress, list):
        progress = {u: True for u in progress}

    group_dir = SAVE_DIR / safe_filename(group_name)

    # ════════════════════════════════════════════════════════
    #  🔑 关键优化：从磁盘重建进度，避免浪费 API 额度
    # ════════════════════════════════════════════════════════
    if mode_rebuild_progress:
        # 清空进度，完全从磁盘重建
        progress = {}
        print("\n🔄 从磁盘重建进度文件...")

    new_from_disk = reconcile_progress_from_disk(topics, group_dir, progress)
    if new_from_disk > 0:
        save_json(PROGRESS_FILE, progress)
        print(f"   ✅ 从磁盘索引新增 {new_from_disk} 条进度标记（无需API调用）")
    else:
        print(f"   ✅ 进度文件与磁盘一致")

    if mode_rebuild_progress:
        print(f"\n✅ 进度文件已重建: {len(progress)} 条标记")
        return

    def download_with_progress(item, label, item_idx, total_count):
        """解析 URL + 下载单个文件，返回 True/False/__DAILY_LIMIT__"""
        name = item.get("name", "")
        ext = item.get("ext", "")
        topic_name = safe_filename(item.get("title", item.get("topic_id", "")))
        file_id = item.get("file_id", "")

        progress_key = file_id or item.get("url", "")
        if not progress_key:
            return False

        if progress.get(progress_key):
            return True  # 进度中已标记

        # 确定保存路径
        save_dir = group_dir / label / topic_name
        if name:
            fname = safe_filename(name)
            if ext and not fname.endswith(ext):
                fname += ext
        else:
            fname = str(item.get("topic_id") or "unknown") + ext
        save_path = save_dir / fname

        if save_path.exists():
            progress[progress_key] = True
            return True

        # ═══════════════════════════════════════════════════
        #  🔑 第二层磁盘检查：扫描目录内是否有相似文件
        #  避免 safe_filename 截断导致路径不匹配
        # ═══════════════════════════════════════════════════
        if save_dir.exists():
            fname_no_ext = os.path.splitext(fname)[0].lower()
            for existing in save_dir.iterdir():
                if existing.is_file():
                    existing_no_ext = os.path.splitext(existing.name)[0].lower()
                    if fname_no_ext[:60] == existing_no_ext[:60]:
                        progress[progress_key] = True
                        return True

        # 解析 URL（API 调用 — 只有真正需要时才触发）
        url = item.get("url")
        if not url and file_id:
            url = resolve_one_file_url(file_id, token)
            if url == "__DAILY_LIMIT__":
                print("\n⛔ 日下载限额已用尽，今日停止")
                return "__DAILY_LIMIT__"
            if not url:
                return False

        # 下载
        save_dir.mkdir(parents=True, exist_ok=True)
        ok = False
        for attempt in range(3):
            try:
                resp = cffi_requests.get(url, headers=make_headers(token), timeout=120,
                                         stream=True, impersonate="chrome120")
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                with open(save_path, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
                ok = True
                break
            except Exception:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))

        if ok:
            progress[progress_key] = True
        return ok

    # ── 下载所有文件（逐个解析+下载）──
    all_file_items = file_id_items + direct_items
    if all_file_items and DOWNLOAD_FILES:
        total = len(all_file_items)
        print(f"\n⬇  下载附件 ({total} 个)...")
        new = skip = fail = 0
        already_verified = 0

        for i, item in enumerate(all_file_items):
            fname = safe_filename(item.get("name", str(item.get("topic_id", ""))))[:60]
            file_id = item.get("file_id", "")
            progress_key = file_id or item.get("url", "")

            # 已下载跳过
            if progress.get(progress_key):
                skip += 1
                if skip % 100 == 1:
                    print(f"    [{i+1}/{total}] (已跳过 {skip}...)")
                continue

            print(f"    [{i+1}/{total}] {fname}", end="", flush=True)

            result = download_with_progress(item, "files", i, total)
            if result == "__DAILY_LIMIT__":
                save_json(PROGRESS_FILE, progress)
                print(f"\n⛔ 今日下载限额已用尽。已下载 {new} 个新文件，进度已保存。")
                print(f"   明天运行 python zsxq_downloader.py --download 继续")
                return
            elif result:
                new += 1
                print(" ✓")
            else:
                fail += 1
                print(" ✗ (限流或失败)")

            # 定期保存进度
            if (i + 1) % 20 == 0:
                save_json(PROGRESS_FILE, progress)

            time.sleep(FILE_RESOLVE_DELAY if file_id else FILE_DOWNLOAD_DELAY)

        save_json(PROGRESS_FILE, progress)
        print(f"    附件 完成: 新增 {new} / 跳过 {skip} / 失败 {fail}")

    elif DOWNLOAD_FILES:
        print("\n   (无文件可下载)")

    # ── 图片下载 ──
    if images and DOWNLOAD_IMAGES:
        print(f"\n⬇  下载图片 ({len(images)} 个)...")
        new = skip = fail = 0
        for i, item in enumerate(images):
            progress_key = item.get("url", "")
            fname = str(item.get("topic_id", "")) + item.get("ext", ".jpg")
            topic_name = safe_filename(item.get("title", str(item.get("topic_id", ""))))
            save_path = group_dir / "images" / topic_name / fname

            if progress.get(progress_key) or save_path.exists():
                skip += 1
                continue

            print(f"    [{i+1}/{len(images)}] {fname[:60]}", end="", flush=True)

            ok = False
            for attempt in range(3):
                try:
                    resp = cffi_requests.get(item["url"], headers=make_headers(token),
                                             timeout=60, stream=True, impersonate="chrome120")
                    resp.raise_for_status()
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(save_path, "wb") as f:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk)
                    ok = True
                    break
                except Exception:
                    if attempt < 2:
                        time.sleep(2)

            if ok:
                progress[progress_key] = True
                new += 1
                print(" ✓")
            else:
                fail += 1
                print(" ✗")

            if (i + 1) % 20 == 0:
                save_json(PROGRESS_FILE, progress)
            time.sleep(FILE_DOWNLOAD_DELAY)

        save_json(PROGRESS_FILE, progress)
        print(f"    图片 完成: 新增 {new} / 跳过 {skip} / 失败 {fail}")

    # ── Markdown ──
    if SAVE_MARKDOWN:
        print(f"\n📝 保存 Markdown...")
        md_dir = group_dir / "markdown"
        count = 0
        for t in topics:
            if (t.get("talk") or t.get("question") or {}).get("text"):
                save_markdown(t, md_dir)
                count += 1
        print(f"    保存了 {count} 个")

    # ── 统计 ──
    total_files = sum(1 for _ in group_dir.rglob("*") if _.is_file())
    total_size = sum(_.stat().st_size for _ in group_dir.rglob("*") if _.is_file())
    print(f"\n{'=' * 60}")
    print(f"  ✅ 完成！星球: {group_name}  话题: {len(topics)}")
    print(f"  图片: {len(images)}")
    print(f"  文件总数: {total_files}  总大小: {total_size / 1024 / 1024:.1f} MB")
    print(f"  位置: {group_dir.resolve()}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
