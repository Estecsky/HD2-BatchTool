"""两列表顶点组字典兼容层。

直接读取 ``bone_name_list/*.py`` 格式。为了让公开插件不会执行用户字典中的任意
Python，只解析两个列表赋值，不 import 字典文件。
"""

from __future__ import annotations

import ast
import os
import secrets
import shutil
from pathlib import Path

from ..utils.constants import addon_root
from ..utils.utils import HD2BTError


PRESET_DIR = addon_root() / "presets" / "bone_name_list"
USER_PRESET_PREFIX = "USER::"
USER_PRESET_ENV = "HD2BT_USER_PRESET_DIR"

# Blender 的动态 EnumProperty 不会自行持有回调返回的 Python 字符串；临时字符串被
# 回收后，中文标签会显示成随机乱码。字符串池和列表必须在插件整个生命周期内存活。
_PRESET_ENUM_STRING_POOL = {}
_PRESET_ENUM_ROWS = []


def _stable_enum_string(value):
    value = str(value)
    return _PRESET_ENUM_STRING_POOL.setdefault(value, value)


def user_preset_dir(*, create=False):
    """返回可写的用户字典目录；测试可以用环境变量隔离真实 Blender 配置。"""

    override = os.environ.get(USER_PRESET_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    import bpy

    value = bpy.utils.user_resource(
        "CONFIG",
        path="HD2BatchTool/bone_name_list",
        create=create,
    )
    return Path(value)


def make_user_preset_id(filename):
    return USER_PRESET_PREFIX + Path(filename).name


def is_user_preset_id(identifier):
    return str(identifier).startswith(USER_PRESET_PREFIX)


def _preset_rows(directory, *, user=False):
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob("*.py"), key=lambda item: item.name.casefold())
    rows = []
    for path in paths:
        identifier = make_user_preset_id(path.name) if user else path.name
        label = f"{path.stem}（用户）" if user else path.stem
        rows.append((identifier, label, str(path)))
    return rows


def list_aq_presets(_self=None, _context=None):
    """Blender EnumProperty 回调；同时热更新内置目录和可写的用户目录。"""

    rows = []
    builtin_rows = _preset_rows(PRESET_DIR)
    builtin_rows.sort(key=lambda row: (0 if row[0] == "mmd通用.py" else 1, row[0].casefold()))
    rows.extend(builtin_rows)
    rows.extend(_preset_rows(user_preset_dir(create=False), user=True))
    if not rows:
        rows.append(
            tuple(
                _stable_enum_string(value)
                for value in ("", "未找到顶点组字典", str(PRESET_DIR))
            )
        )
    # 原地刷新以保留 Blender 已取得的列表引用；字符串则由上面的池永久持有。
    _PRESET_ENUM_ROWS[:] = [
        tuple(_stable_enum_string(value) for value in row)
        for row in rows
    ]
    return _PRESET_ENUM_ROWS


def clear_runtime_cache():
    """插件注销后释放动态枚举引用"""

    _PRESET_ENUM_ROWS.clear()
    _PRESET_ENUM_STRING_POOL.clear()


def _safe_user_filename(name):
    value = str(name).strip()
    if value.lower().endswith(".py"):
        value = value[:-3].rstrip()
    if not value:
        raise HD2BTError("请输入字典名称")
    if any(character in value for character in '<>:"/\\|?*'):
        raise HD2BTError("字典名称不能包含 Windows 文件名保留字符")
    if value.endswith((".", " ")):
        raise HD2BTError("字典名称不能以句点或空格结尾")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{index}" for index in range(1, 10))
    reserved.update(f"LPT{index}" for index in range(1, 10))
    device_name = value.split(".", 1)[0].rstrip(" .").upper()
    if device_name in reserved:
        raise HD2BTError("该名称是 Windows 保留文件名")
    return value + ".py"


def _validated_pairs(pairs, label):
    result = []
    for index, pair in enumerate(pairs):
        if len(pair) != 2:
            raise HD2BTError(f"{label}第 {index + 1} 行不是两个骨名")
        source, target = (str(value).strip() for value in pair)
        if not source or not target:
            raise HD2BTError(f"{label}第 {index + 1} 行存在空骨名")
        result.append((source, target))
    return result


def _serialize_pairs(variable_name, pairs):
    lines = [f"{variable_name} = ["]
    lines.extend(f"    [{source!r}, {target!r}]," for source, target in pairs)
    lines.append("]")
    return "\n".join(lines)


