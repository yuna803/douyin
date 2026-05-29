"""
抖音视频/图集下载器
支持 v.douyin.com 短链接，自动解析并下载无水印视频/图片。
"""

import os
import re
import sys
import json
import time
import urllib.parse
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

import httpx
import execjs

# ===================== 常量 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "mp4")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

A_BOGUS_PATH = os.path.join(BASE_DIR, "a-bogus.js")

COMMON_HEADER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
    ),
}

DOUYIN_DETAIL_URL = (
    "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web"
    "&aweme_id={}&pc_client_type=1&version_code=190500"
    "&version_name=19.5.0&cookie_enabled=true"
    "&screen_width=1344&screen_height=756&browser_language=zh-CN"
    "&browser_platform=Win32&browser_name=Firefox"
    "&browser_version=118.0&browser_online=true"
    "&engine_name=Gecko&engine_version=109.0"
    "&os_name=Windows&os_version=10&cpu_core_num=16"
    "&device_memory=&platform=PC"
)

DOUYIN_VIDEO_URL = (
    "https://aweme.snssdk.com/aweme/v1/play/"
    "?video_id={}&ratio=1080p&line=0"
)

URL_TYPE_MAP = {
    2: "image",
    4: "video",
    68: "image",
}

# 备用 API（不需要 cookie 和 X-Bogus）
FALLBACK_API = "https://api.xingzhige.com/API/douyin/?url={}"


