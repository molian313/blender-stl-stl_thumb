# SPDX-License-Identifier: GPL-2.0-or-later

bl_info = {
    "name": "STL-Thumb Cache → PNG",
    "author": "trae-assistant",
    "version": (1, 0, 22),
    "blender": (3, 0, 0),
    "location": "3D View → Sidebar (N) → STL-Thumb",
    "description": "将 stl-thumb 生成的缩略图转换为 PNG 文件",
    "category": "Import-Export",
}

import os
import sys
import hashlib
import shutil
import subprocess
import threading
import urllib.parse
import platform
import traceback
import struct

import bpy
from bpy.props import (
    StringProperty,
    EnumProperty,
    IntProperty,
    BoolProperty,
    FloatProperty,
    PointerProperty,
    CollectionProperty,
)
from bpy.types import (
    AddonPreferences,
    Operator,
    Panel,
    PropertyGroup,
)
from bpy_extras.io_utils import ImportHelper

# 全局预览缓存，避免在 draw() 里反复创建/销毁导致崩溃
_preview_collection = {}

def _get_preview_collection():
    global _preview_collection
    pcoll_key = __name__
    if pcoll_key not in _preview_collection:
        _preview_collection[pcoll_key] = bpy.utils.previews.new()
    return _preview_collection[pcoll_key]


# ============================================================
#   工具函数
# ============================================================

def _log(msg: str, level: str = "INFO") -> None:
    print(f"[STL-Thumb] [{level}] {msg}")


def _list_stl_files(folder: str, recursive: bool):
    result = []
    if recursive:
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".stl"):
                    result.append(os.path.join(root, f))
    else:
        for f in os.listdir(folder):
            full = os.path.join(folder, f)
            if os.path.isfile(full) and f.lower().endswith(".stl"):
                result.append(full)
    return sorted(result)


