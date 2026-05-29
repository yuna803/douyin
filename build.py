"""
抖音下载器 - PyInstaller 打包脚本
运行: python build.py
"""
import subprocess
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

NODE_PATH = r"C:\Program Files\nodejs\node.exe"

print("=" * 50)
print("  Douyin Downloader - PyInstaller Build")
print("=" * 50)

# 检查 node.exe
if not os.path.exists(NODE_PATH):
    print(f"ERROR: node.exe not found at {NODE_PATH}")
    print("Please install Node.js or update NODE_PATH in this script.")
    sys.exit(1)

# 清理
print("\n[1/3] Cleaning old files...")
import shutil
for path in ["dist", "build"]:
    if os.path.exists(path):
        shutil.rmtree(path)
for f in os.listdir("."):
    if f.endswith(".spec"):
        os.remove(f)

# 打包
print("[2/3] Building exe...")
cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "DouyinDownloader",
    "--add-binary", f"{NODE_PATH};.",
    "--add-data", "a-bogus.js;.",
    "--hidden-import", "execjs",
    "--hidden-import", "httpx",
    "--clean",
    "douyin_downloader.py",
]

result = subprocess.run(cmd)
if result.returncode != 0:
    print("\nBuild FAILED!")
    sys.exit(1)

# 重命名
print("[3/3] Renaming output...")
src = os.path.join("dist", "DouyinDownloader.exe")
dst = os.path.join("dist", "doyin_downloader.exe")
if os.path.exists(dst):
    os.remove(dst)
os.rename(src, dst)

print("\n" + "=" * 50)
print(f"  SUCCESS -> dist\\douyin_downloader.exe")
print("=" * 50)