# ===================== 工具函数 =====================
def check_nodejs() -> bool:
    """检查 Node.js 是否可用（X-Bogus 签名需要）"""
    try:
        result = subprocess.run(
            ["node", "--version"], capture_output=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return result.returncode == 0
    except Exception:
        return False


def generate_x_bogus(url: str, user_agent: str) -> str:
    """使用 a-bogus.js 生成 X-Bogus 签名"""
    query = urllib.parse.urlparse(url).query
    with open(A_BOGUS_PATH, "r", encoding="utf-8") as f:
        js_code = f.read()
    abogus = execjs.compile(js_code).call("generate_a_bogus", query, user_agent)
    return url + "&a_bogus=" + abogus


def resolve_short_link(short_url: str) -> str | None:
    """跟随 v.douyin.com 短链接重定向，返回完整 URL"""
    headers = {
        **COMMON_HEADER,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cache-Control": "no-cache",
    }
    try:
        # 先禁止重定向，从 Location 头获取目标 URL
        resp = httpx.get(short_url, headers=headers, follow_redirects=False, timeout=15)
        location = resp.headers.get("location", "")
        if location and ("douyin.com" in location or "iesdouyin.com" in location):
            return location

        # 如果返回了 302/301 但没有 location 头，尝试跟随重定向
        if resp.status_code in (301, 302, 307, 308):
            resp2 = httpx.get(short_url, headers=headers, follow_redirects=True, timeout=15)
            return str(resp2.url)

        # 可能是 JS 重定向页面，尝试从响应体中提取
        body = resp.text
        m = re.search(r"(https?://(?:www\.)?douyin\.com/[^\s\"']+)", body)
        if m:
            return m.group(1)

        raise RuntimeError(f"短链接解析失败: 状态码 {resp.status_code}，无法获取重定向地址")
    except Exception as e:
        raise RuntimeError(f"短链接解析失败: {e}")


def extract_id_from_url(url: str) -> tuple[str | None, str | None]:
    """从完整 URL 提取视频/图集 ID 和类型"""
    # /video/742839201873.../ 或 /note/742839201873.../
    m = re.search(r"(?:video|note)/(\d+)", url)
    if m:
        return m.group(1), "normal"

    # /share/video/742839201873.../ 或 /share/note/742839201873.../
    m = re.search(r"share/(?:video|note)/(\d+)", url)
    if m:
        return m.group(1), "normal"

    # /share/slides/... 类型（图集）
    if "share/slides" in url:
        return None, "slides"

    # /discover?modal_id=742839201873...
    m = re.search(r"modal_id=(\d+)", url)
    if m:
        return m.group(1), "normal"

    return None, None


# ===================== 核心下载逻辑 =====================
def fetch_detail_with_signature(dou_id: str, cookie: str) -> dict:
    """通过官方 API 获取作品详情（需要 cookie 和 X-Bogus）"""
    api_url = DOUYIN_DETAIL_URL.format(dou_id)
    api_url = generate_x_bogus(api_url, COMMON_HEADER["User-Agent"])

    headers = {
        **COMMON_HEADER,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "referer": f"https://www.douyin.com/video/{dou_id}",
        "cookie": cookie,
    }

    resp = httpx.get(api_url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data["aweme_detail"]


def fetch_detail_fallback(short_url: str) -> dict | None:
    """通过备用 API 获取作品详情（不需要 cookie）"""
    try:
        resp = httpx.get(FALLBACK_API.format(short_url), headers=COMMON_HEADER, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 200:
            return data.get("data", {})
    except Exception:
        pass
    return None


def download_file(url: str, filename: str, progress_callback=None) -> str:
    """下载文件到 DOWNLOAD_DIR，返回本地路径"""
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    headers = {**COMMON_HEADER, "referer": "https://www.douyin.com/"}

    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))

        with open(filepath, "wb") as f:
            downloaded = 0
            for chunk in resp.iter_bytes(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total > 0:
                    progress_callback(downloaded, total)

    return filepath


def download_video(dou_id: str, cookie: str, progress_callback=None) -> tuple[str, str, str, str]:
    """
    下载抖音视频，返回 (本地路径, 标题, 作者, 封面URL)
    使用官方 API（需要 cookie）
    """
    detail = fetch_detail_with_signature(dou_id, cookie)
    aweme_type = URL_TYPE_MAP.get(detail.get("aweme_type"), "video")
    title = detail.get("desc", "无标题")
    author = detail.get("author", {}).get("nickname", "未知作者")
    cover = detail.get("video", {}).get("cover", {}).get("url_list", [""])[0]

    if aweme_type == "video":
        video_uri = detail["video"]["play_addr"]["uri"]
        video_url = DOUYIN_VIDEO_URL.format(video_uri)
        filename = f"douyin_{dou_id}.mp4"
        filepath = download_file(video_url, filename, progress_callback)
        return filepath, title, author, cover

    elif aweme_type == "image":
        # 图集：下载所有图片
        images = detail.get("images", [])
        saved_files = []
        for i, img in enumerate(images):
            img_url = img["url_list"][0]  # 无水印
            ext = img_url.split(".")[-1].split("?")[0] or "jpg"
            filename = f"douyin_{dou_id}_{i+1}.{ext}"
            filepath = download_file(img_url, filename)
            saved_files.append(filepath)
        return ";".join(saved_files), title, author, cover

    raise RuntimeError(f"不支持的类型: {aweme_type}")


def download_fallback(short_url: str, progress_callback=None) -> tuple[str, str, str, str]:
    """使用备用 API 下载（不需要 cookie）"""
    data = fetch_detail_fallback(short_url)
    if not data:
        raise RuntimeError("备用 API 解析失败")

    item = data.get("item", {})
    author_info = data.get("author", {})
    title = item.get("title", "无标题")
    author = author_info.get("name", "未知作者")
    cover = item.get("cover", "")

    # 判断类型
    item_type = data.get("jx", {}).get("type", "视频")
    video_url = item.get("video_play_url", [""])[0] if item.get("video_play_url") else ""

    if video_url:
        item_id = data.get("jx", {}).get("item_id", int(time.time()))
        filename = f"douyin_{item_id}.mp4"
        filepath = download_file(video_url, filename, progress_callback)
        return filepath, title, author, cover

    # 图集
    images = item.get("images", [])
    if images:
        saved_files = []
        item_id = data.get("jx", {}).get("item_id", int(time.time()))
        for i, img_url in enumerate(images):
            ext = img_url.split(".")[-1].split("?")[0] or "jpg"
            filename = f"douyin_{item_id}_{i+1}.{ext}"
            filepath = download_file(img_url, filename)
            saved_files.append(filepath)
        return ";".join(saved_files), title, author, cover

    raise RuntimeError("未找到可下载的内容")


# ===================== Tkinter GUI =====================
class DouyinDownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("抖音下载器")
        self.root.geometry("600x450")
        self.root.resizable(False, False)

        # 设置样式
        style = ttk.Style()
        style.theme_use("clam")

        self._build_ui()

    def _build_ui(self):
        # 标题
        title_label = ttk.Label(
            self.root, text="抖音视频/图集下载器",
            font=("Microsoft YaHei", 16, "bold")
        )
        title_label.pack(pady=(20, 5))

        subtitle = ttk.Label(
            self.root, text="支持 v.douyin.com 短链接，自动解析无水印下载",
            font=("Microsoft YaHei", 9), foreground="gray"
        )
        subtitle.pack(pady=(0, 5))

        # Node.js 状态
        if check_nodejs():
            node_label = ttk.Label(
                self.root, text="✅ Node.js 已就绪（支持官方 API 高画质下载）",
                font=("Microsoft YaHei", 8), foreground="green"
            )
        else:
            node_label = ttk.Label(
                self.root, text="⚠️ 未检测到 Node.js，将使用备用 API（无需 Cookie 也可下载）",
                font=("Microsoft YaHei", 8), foreground="orange"
            )
        node_label.pack(pady=(0, 10))

        # Cookie 输入
        cookie_frame = ttk.LabelFrame(self.root, text="Cookie（可选，填写后可下载更高画质）", padding=8)
        cookie_frame.pack(fill="x", padx=20, pady=(0, 10))

        self.cookie_var = tk.StringVar()
        cookie_entry = ttk.Entry(cookie_frame, textvariable=self.cookie_var, show="*")
        cookie_entry.pack(fill="x")

        # URL 输入
        url_frame = ttk.LabelFrame(self.root, text="抖音链接", padding=8)
        url_frame.pack(fill="x", padx=20, pady=(0, 10))

        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(url_frame, textvariable=self.url_var)
        url_entry.pack(fill="x")
        url_entry.bind("<Return>", lambda e: self._start_download())

        # 按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(pady=(5, 10))

        self.download_btn = ttk.Button(
            btn_frame, text="⬇ 下载", command=self._start_download, width=15
        )
        self.download_btn.pack(side="left", padx=5)

        self.open_btn = ttk.Button(
            btn_frame, text="📂 打开下载目录", command=self._open_dir, width=15
        )
        self.open_btn.pack(side="left", padx=5)

        # 进度条
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            self.root, variable=self.progress_var, maximum=100, length=500
        )
        self.progress_bar.pack(pady=(5, 5))

        self.progress_label = ttk.Label(self.root, text="", font=("Microsoft YaHei", 9))
        self.progress_label.pack()

        # 信息展示
        info_frame = ttk.LabelFrame(self.root, text="作品信息", padding=8)
        info_frame.pack(fill="both", expand=True, padx=20, pady=(5, 10))

        self.info_text = tk.Text(
            info_frame, height=8, wrap="word", state="disabled",
            font=("Microsoft YaHei", 9)
        )
        self.info_text.pack(fill="both", expand=True, side="left")

        scrollbar = ttk.Scrollbar(info_frame, command=self.info_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.info_text.config(yscrollcommand=scrollbar.set)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(
            self.root, textvariable=self.status_var,
            relief="sunken", anchor="w", font=("Microsoft YaHei", 8)
        )
        status_bar.pack(fill="x", side="bottom")

    def _log_info(self, text: str):
        self.info_text.config(state="normal")
        self.info_text.insert("end", text + "\n")
        self.info_text.see("end")
        self.info_text.config(state="disabled")

    def _set_status(self, text: str):
        self.status_var.set(text)

    def _set_progress(self, downloaded: int, total: int):
        if total > 0:
            pct = (downloaded / total) * 100
            self.progress_var.set(pct)
            mb_dl = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            self.progress_label.config(text=f"{mb_dl:.1f} MB / {mb_total:.1f} MB")
            self.root.update_idletasks()

    def _open_dir(self):
        os.startfile(DOWNLOAD_DIR)

    def _start_download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请输入抖音链接")
            return

        # 提取短链接
        m = re.search(r"https?://v\.douyin\.com/[A-Za-z\d._?%&+\-=#]*", url)
        if not m:
            messagebox.showwarning("提示", "未识别到有效的抖音链接（需要 v.douyin.com 短链接）")
            return
        short_url = m.group(0)

        cookie = self.cookie_var.get().strip()

        self.download_btn.config(state="disabled")
        self.progress_var.set(0)
        self.progress_label.config(text="")
        self._log_info("")  # 清空
        self._set_status("解析中...")

        # 在后台线程执行下载
        thread = threading.Thread(
            target=self._do_download, args=(short_url, cookie), daemon=True
        )
        thread.start()

    def _do_download(self, short_url: str, cookie: str):
        try:
            # 1. 解析短链接
            self._set_status("正在解析短链接...")
            self._log_info(f"短链接: {short_url}")
            full_url = resolve_short_link(short_url)
            self._log_info(f"重定向: {full_url}")

            dou_id, link_type = extract_id_from_url(full_url)

            # 2. 下载
            if dou_id and cookie and check_nodejs():
                # 有 cookie + Node.js，使用官方 API
                self._set_status("使用官方 API 获取作品信息...")
                self._log_info(f"作品 ID: {dou_id}")
                filepath, title, author, cover = download_video(
                    dou_id, cookie,
                    progress_callback=lambda d, t: self.root.after(
                        0, self._set_progress, d, t
                    )
                )
            else:
                # 无 cookie，使用备用 API
                if not cookie:
                    self._log_info("未填写 Cookie，使用备用 API 解析")
                else:
                    self._log_info(f"短链接类型: {link_type}，使用备用 API")
                self._set_status("使用备用 API 解析...")
                filepath, title, author, cover = download_fallback(
                    short_url,
                    progress_callback=lambda d, t: self.root.after(
                        0, self._set_progress, d, t
                    )
                )

            # 3. 显示结果
            self.root.after(0, self._on_success, filepath, title, author, cover)

        except Exception as e:
            self.root.after(0, self._on_error, str(e))

    def _on_success(self, filepath: str, title: str, author: str, cover: str):
        self._set_status("下载完成")
        self.progress_var.set(100)
        self._log_info(f"标题: {title}")
        self._log_info(f"作者: {author}")
        self._log_info(f"封面: {cover}")

        # 显示保存的文件
        for fp in filepath.split(";"):
            if os.path.exists(fp):
                size_mb = os.path.getsize(fp) / (1024 * 1024)
                self._log_info(f"已保存: {fp} ({size_mb:.1f} MB)")

        self.progress_label.config(text="✅ 下载完成！")
        self.download_btn.config(state="normal")
        messagebox.showinfo("完成", f"下载完成！\n\n标题: {title}\n作者: {author}")

    def _on_error(self, error_msg: str):
        self._set_status("下载失败")
        self._log_info(f"错误: {error_msg}")
        self.progress_label.config(text="❌ 下载失败")
        self.download_btn.config(state="normal")
        messagebox.showerror("错误", f"下载失败:\n{error_msg}")


def main():
    root = tk.Tk()
    app = DouyinDownloaderApp(root)

    # 如果命令行传了 URL，自动填入
    if len(sys.argv) > 1:
        app.url_var.set(sys.argv[1])

    root.mainloop()


if __name__ == "__main__":
    main()