def _find_stl_thumb_cli(preferences) -> str:
    if preferences and getattr(preferences, "stl_thumb_cli_path", ""):
        custom = bpy.path.abspath(preferences.stl_thumb_cli_path).strip()
        if custom and os.path.isfile(custom):
            return custom
    exe = shutil.which("stl-thumb")
    if exe:
        return exe
    home = os.path.expanduser("~")
    for cand in (
        r"C:\Program Files\stl-thumb\stl-thumb.exe",
        r"C:\Program Files (x86)\stl-thumb\stl-thumb.exe",
        os.path.join(home, ".cargo", "bin", "stl-thumb.exe"),
        os.path.join(home, ".cargo", "bin", "stl-thumb"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def _freedesktop_thumbnail_path(stl_path: str, size_dir: str = "normal"):
    abs_path = os.path.abspath(stl_path)
    uri = "file://" + urllib.parse.quote(abs_path)
    digest = hashlib.md5(uri.encode("utf-8")).hexdigest()
    base = os.path.expanduser("~/.cache/thumbnails")
    return os.path.join(base, size_dir, digest + ".png")


# ============================================================
#   策略 1：调用 stl-thumb CLI 直接生成 PNG
# ============================================================

def extract_via_cli(stl_path: str, out_path: str, size: int, cli_path: str) -> bool:
    if not cli_path:
        return False
    try:
        args = [cli_path, "-s", str(size), stl_path, out_path]
        _log(f"CLI: {' '.join(args)}")
        proc = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            _log(f"stl-thumb 失败: {proc.stderr}", "ERROR")
            _log(f"stdout: {proc.stdout}", "ERROR")
            return False
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception as exc:
        _log(f"调用 stl-thumb 异常: {exc}", "ERROR")
        return False


# ============================================================
#   策略 2：读取 Linux FreeDesktop 缩略图缓存
# ============================================================

def extract_via_freedesktop_cache(stl_path: str, out_path: str) -> bool:
    for size_dir in ("large", "normal", "small"):
        src = _freedesktop_thumbnail_path(stl_path, size_dir)
        if os.path.isfile(src) and os.path.getsize(src) > 0:
            try:
                shutil.copyfile(src, out_path)
                _log(f"FreeDesktop 缓存: {src}")
                return True
            except Exception as exc:
                _log(f"复制缓存失败: {exc}", "ERROR")
    return False


# ============================================================
#   策略 3：Windows Shell 缩略图
# ============================================================

def _extract_via_win_shell(stl_path: str, out_path: str, size: int) -> bool:
    if platform.system() != "Windows":
        return False
    try:
        ps_path = stl_path.replace("'", "''")
        ps_out = out_path.replace("'", "''")
        ps_script = (
            "Add-Type -AssemblyName System.Drawing; "
            "$ErrorActionPreference = 'Stop'; "
            f"$path = '{ps_path}'; "
            f"$out = '{ps_out}'; "
            f"$size = {size}; "
            "$shell = New-Object -ComObject Shell.Application; "
            "$parent = Split-Path $path -Parent; "
            "$name = Split-Path $path -Leaf; "
            "$folder = $shell.Namespace($parent); "
            "if ($null -eq $folder) { exit 2 }; "
            "$item = $folder.ParseName($name); "
            "if ($null -eq $item) { exit 3 }; "
            "try { $bmp = $item.GetThumbnail($size, $size) } catch { exit 4 }; "
            "if ($null -eq $bmp) { exit 5 }; "
            "$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png); "
            "exit 0"
        )
        _log("通过 PowerShell + Shell 读取缩略图")
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, timeout=90,
        )
        if proc.returncode != 0:
            _log(f"PowerShell 返回码 {proc.returncode}", "WARN")
            if proc.stderr:
                _log(f"stderr: {proc.stderr.strip()}", "WARN")
        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0
    except Exception as exc:
        _log(f"PowerShell 调用异常: {exc}", "ERROR")
        return False


# ============================================================
#   缩略图 PNG 输出路径工具
# ============================================================

def _get_pref_thumb_folder(preferences) -> str:
    """获取偏好设置里的缩略图保存目录；未设置则返回空字符串。"""
    if preferences is None:
        return ""
    try:
        v = getattr(preferences, "thumb_folder", "") or ""
    except Exception:
        return ""
    return bpy.path.abspath(v).strip() if v else ""


def _build_thumb_path(stl_path: str, preferences) -> str:
    """为一个 STL 文件计算缩略图 PNG 的完整路径。

    - 如果偏好设置里填了「缩略图 PNG 保存目录」：PNG 统一放到那里，
      文件名用 "<原名>_<短哈希>.png"，避免不同目录下同名 STL 冲突。
    - 否则：返回 STL 所在目录 + "<原名>.png"（旧行为）。
    """
    pref_folder = _get_pref_thumb_folder(preferences)
    base = os.path.splitext(os.path.basename(stl_path))[0]
    if pref_folder:
        try:
            os.makedirs(pref_folder, exist_ok=True)
        except OSError:
            pass
        short_hex = hashlib.md5(os.path.abspath(stl_path).encode("utf-8")).hexdigest()[:8]
        return os.path.join(pref_folder, f"{base}_{short_hex}.png")
    return os.path.join(os.path.dirname(os.path.abspath(stl_path)), base + ".png")


def _pick_thumb_for_stl(stl_path: str, preferences) -> str:
    """给定一个 STL，找到它对应的缩略图 PNG（如果存在）。

    查找顺序：
      1) _build_thumb_path() 返回的路径（用户设置的缩略图目录）
      2) STL 同目录下的 <同名>.png（历史版本产生的缓存）
    两者都不在就返回空串。
    """
    candidates = [
        _build_thumb_path(stl_path, preferences),
        os.path.join(
            os.path.dirname(os.path.abspath(stl_path)),
            os.path.splitext(os.path.basename(stl_path))[0] + ".png",
        ),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


def _stable_pcoll_key(stl_path: str) -> str:
    """为一个 STL 生成稳定的 pcoll key。

    Blender 5.x 的 pcoll 对 key 没什么限制，但我们要保证唯一，
    所以用 "短哈希_文件名" 组合，避免两个目录下同名字的 STL 冲突。
    """
    abs_path = os.path.abspath(stl_path)
    short_hex = hashlib.md5(abs_path.encode("utf-8")).hexdigest()[:8]
    return short_hex + "_" + os.path.basename(abs_path)


# 全局预览缓存：记录已经加载到 pcoll 的 STL（避免 draw 里重复 load）
_pcoll_cache = set()


def _ensure_thumb_in_pcoll(stl_path: str, thumb_path: str, pcoll) -> int:
    """在 draw() 里安全地加载缩略图到 pcoll（不写 item.icon_id）。
    
    返回 icon_id 如果成功，返回 0 如果失败。
    """
    pkey = _stable_pcoll_key(stl_path)
    
    # 已经加载过了
    if pkey in _pcoll_cache:
        if pkey in pcoll:
            return pcoll[pkey].icon_id
        # pcoll 被清空了，清除缓存让它重新加载
        _pcoll_cache.discard(pkey)
    
    abs_png = bpy.path.abspath(thumb_path).strip()
    if not abs_png or not os.path.isfile(abs_png):
        return 0
    
    try:
        if pkey in pcoll:
            del pcoll[pkey]
        pcoll.load(pkey, abs_png, 'IMAGE')
        _pcoll_cache.add(pkey)
        return pcoll[pkey].icon_id
    except Exception:
        return 0


def _scan_and_load_thumbs(scene, prefs) -> int:
    """简单直接：扫描 folder 里的 STL，找 PNG 缩略图，加载到预览缓存。"""
    try:
        props = scene.stl_thumb_export
    except AttributeError:
        _log("_scan: scene.stl_thumb_export 不可访问", "WARN")
        return 0

    folder = bpy.path.abspath(props.folder).strip()
    _log(f"_scan: folder_raw='{props.folder}' abs='{folder}' is_dir={os.path.isdir(folder)} items={len(props.items)}")
    if not folder or not os.path.isdir(folder):
        _log(f"_scan: folder 无效: '{folder}'")
        return 0

    pcoll = _get_preview_collection()

    # 扫描 STL
    files = _list_stl_files(folder, props.recursive)
    if not files:
        _log(f"_scan: {folder} 里没有 STL 文件")
        return 0

    # 清空旧条目
    if len(props.items) > 0:
        props.items.clear()

    # 获取偏好里的缩略图目录（用于模糊匹配兜底）
    thumb_folder = ""
    if prefs is not None:
        try:
            tf = getattr(prefs, "thumb_folder", "") or ""
            if tf:
                tf_abs = bpy.path.abspath(tf).strip()
                if tf_abs and os.path.isdir(tf_abs):
                    thumb_folder = tf_abs
        except Exception:
            pass

    ok = 0
    _log(f"_scan: 在 {folder} 里找到 {len(files)} 个 STL")
    for stl in files:
        png = _pick_thumb_for_stl(stl, prefs)

        # 兜底：在 thumb_folder 里按文件名模糊匹配
        if not png and thumb_folder:
            stl_base = os.path.splitext(os.path.basename(stl))[0]
            for fn in os.listdir(thumb_folder):
                if fn.lower().endswith(".png") and stl_base.lower() in fn.lower():
                    candidate = os.path.join(thumb_folder, fn)
                    if os.path.isfile(candidate):
                        png = candidate
                        break

        # 如果还是没有，尝试从 stl-thumb 缓存提取并放入 thumb_folder
        if not png:
            try:
                strategy = getattr(props, "strategy", "AUTO")
                size = getattr(props, "thumb_size", 128)
                png = try_extract_one(stl, folder, size, strategy, prefs)
            except Exception as exc:
                _log(f"提取失败 {stl}: {exc}", "WARN")

        item = props.items.add()
        item.filepath = stl
        item.display_name = os.path.relpath(stl, folder)
        try:
            item.mtime = int(os.path.getmtime(stl))
        except OSError:
            item.mtime = 0

        if png:
            pkey = _stable_pcoll_key(stl)
            try:
                if pkey in pcoll:
                    del pcoll[pkey]
                pcoll.load(pkey, png, 'IMAGE')
                item.thumb = png
                ok += 1
            except Exception as exc:
                _log(f"加载失败 {png}: {exc}", "WARN")
        else:
            pass

    _log(f"_scan: 恢复 {ok} / {len(files)} 个缩略图")
    return ok


# ============================================================
#   总调度
# ============================================================

def try_extract_one(stl_path: str, out_dir_ignored: str, size: int, strategy: str, preferences) -> str:
    """生成给定 STL 文件的缩略图 PNG，返回 PNG 路径；失败返回空字符串。

    注意：out_dir_ignored 参数为兼容保留，实际输出路径由偏好设置 + STL 路径决定。
    """
    out_path = _build_thumb_path(stl_path, preferences)
    parent = os.path.dirname(out_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError:
            pass

    cli_path = _find_stl_thumb_cli(preferences)

    if strategy == "AUTO":
        order = []
        if cli_path:
            order.append("CLI")
        if platform.system() == "Windows":
            order.append("WIN_SHELL")
            order.append("WIN_CACHE")
        if platform.system() in ("Linux", "Darwin"):
            order.append("LINUX_CACHE")
        if not order:
            order = ["CLI"]
    else:
        order = [strategy]

    for strat in order:
        ok = False
        try:
            if strat == "CLI":
                ok = extract_via_cli(stl_path, out_path, size, cli_path)
            elif strat == "LINUX_CACHE":
                ok = extract_via_freedesktop_cache(stl_path, out_path)
            elif strat == "WIN_SHELL":
                ok = _extract_via_win_shell(stl_path, out_path, size)
            elif strat == "WIN_CACHE":
                ok = _extract_via_win_shell(stl_path, out_path, max(size, 512))
        except Exception as exc:
            _log(f"策略 {strat} 异常: {exc}", "ERROR")
            _log(traceback.format_exc(), "ERROR")
            ok = False

        if ok and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            _log(f"{os.path.basename(stl_path)} -> {out_path} (通过 {strat})")
            return out_path
        if os.path.isfile(out_path) and os.path.getsize(out_path) == 0:
            try:
                os.remove(out_path)
            except OSError:
                pass

    _log(f"无法提取: {stl_path}", "ERROR")
    return ""


# ============================================================
#   场景属性
# ============================================================

class STLThumbSceneProps(PropertyGroup):
    """文件夹批量转换场景属性（上面的面板需要它）。"""
    input_folder: StringProperty(
        name="STL 文件夹",
        description="选择包含 .stl 文件的文件夹",
        subtype="DIR_PATH",
        default="",
    )
    output_folder: StringProperty(
        name="PNG 保存到",
        description="留空则与每个 STL 同目录",
        subtype="DIR_PATH",
        default="",
    )
    recursive: BoolProperty(name="递归子目录", default=True)
    size: IntProperty(name="图像大小 (px)", default=512, min=32, max=4096)
    strategy: EnumProperty(
        name="提取策略",
        items=[
            ("AUTO", "自动", "按系统和可用工具自动选择"),
            ("CLI", "stl-thumb CLI", ""),
            ("WIN_SHELL", "Windows Shell", "（仅 Windows）"),
            ("WIN_CACHE", "Windows 缓存", "（仅 Windows）"),
            ("LINUX_CACHE", "FreeDesktop 缓存", "（Linux）"),
        ],
        default="AUTO",
    )


class STLThumbExportItem(PropertyGroup):
    filepath: StringProperty(name="文件路径", subtype="FILE_PATH")
    thumb: StringProperty(name="缩略图路径", subtype="FILE_PATH")
    display_name: StringProperty(name="显示名称")
    # icon_id 是 Blender 运行期 IconPreviewCollection 返回的 ID，**绝对不能**存进 .blend 文件
    # —— 新会话里 pcoll 会重建，存下来的 ID 必然是无效的，会触发 "no icon for icon ID"
    icon_id: IntProperty(name="图标ID", default=0, options={"SKIP_SAVE"})
    mtime: IntProperty(name="修改时间", default=0)


class STLThumbExportProps(PropertyGroup):
    folder: StringProperty(
        name="导出目标文件夹",
        description="扫描这个目录里的 STL 文件并显示缩略图",
        subtype="DIR_PATH",
        default="",
    )
    columns: IntProperty(
        name="列数", default=3, min=1, max=8,
        description="缩略图网格的列数",
    )
    thumb_size: IntProperty(
        name="缩略图大小 (px)", default=128, min=64, max=1024,
    )
    recursive: BoolProperty(
        name="递归子目录", default=False,
    )
    new_filename: StringProperty(
        name="新文件名", default="",
        description="导出到一个全新的 STL 文件时用的名字（不带 .stl 也可以）",
    )
    use_selection: BoolProperty(
        name="仅导出选中物体", default=True,
        description="关闭则导出场景中所有可见物体",
    )
    scale: FloatProperty(
        name="导出缩放", default=10.0, min=0.001, max=10000.0,
        description="STL 文件缩放倍数。Blender 默认单位是米，3D 打印常用毫米，一般填 10。",
    )
    strategy: EnumProperty(
        name="缩略图策略",
        items=[
            ("AUTO", "自动", "按系统和可用工具自动选择"),
            ("CLI", "stl-thumb CLI", "直接调用 stl-thumb 可执行程序"),
            ("WIN_SHELL", "Windows Shell", "通过 Windows 资源管理器接口读取（仅 Windows）"),
            ("WIN_CACHE", "Windows 缓存", "（仅 Windows）"),
            ("LINUX_CACHE", "FreeDesktop 缓存", "从 ~/.cache/thumbnails 读取（Linux）"),
        ],
        default="AUTO",
    )
    items: CollectionProperty(type=STLThumbExportItem)
    active_index: IntProperty(default=0)


# ============================================================
#   插件偏好设置
# ============================================================

class STLThumbAddonPreferences(AddonPreferences):
    bl_idname = __name__

    stl_thumb_cli_path: StringProperty(
        name="stl-thumb 可执行文件",
        description="手动指定 stl-thumb.exe 的完整路径。留空会自动搜索 PATH。",
        subtype="FILE_PATH",
        default="",
    )
    thumb_folder: StringProperty(
        name="缩略图 PNG 保存目录",
        description="生成的缩略图 PNG 统一存放到这个目录；留空则保存到与每个 STL 同目录。",
        subtype="DIR_PATH",
        default="",
    )
    # 下面两个用于「批量转换」（STL → PNG）功能，移到偏好设置里管理，不在 3D 视图侧栏显示
    conv_input_folder: StringProperty(
        name="STL 来源文件夹",
        description="批量转换时扫描的包含 .stl 文件的目录。",
        subtype="DIR_PATH",
        default="",
    )
    conv_output_folder: StringProperty(
        name="PNG 输出目录",
        description="批量转换时 PNG 输出目录；留空则使用上面「缩略图 PNG 保存目录」。",
        subtype="DIR_PATH",
        default="",
    )
    conv_recursive: BoolProperty(name="递归子目录", default=True)
    conv_size: IntProperty(name="图像大小 (px)", default=256, min=32, max=4096)
    conv_strategy: EnumProperty(
        name="提取策略",
        items=[
            ("AUTO", "自动", ""),
            ("CLI", "stl-thumb CLI", ""),
            ("WIN_SHELL", "Windows Shell", ""),
            ("WIN_CACHE", "Windows 缓存", ""),
            ("LINUX_CACHE", "FreeDesktop 缓存", ""),
        ],
        default="AUTO",
    )
    float_panel_width: IntProperty(
        name="浮动面板宽度",
        description="浮动弹窗面板的宽度（像素）",
        default=400, min=200, max=1200,
    )
    float_panel_thumb_scale: FloatProperty(
        name="浮动缩略图大小",
        description="浮动弹窗中缩略图的显示大小（scale 参数）",
        default=4.0, min=1.0, max=10.0,
    )
    float_columns: IntProperty(
        name="浮动弹窗列数",
        description="浮动弹窗中缩略图网格的列数",
        default=4, min=1, max=12,
    )

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        box = layout.box()
        box.label(text="工具路径", icon="FILE_FOLDER")
        box.prop(self, "stl_thumb_cli_path")
        found = _find_stl_thumb_cli(self)
        if found:
            box.label(text=f"已检测到: {found}", icon="CHECKMARK")
        else:
            box.label(
                text="未检测到 stl-thumb CLI，可从 https://github.com/stefanhaustein/stl-thumb 下载",
                icon="ERROR",
            )
        box.label(text=f"当前系统: {platform.system()}")

        box = layout.box()
        box.label(text="缩略图存储", icon="FILE_IMAGE")
        box.prop(self, "thumb_folder")
        if not self.thumb_folder:
            box.label(
                text="（未指定 —— 缩略图 PNG 将与每个 STL 保存在同目录）",
                icon="INFO",
            )

        box = layout.box()
        box.label(text="批量转换：STL → PNG（不常用，放这里管理）", icon="RENDER_RESULT")
        row = box.row(align=True)
        row.prop(self, "conv_input_folder", text="STL 目录")
        row.operator("stl_thumb.pick_conv_input", text="", icon="FILE_FOLDER")
        row = box.row(align=True)
        row.prop(self, "conv_output_folder", text="PNG 目录")
        row.operator("stl_thumb.pick_conv_output", text="", icon="FILE_FOLDER")
        box.prop(self, "conv_recursive")
        box.prop(self, "conv_size")
        box.prop(self, "conv_strategy")
        box.operator("stl_thumb.convert_folder_pref", icon="RENDER_RESULT")

        box = layout.box()
        box.label(text="单个文件转换", icon="FILE_NEW")
        box.operator("stl_thumb.convert_single", icon="IMAGE_DATA")

        box = layout.box()
        box.label(text="浮动面板设置", icon="WINDOW")
        box.prop(self, "float_panel_width", slider=True)
        box.prop(self, "float_panel_thumb_scale", slider=True)
        box.prop(self, "float_columns")


# ============================================================
#   操作符：刷新 / 扫描
# ============================================================

class STLTHUMB_OT_refresh_export_list(Operator):
    bl_idname = "stl_thumb.refresh_export_list"
    bl_label = "刷新缩略图"
    bl_description = "扫描目标文件夹中的 STL，生成缩略图缓存，刷新下方列表"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.stl_thumb_export
        folder = bpy.path.abspath(props.folder).strip()
        if not folder or not os.path.isdir(folder):
            self.report({"ERROR"}, "请先选择导出目标文件夹")
            return {"CANCELLED"}

        files = _list_stl_files(folder, props.recursive)
        prefs = context.preferences.addons[__name__].preferences

        # 清空旧列表和预览缓存
        props.items.clear()
        props.active_index = 0
        pcoll = _get_preview_collection()
        pcoll.clear()
        _pcoll_cache.clear()

        if not files:
            self.report({"INFO"}, "目标文件夹里没有 .stl 文件")
            return {"FINISHED"}

        ok = 0
        wm = context.window_manager
        wm.progress_begin(0, len(files))
        try:
            for i, stl in enumerate(files):
                item = props.items.add()
                item.filepath = stl
                item.display_name = os.path.relpath(stl, folder)
                try:
                    item.mtime = int(os.path.getmtime(stl))
                except OSError:
                    item.mtime = 0

                # 生成缩略图 PNG
                png = try_extract_one(stl, folder, props.thumb_size, props.strategy, prefs)
                if png:
                    item.thumb = png
                    ok += 1
                    # 用稳定 key（不是 display_name）加载到预览缓存
                    pkey = _stable_pcoll_key(stl)
                    if pkey in pcoll:
                        del pcoll[pkey]
                    pcoll.load(pkey, png, 'IMAGE')
                    item.icon_id = pcoll[pkey].icon_id
                else:
                    item.icon_id = 0

                wm.progress_update(i + 1)
        finally:
            wm.progress_end()

        self.report({"INFO"}, f"已扫描 {len(files)} 个 STL，{ok} 个生成了缩略图")
        _log(f"扫描完成: {len(files)} 个文件，{ok} 张缩略图")
        return {"FINISHED"}


class STLTHUMB_OT_pick_export_folder(Operator):
    bl_idname = "stl_thumb.pick_export_folder"
    bl_label = "选择导出目标文件夹"

    directory: StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        context.scene.stl_thumb_export.folder = self.directory
        return {"FINISHED"}


# ============================================================
#   操作符：文件夹批量转换、单个文件转换、选择输入/输出目录
# ============================================================

class STLTHUMB_OT_convert_folder(Operator):
    bl_idname = "stl_thumb.convert_folder"
    bl_label = "批量转换"
    bl_description = "把所选文件夹中的 .stl 文件缩略图保存为 PNG"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.stl_thumb
        folder = bpy.path.abspath(props.input_folder).strip()
        if not folder or not os.path.isdir(folder):
            self.report({"ERROR"}, "请先选择有效的 STL 文件夹")
            return {"CANCELLED"}

        out_dir = bpy.path.abspath(props.output_folder).strip()
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        files = _list_stl_files(folder, props.recursive)
        if not files:
            self.report({"INFO"}, "文件夹内没有 .stl 文件")
            return {"CANCELLED"}

        prefs = context.preferences.addons[__name__].preferences
        total = len(files)
        ok = 0
        failed = []

        wm = context.window_manager
        wm.progress_begin(0, total)
        try:
            for i, stl in enumerate(files):
                target_dir = out_dir if out_dir else os.path.dirname(stl)
                result = try_extract_one(
                    stl, target_dir, props.size, props.strategy, prefs
                )
                if result:
                    ok += 1
                else:
                    failed.append(os.path.relpath(stl, folder))
                wm.progress_update(i + 1)
        finally:
            wm.progress_end()

        self.report({"INFO"}, f"完成：共 {total} 个，成功 {ok} 个，失败 {len(failed)} 个")
        _log(f"批量转换完成: {total} 个文件, {ok} 成功")
        return {"FINISHED"}


class STLTHUMB_OT_convert_single(Operator, ImportHelper):
    bl_idname = "stl_thumb.convert_single"
    bl_label = "转换单个 STL"
    bl_description = "选择一个 .stl 文件，将其缩略图保存为 PNG"

    filename_ext = ".stl"
    filter_glob: StringProperty(default="*.stl", options={"HIDDEN"})

    def execute(self, context):
        if not self.filepath or not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "请选择有效的 STL 文件")
            return {"CANCELLED"}

        props = context.scene.stl_thumb
        prefs = context.preferences.addons[__name__].preferences

        out_dir = bpy.path.abspath(props.output_folder).strip()
        if not out_dir:
            out_dir = os.path.dirname(os.path.abspath(self.filepath))
        else:
            os.makedirs(out_dir, exist_ok=True)

        result = try_extract_one(
            self.filepath, out_dir, props.size, props.strategy, prefs
        )
        if result:
            self.report({"INFO"}, f"已保存: {result}")
            return {"FINISHED"}

        self.report({"ERROR"}, "缩略图提取失败，请尝试其他策略")
        return {"CANCELLED"}


class STLTHUMB_OT_pick_folder(Operator):
    bl_idname = "stl_thumb.pick_folder"
    bl_label = "选择 STL 文件夹"

    directory: StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        context.scene.stl_thumb.input_folder = self.directory
        return {"FINISHED"}


class STLTHUMB_OT_pick_output(Operator):
    bl_idname = "stl_thumb.pick_output"
    bl_label = "选择 PNG 输出目录"

    directory: StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        context.scene.stl_thumb.output_folder = self.directory
        return {"FINISHED"}


# ============================================================
#   STL 导出逻辑
# ============================================================

def _do_export_stl(context, filepath: str, use_selection: bool, scale: float = 1.0) -> bool:
    """直接手写二进制 STL，不依赖 bpy.ops（Blender 5.x 里 export_mesh.stl 已移除）。"""
    try:
        return _write_binary_stl(context, filepath, use_selection, scale)
    except Exception as exc:
        _log(f"STL 导出失败: {exc}", "ERROR")
        _log(traceback.format_exc(), "ERROR")
        return False


def _write_binary_stl(context, filepath: str, use_selection: bool, scale: float = 1.0) -> bool:
    """二进制 STL。支持 Blender 4.x / 5.x。scale: 导出缩放倍数。"""
    from mathutils import Matrix

    depsgraph = context.evaluated_depsgraph_get()
    triangles = []

    objs = context.selected_objects if use_selection else context.scene.objects
    objs = [o for o in objs if o.type == "MESH" and o.visible_get()]

    if not objs:
        _log("没有可导出的 mesh 物体", "ERROR")
        return False

    safe_scale = float(scale) if scale and scale > 0 else 1.0
    scale_matrix = Matrix.Scale(safe_scale, 4)
    _log(f"导出 STL: 使用缩放 ×{safe_scale}")

    for obj in objs:
        try:
            eval_obj = obj.evaluated_get(depsgraph)
            mesh = eval_obj.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        except Exception:
            try:
                mesh = obj.to_mesh()
            except Exception:
                continue
        if mesh is None:
            continue
        try:
            mesh.calc_loop_triangles()
            # 先应用世界矩阵，再乘缩放
            matrix = scale_matrix @ obj.matrix_world
            for tri in mesh.loop_triangles:
                # Blender 5.x: tri.loops 是 (int, int, int) 顶点索引三元组
                # Blender 4.x: tri.loops 是 MLoopCorner 对象列表
                if hasattr(tri, "vertices") and len(tri.vertices) == 3:
                    v_indices = tri.vertices
                elif hasattr(tri, "loops"):
                    loops = tri.loops
                    if hasattr(loops[0], "vertex_index"):
                        v_indices = [loops[i].vertex_index for i in range(3)]
                    else:
                        v_indices = list(loops)[:3]
                else:
                    continue
                n = tri.normal if hasattr(tri, "normal") and tri.normal else (0.0, 0.0, 1.0)
                verts = [matrix @ mesh.vertices[vi].co for vi in v_indices]
                triangles.append((
                    float(n[0]), float(n[1]), float(n[2]),
                    float(verts[0][0]), float(verts[0][1]), float(verts[0][2]),
                    float(verts[1][0]), float(verts[1][1]), float(verts[1][2]),
                    float(verts[2][0]), float(verts[2][1]), float(verts[2][2]),
                ))
        finally:
            try:
                obj.evaluated_get(depsgraph).to_mesh_clear()
            except Exception:
                try:
                    obj.to_mesh_clear()
                except Exception:
                    pass

    parent = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(parent, exist_ok=True)

    with open(filepath, "wb") as fp:
        fp.write(b"\x00" * 80)
        fp.write(struct.pack("<I", len(triangles)))
        for t in triangles:
            fp.write(struct.pack("<12fH", *t, 0))
    _log(f"STL 导出: {filepath} ({len(triangles)} 个三角面)")
    return True


class STLTHUMB_OT_export_to_stl(Operator):
    bl_idname = "stl_thumb.export_to_stl"
    bl_label = "导出到此文件"
    bl_description = "把当前场景/选中物体导出为 STL，并刷新它的缩略图"
    bl_options = {"REGISTER", "UNDO"}

    target: StringProperty(
        name="目标 STL 路径", subtype="FILE_PATH",
        description="留空表示用下方「新文件名」",
        default="",
    )

    def execute(self, context):
        props = context.scene.stl_thumb_export
        folder = bpy.path.abspath(props.folder).strip()

        # 是否为「覆盖导出」（即用户点了某个缩略图下的按钮，明确要覆盖目标文件）
        is_overwrite = False
        used_rename_suffix = False

        # 决定输出路径
        if self.target:
            # 覆盖导出模式：直接覆盖目标文件，不做自动重命名
            out_path = self.target
            if not os.path.isabs(out_path):
                out_path = os.path.join(folder, out_path)
            is_overwrite = os.path.isfile(out_path)
        else:
            # 新建导出模式：如果文件存在，自动加 _1 / _2 递增后缀，直到不冲突
            name = props.new_filename.strip()
            if not name:
                self.report({"ERROR"}, "请填写「新文件名」或点击一个已有 STL 下方的按钮")
                return {"CANCELLED"}
            if not name.lower().endswith(".stl"):
                name += ".stl"
            base_dir = folder if folder else os.getcwd()
            candidate = os.path.join(base_dir, name)
            if os.path.isfile(candidate):
                # 自动重命名：把 .stl 前插入 _N
                base, ext = os.path.splitext(name)
                n = 1
                while n < 10000:
                    alt = os.path.join(base_dir, f"{base}_{n}{ext}")
                    if not os.path.isfile(alt):
                        candidate = alt
                        used_rename_suffix = True
                        break
                    n += 1
            out_path = candidate

        if os.path.isdir(out_path):
            self.report({"ERROR"}, f"目标是一个目录: {out_path}")
            return {"CANCELLED"}

        parent = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(parent, exist_ok=True)

        if not _do_export_stl(context, out_path, props.use_selection, props.scale):
            self.report({"ERROR"}, f"导出失败: {os.path.basename(out_path)}")
            return {"CANCELLED"}

        # 导出成功后，**后台线程**更新这个文件的缩略图 —— 用户不阻塞等待渲染
        prefs = context.preferences.addons[__name__].preferences
        size = props.thumb_size
        strategy = props.strategy

        # 计算相对于目标文件夹的显示名（新建文件时列表里还没有这个条目）
        abs_out = os.path.abspath(out_path)
        abs_folder = os.path.abspath(folder) if folder else ""
        if abs_folder and abs_out.startswith(abs_folder + os.sep):
            display_name = os.path.relpath(abs_out, abs_folder)
        else:
            display_name = os.path.basename(abs_out)

        # --- 立即在主线程：如果是新文件则新增占位条目；如果是覆盖则把 icon_id 置 0 ---
        # 这样用户会立刻看到列表里出现这个文件（显示“(无缩略图) 刷新中”）
        def _mark_refresh_in_main_thread():
            try:
                scene_ctx = bpy.context.scene
                items = getattr(scene_ctx, "stl_thumb_export", None)
                if items is not None:
                    found = False
                    for it in items.items:
                        if os.path.abspath(it.filepath) == abs_out:
                            it.icon_id = 0
                            found = True
                            break
                    if not found:
                        # 新文件 —— 新增占位条目
                        new_item = items.items.add()
                        new_item.filepath = abs_out
                        new_item.display_name = display_name
                        new_item.thumb = ""
                        new_item.icon_id = 0
                        new_item.mtime = 0
                # 触发 3D 视图侧栏重绘
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == "VIEW_3D":
                            area.tag_redraw()
            except Exception as exc:
                _log(f"标记刷新状态失败: {exc}", "WARN")

        try:
            bpy.app.timers.register(_mark_refresh_in_main_thread, first_interval=0.05)
        except Exception:
            pass

        def _bg_update_thumb(stl_path, target_folder, out_png_path_hint,
                             size_int, strat, prefs_obj, is_new_file_flag,
                             disp_name,
                             addon_name=__name__):
            try:
                png = try_extract_one(stl_path, target_folder, size_int, strat, prefs_obj)
            except Exception as exc:
                _log(f"后台缩略图更新异常: {exc}", "ERROR")
                return
            if not png:
                return

            def _apply_in_main_thread():
                try:
                    pcoll = _get_preview_collection()
                    scene_ctx = bpy.context.scene
                    items = getattr(scene_ctx, "stl_thumb_export", None)
                    if items is not None:
                        target_item = None
                        for it in items.items:
                            if os.path.abspath(it.filepath) == abs_out:
                                target_item = it
                                break
                        # 没找到就新增加一个条目（新建导出场景）
                        if target_item is None:
                            target_item = items.items.add()
                            target_item.filepath = abs_out
                            target_item.display_name = disp_name
                            target_item.mtime = 0
                        target_item.thumb = png
                        pkey = _stable_pcoll_key(target_item.filepath)
                        if pkey in pcoll:
                            del pcoll[pkey]
                        pcoll.load(pkey, png, 'IMAGE')
                        target_item.icon_id = pcoll[pkey].icon_id

                    # 新图加载完毕，强制 3D 视图/侧栏重绘
                    for window in bpy.context.window_manager.windows:
                        for area in window.screen.areas:
                            if area.type == "VIEW_3D":
                                area.tag_redraw()
                                for region in area.regions:
                                    region.tag_redraw()
                except Exception as exc:
                    _log(f"应用缩略图到预览缓存失败: {exc}", "WARN")
                return None

            try:
                bpy.app.timers.register(_apply_in_main_thread, first_interval=0.1)
            except Exception as exc:
                _log(f"注册 timer 失败: {exc}", "WARN")

        t = threading.Thread(
            target=_bg_update_thumb,
            args=(out_path, folder, None, size, strategy, prefs, not is_overwrite, display_name),
            daemon=True,
        )
        t.start()

        if is_overwrite:
            msg = f"覆盖成功：{os.path.basename(out_path)}（缩略图正在后台刷新…）"
        elif used_rename_suffix:
            msg = f"命名冲突，已自动重命名为 {os.path.basename(out_path)}（缩略图正在后台刷新…）"
        else:
            msg = f"已导出新文件：{os.path.basename(out_path)}（缩略图正在后台刷新…）"
        self.report({"INFO"}, msg)
        _log(f"{os.path.basename(out_path)} 已导出，缩略图后台刷新中")
        return {"FINISHED"}


class STLTHUMB_OT_delete_stl(Operator):
    bl_idname = "stl_thumb.delete_stl"
    bl_label = "删除此 STL"
    bl_description = "从磁盘删除选中的 STL 文件及其缩略图缓存"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(name="STL 文件路径", subtype="FILE_PATH", default="")

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        path = bpy.path.abspath(self.filepath).strip()
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "文件不存在")
            return {"CANCELLED"}

        props = context.scene.stl_thumb_export
        prefs = context.preferences.addons[__name__].preferences

        # 尝试删除 STL 文件
        try:
            os.remove(path)
        except Exception as exc:
            self.report({"ERROR"}, f"删除失败: {exc}")
            return {"CANCELLED"}

        # 同时在两处尝试删除缩略图：
        # 1) 与 STL 同目录的 <同名>.png（老路径 / 未指定缩略图目录时）
        # 2) 偏好设置里的缩略图目录（通过 _build_thumb_path 计算）
        removed_png = False
        for candidate_png in (
            os.path.splitext(path)[0] + ".png",
            _build_thumb_path(path, prefs),
        ):
            if candidate_png and os.path.isfile(candidate_png):
                try:
                    os.remove(candidate_png)
                    removed_png = True
                except OSError:
                    pass

        # 清理预览缓存中的项目
        pcoll = _get_preview_collection()
        try:
            items_to_remove = []
            for it in props.items:
                if os.path.abspath(it.filepath) == os.path.abspath(path):
                    items_to_remove.append(it.display_name)
            # 从 CollectionProperty 里真正删除（从后往前删）
            # Blender 5.x 使用 collection_property.items() 不能直接 remove，需通过序列方式删除
            # 用 reverse index 删除：找到匹配项的 index
            for idx in range(len(props.items) - 1, -1, -1):
                it = props.items[idx]
                if os.path.abspath(it.filepath) == os.path.abspath(path):
                    pkey = _stable_pcoll_key(it.filepath)
                    if pkey in pcoll:
                        del pcoll[pkey]
                    props.items.remove(idx)
        except Exception as exc:
            _log(f"清理预览缓存时出错: {exc}", "WARN")

        msg = "已删除"
        if removed_png:
            msg += "（含缩略图）"
        self.report({"INFO"}, f"{msg}: {os.path.basename(path)}")
        _log(f"{msg}: {path}")
        return {"FINISHED"}


class STLTHUMB_OT_set_scale_10(Operator):
    bl_idname = "stl_thumb.set_scale_10"
    bl_label = "设置缩放为 10"
    bl_description = "一键把导出缩放设为 10（Blender 米 → 3D 打印毫米）"

    value: FloatProperty(name="值", default=10.0)

    def execute(self, context):
        context.scene.stl_thumb_export.scale = self.value
        return {"FINISHED"}


class STLTHUMB_OT_set_scale_1(Operator):
    bl_idname = "stl_thumb.set_scale_1"
    bl_label = "设置缩放为 1"
    bl_description = "一键把导出缩放设为 1（不缩放）"

    value: FloatProperty(name="值", default=1.0)

    def execute(self, context):
        context.scene.stl_thumb_export.scale = self.value
        return {"FINISHED"}


# ============================================================
#   操作符：偏好设置中的工具（批量转换、选择目录）
# ============================================================

class STLTHUMB_OT_pick_conv_input(Operator):
    bl_idname = "stl_thumb.pick_conv_input"
    bl_label = "选择 STL 目录"

    directory: StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        prefs.conv_input_folder = self.directory
        return {"FINISHED"}


class STLTHUMB_OT_pick_conv_output(Operator):
    bl_idname = "stl_thumb.pick_conv_output"
    bl_label = "选择 PNG 目录"

    directory: StringProperty(subtype="DIR_PATH")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        prefs.conv_output_folder = self.directory
        return {"FINISHED"}


class STLTHUMB_OT_convert_folder_pref(Operator):
    bl_idname = "stl_thumb.convert_folder_pref"
    bl_label = "开始批量转换"
    bl_description = "按偏好设置里的参数把 STL 转成 PNG 缩略图"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        in_folder = bpy.path.abspath(prefs.conv_input_folder).strip()
        if not in_folder or not os.path.isdir(in_folder):
            self.report({"ERROR"}, "请先在偏好设置里选择有效的 STL 目录")
            return {"CANCELLED"}

        files = _list_stl_files(in_folder, prefs.conv_recursive)
        if not files:
            self.report({"INFO"}, "目录内没有 .stl 文件")
            return {"CANCELLED"}

        total = len(files)
        ok = 0
        wm = context.window_manager
        wm.progress_begin(0, total)
        try:
            for i, stl in enumerate(files):
                result = try_extract_one(
                    stl, "", int(prefs.conv_size), prefs.conv_strategy, prefs
                )
                if result:
                    ok += 1
                wm.progress_update(i + 1)
        finally:
            wm.progress_end()

        self.report({"INFO"}, f"完成：共 {total} 个，成功 {ok} 个")
        _log(f"批量转换完成: {total} 个文件, {ok} 成功")
        return {"FINISHED"}


class STLTHUMB_OT_open_export_panel(Operator):
    """把「STL 导出设置」面板作为浮动弹窗打开。可被右键添加到快速收藏夹 (Q 菜单)。"""
    bl_idname = "stl_thumb.open_export_panel"
    bl_label = "打开 STL 导出设置"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        prefs = context.preferences.addons[__name__].preferences
        width = int(getattr(prefs, "float_panel_width", 400))
        return context.window_manager.invoke_popup(self, width=width)

    def execute(self, context):
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        props = context.scene.stl_thumb_export

        box = layout.box()
        row = box.row(align=True)
        row.prop(props, "folder", text="目标文件夹")
        row.operator("stl_thumb.pick_export_folder", text="", icon="FILE_FOLDER")

        row = box.row(align=True)
        row.prop(props, "scale", text="导出缩放 (×)")
        row.operator("stl_thumb.set_scale_10", text="×10", icon="DRIVER_DISTANCE").value = 10.0
        row.operator("stl_thumb.set_scale_1", text="×1", icon="DRIVER_DISTANCE").value = 1.0

        box.prop(props, "use_selection", text="仅导出选中物体")

        row = box.row(align=True)
        row.prop(props, "new_filename", text="新文件名")
        op = row.operator("stl_thumb.export_to_stl", text="新建导出", icon="EXPORT")
        op.target = ""


class STLTHUMB_OT_open_thumb_grid_popup(Operator):
    """把「缩略图浏览器」面板作为浮动弹窗打开。可被右键添加到快速收藏夹 (Q 菜单)。"""
    bl_idname = "stl_thumb.open_thumb_grid_popup"
    bl_label = "打开缩略图浏览器"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        prefs = context.preferences.addons[__name__].preferences
        width = int(getattr(prefs, "float_panel_width", 400))
        return context.window_manager.invoke_popup(self, width=width)

    def execute(self, context):
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        props = context.scene.stl_thumb_export
        prefs = context.preferences.addons[__name__].preferences

        box = layout.box()
        row = box.row(align=True)
        row.prop(props, "recursive", text="递归")
        row.prop(prefs, "float_columns", text="列")
        row.prop(props, "thumb_size", text="尺寸")

        layout.operator("stl_thumb.refresh_export_list", icon="FILE_REFRESH")

        if not props.items:
            col = layout.column(align=True)
            col.label(text="（列表为空 —— 点击上方「刷新缩略图」）", icon="INFO")
            return

        cols = max(1, int(prefs.float_columns))
        thumb_scale = float(getattr(prefs, "float_panel_thumb_scale", 4.0))
        grid = layout.grid_flow(
            row_major=True, columns=cols, even_columns=True, even_rows=False, align=True
        )

        pcoll = _get_preview_collection()
        for idx, it in enumerate(props.items):
            cell = grid.box()
            col = cell.column(align=True)

            stl = getattr(it, "filepath", "") or ""
            thumb = getattr(it, "thumb", "") or ""
            icon_value = _ensure_thumb_in_pcoll(stl, thumb, pcoll)

            if icon_value:
                col.template_icon(icon_value=icon_value, scale=thumb_scale)
            else:
                col.label(text="(无缩略图)", icon="QUESTION")

            col.label(text=it.display_name, icon="FILE_BLANK")

            row = col.row(align=True)
            op = row.operator("stl_thumb.export_to_stl", text="导出到此", icon="EXPORT")
            op.target = it.filepath
            op2 = row.operator("stl_thumb.delete_stl", text="", icon="TRASH")
            op2.filepath = it.filepath


# ============================================================
#   面板
# ============================================================

class STLTHUMB_PT_export_panel(Panel):
    bl_label = "STL 导出设置"
    bl_idname = "STLTHUMB_PT_export_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "STL-Thumb"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        props = context.scene.stl_thumb_export

        layout.operator(
            "stl_thumb.open_export_panel",
            text="作为浮动弹窗打开",
            icon="WINDOW",
        )
        layout.separator()

        box = layout.box()
        row = box.row(align=True)
        row.prop(props, "folder", text="目标文件夹")
        row.operator("stl_thumb.pick_export_folder", text="", icon="FILE_FOLDER")

        row = box.row(align=True)
        row.prop(props, "scale", text="导出缩放 (×)")
        row.operator("stl_thumb.set_scale_10", text="×10", icon="DRIVER_DISTANCE").value = 10.0
        row.operator("stl_thumb.set_scale_1", text="×1", icon="DRIVER_DISTANCE").value = 1.0

        box.prop(props, "use_selection", text="仅导出选中物体")

        row = box.row(align=True)
        row.prop(props, "new_filename", text="新文件名")
        op = row.operator("stl_thumb.export_to_stl", text="新建导出", icon="EXPORT")
        op.target = ""


class STLTHUMB_PT_thumb_grid_panel(Panel):
    bl_label = "缩略图浏览器"
    bl_idname = "STLTHUMB_PT_thumb_grid_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "STL-Thumb"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        props = context.scene.stl_thumb_export

        layout.operator(
            "stl_thumb.open_thumb_grid_popup",
            text="作为浮动弹窗打开",
            icon="WINDOW",
        )
        layout.separator()

        box = layout.box()
        row = box.row(align=True)
        row.prop(props, "recursive", text="递归")
        row.prop(props, "columns", text="列")
        row.prop(props, "thumb_size", text="尺寸")

        layout.operator("stl_thumb.refresh_export_list", icon="FILE_REFRESH")

        if not props.items:
            col = layout.column(align=True)
            col.label(text="（列表为空 —— 点击上方「刷新缩略图」）", icon="INFO")
            return

        cols = max(1, int(props.columns))
        grid = layout.grid_flow(
            row_major=True, columns=cols, even_columns=True, even_rows=False, align=True
        )

        pcoll = _get_preview_collection()
        for idx, it in enumerate(props.items):
            cell = grid.box()
            col = cell.column(align=True)

            stl = getattr(it, "filepath", "") or ""
            thumb = getattr(it, "thumb", "") or ""
            icon_value = _ensure_thumb_in_pcoll(stl, thumb, pcoll)

            if icon_value:
                col.template_icon(icon_value=icon_value, scale=6)
            else:
                col.label(text="(无缩略图)", icon="QUESTION")

            col.label(text=it.display_name, icon="FILE_BLANK")

            row = col.row(align=True)
            op = row.operator("stl_thumb.export_to_stl", text="导出到此", icon="EXPORT")
            op.target = it.filepath
            op2 = row.operator("stl_thumb.delete_stl", text="", icon="TRASH")
            op2.filepath = it.filepath


# STLTHUMB_PT_main_panel 已移除：批量转换功能改由偏好设置中的「批量转换」块管理。


# ============================================================
#   注册 / 注销
# ============================================================

_classes = (
    STLThumbSceneProps,
    STLThumbExportItem,
    STLThumbExportProps,
    STLThumbAddonPreferences,
    STLTHUMB_OT_convert_folder,
    STLTHUMB_OT_convert_single,
    STLTHUMB_OT_refresh_export_list,
    STLTHUMB_OT_pick_folder,
    STLTHUMB_OT_pick_output,
    STLTHUMB_OT_pick_export_folder,
    STLTHUMB_OT_export_to_stl,
    STLTHUMB_OT_delete_stl,
    STLTHUMB_OT_set_scale_10,
    STLTHUMB_OT_set_scale_1,
    STLTHUMB_OT_pick_conv_input,
    STLTHUMB_OT_pick_conv_output,
    STLTHUMB_OT_convert_folder_pref,
    STLTHUMB_OT_open_export_panel,
    STLTHUMB_OT_open_thumb_grid_popup,
    STLTHUMB_PT_export_panel,
    STLTHUMB_PT_thumb_grid_panel,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.stl_thumb = PointerProperty(type=STLThumbSceneProps)
    bpy.types.Scene.stl_thumb_export = PointerProperty(type=STLThumbExportProps)
    _log("插件已注册 (3D 视图按 N 键 → STL-Thumb 面板)")


def unregister():
    global _preview_collection
    for pcoll in _preview_collection.values():
        bpy.utils.previews.remove(pcoll)
    _preview_collection.clear()
    _pcoll_cache.clear()
    _lazy_restore_done.clear()

    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    if hasattr(bpy.types.Scene, "stl_thumb"):
        del bpy.types.Scene.stl_thumb
    if hasattr(bpy.types.Scene, "stl_thumb_export"):
        del bpy.types.Scene.stl_thumb_export
    _log("插件已注销")


if __name__ == "__main__":
    register()