def save_user_preset(name, snap_pairs, rename_pairs):
    """按 AQ 两列表格式安全写入用户目录，并为被覆盖文件保留一份 .bak。"""

    filename = _safe_user_filename(name)
    if filename.casefold() == "mmd通用.py".casefold():
        raise HD2BTError("“mmd通用”是内置字典，请换一个名称保存用户副本")
    snap_pairs = _validated_pairs(snap_pairs, "骨骼吸附")
    rename_pairs = _validated_pairs(rename_pairs, "顶点组重命名")
    directory = user_preset_dir(create=True).resolve()
    path = (directory / filename).resolve()
    try:
        path.relative_to(directory)
    except ValueError as exc:
        raise HD2BTError("用户字典必须位于 Blender 用户配置目录") from exc

    source = "\n".join(
        (
            "# -*- coding: utf-8 -*-",
            "# HD2 Batch Tool 用户顶点组字典",
            "# 列表方向：[来源骨/顶点组, 潜兵目标骨]。",
            "",
            _serialize_pairs("snap_bone_fixed_name_list", snap_pairs),
            "",
            _serialize_pairs("rename_vg_fixed_name_list", rename_pairs),
            "",
        )
    )
    token = f".{os.getpid()}.{secrets.token_hex(8)}.tmp"
    temporary = path.with_name(path.name + token)
    backup = path.with_suffix(path.suffix + ".bak")
    backup_temporary = backup.with_name(backup.name + token)
    previous_backup_temporary = backup.with_name(backup.name + ".previous" + token)
    preserve_previous_backup = False
    try:
        temporary.write_text(source, encoding="utf-8", newline="\n")
        # 备份也先写唯一临时文件再原子替换。复制中断时旧 .bak 仍可用，
        # 多个 Blender 进程也不会争用同一个固定 .tmp 文件名。
        if path.is_file():
            had_previous_backup = backup.is_file()
            if had_previous_backup:
                shutil.copy2(backup, previous_backup_temporary)
            shutil.copy2(path, backup_temporary)
            os.replace(backup_temporary, backup)
            try:
                os.replace(temporary, path)
            except Exception as replace_exc:
                try:
                    if had_previous_backup:
                        os.replace(previous_backup_temporary, backup)
                    elif backup.exists():
                        backup.unlink()
                except Exception as restore_exc:
                    preserve_previous_backup = previous_backup_temporary.exists()
                    recovery_hint = (
                        f"；旧备份副本保留在：{previous_backup_temporary}"
                        if preserve_previous_backup
                        else ""
                    )
                    raise HD2BTError(
                        f"用户字典保存失败，且旧备份恢复失败：{restore_exc}"
                        f"{recovery_hint}"
                    ) from replace_exc
                raise
        else:
            os.replace(temporary, path)
    finally:
        for leftover in (temporary, backup_temporary, previous_backup_temporary):
            if preserve_previous_backup and leftover == previous_backup_temporary:
                continue
            if leftover.exists():
                leftover.unlink()
    return make_user_preset_id(path.name), path


def _literal_assignment(module, variable_name, path):
    for node in module.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == variable_name for target in targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError, SyntaxError) as exc:
            raise HD2BTError(f"顶点组字典 {path.name} 的 {variable_name} 不是纯列表") from exc
        if not isinstance(value, list):
            raise HD2BTError(f"顶点组字典 {path.name} 的 {variable_name} 必须是列表")
        result = []
        for index, pair in enumerate(value):
            if (
                not isinstance(pair, (list, tuple))
                or len(pair) != 2
                or not all(isinstance(item, str) and item.strip() for item in pair)
            ):
                raise HD2BTError(
                    f"顶点组字典 {path.name} 的 {variable_name}[{index}] 必须是两个骨名"
                )
            result.append((pair[0].strip(), pair[1].strip()))
        return result
    raise HD2BTError(f"顶点组字典 {path.name} 缺少 {variable_name}")


def load_aq_preset(filename):
    """读取顶点组字典并转换成插件内部 Profile；不执行字典里的任何代码。"""

    if not filename:
        raise HD2BTError(f"未选择顶点组字典；字典目录：{PRESET_DIR}")
    is_user = is_user_preset_id(filename)
    raw_filename = str(filename)[len(USER_PRESET_PREFIX):] if is_user else str(filename)
    directory = user_preset_dir(create=False) if is_user else PRESET_DIR
    path = (directory / Path(raw_filename).name).resolve()
    try:
        path.relative_to(directory.resolve())
    except ValueError as exc:
        raise HD2BTError("顶点组字典不在允许的字典目录中") from exc
    if not path.is_file():
        raise HD2BTError(f"顶点组字典不存在：{path}")
    try:
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise HD2BTError(f"无法读取顶点组字典 {path.name}：{exc}") from exc

    snap_pairs = _literal_assignment(module, "snap_bone_fixed_name_list", path)
    rename_pairs = _literal_assignment(module, "rename_vg_fixed_name_list", path)

    # 列表方向固定为 [来源骨, 潜兵目标骨]。同一目标允许多个来源别名。
    snap_map = {}
    weight_map = {}
    for source, target in snap_pairs:
        snap_map.setdefault(target, []).append(source)
    for source, target in rename_pairs:
        weight_map.setdefault(target, []).append(source)
    return {
        "schema_version": 1,
        "name": path.stem,
        "source": "user_bone_name_list" if is_user else "bone_name_list",
        "path": str(path),
        "source_path": str(path),
        "snap_pairs": snap_pairs,
        "rename_pairs": rename_pairs,
        "snap_map": snap_map,
        "weight_map": weight_map,
    }
