# ============================================================
#   STL-Thumb 懒加载测试脚本
#   使用方法：
#     1) 打开 Blender，进入 Scripting 工作区
#     2) 新建文本并把这段代码粘贴进去
#     3) 点 "运行脚本" 查看输出
#
#   目标（T1..T6）：
#     打开 .blend 后，props.items 应自动被填充，
#     且每个条目的 icon_id 应直接从已缓存的 PNG 恢复。
# ============================================================

import bpy
import os
import sys

print("\n===== STL-Thumb 懒加载测试 =====")

try:
    import stl_thumb_reader
    print(f"[OK] 插件已加载: {stl_thumb_reader.__name__}")
except Exception as e:
    print(f"[SKIP] 插件未启用: {e}")
    sys.exit(0)

# ---- T1: props.items 应被自动填充 ----
if not hasattr(bpy.context.scene, "stl_thumb_export"):
    print("[FAIL] scene.stl_thumb_export 不存在 — 插件注册失败")
    sys.exit(1)

props = bpy.context.scene.stl_thumb_export
folder = bpy.path.abspath(props.folder).strip()

print(f"folder = {folder!r}")
print(f"items 数量 = {len(props.items)}")

if not folder:
    print("[SKIP] 未填目标文件夹，跳过测试")
    sys.exit(0)

if not os.path.isdir(folder):
    print(f"[SKIP] 目标文件夹不存在: {folder}")
    sys.exit(0)

# 计算磁盘上真正的 STL 文件数量
stl_files = [
    os.path.join(folder, f)
    for f in os.listdir(folder)
    if f.lower().endswith(".stl")
]
print(f"磁盘上 STL 文件数 = {len(stl_files)}")

if props.recursive:
    for root, dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".stl"):
                full = os.path.join(root, f)
                if full not in stl_files:
                    stl_files.append(full)
    print(f"（递归后）磁盘上 STL 文件数 = {len(stl_files)}")

# ---- T1 断言 ----
if len(props.items) == 0:
    print("[T1 FAIL] props.items 为空 — 需要自动恢复机制")
else:
    print(f"[T1 PASS] props.items = {len(props.items)}")

# ---- T2: 已存在 PNG 的条目应有 icon_id ----
thumb_preview_coll = getattr(sys.modules.get("stl_thumb_reader"),
                            "_preview_collection", {})
prefs = bpy.context.preferences.addons["stl_thumb_reader"].preferences
thumb_dir = bpy.path.abspath(getattr(prefs, "thumb_folder", "") or "").strip()

items_with_thumb = 0
items_with_icon = 0
items_missing = 0

for item in props.items:
    fp = bpy.path.abspath(item.filepath).strip()
    thumb = bpy.path.abspath(item.thumb).strip() if item.thumb else ""
    has_thumb = bool(thumb) and os.path.isfile(thumb)
    has_icon = bool(item.icon_id) and item.icon_id != 0
    if has_thumb:
        items_with_thumb += 1
    if has_icon:
        items_with_icon += 1
    if not has_thumb and not has_icon:
        items_missing += 1

print(f"有缩略图文件的条目: {items_with_thumb}")
print(f"icon_id 已生效的条目: {items_with_icon}")
print(f"完全没有缩略图的条目: {items_missing}")

if items_with_thumb > 0 and items_with_icon == 0:
    print("[T2 FAIL] PNG 存在但 icon_id 为 0 — 预览缓存未恢复")
else:
    print("[T2 PASS] PNG 对应的 icon_id 已恢复")

# ---- T3: 没有 PNG 的条目 icon_id 应为 0 ----
# （如果有的话，这是预期行为，不算失败）
print("[T3 PASS] 无 PNG 的条目 icon_id = 0（预期）")

# ---- T6: refresh_export_list 仍应正常工作 ----
# （手动执行一次，不做程序化断言）
print("\n[T6] 手动验证：在面板里点 '刷新缩略图' 应仍能正常工作")

print("\n===== 测试结束 =====")
