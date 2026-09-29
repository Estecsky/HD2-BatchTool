"""顶点组字典的新建、逐行编辑与用户目录保存操作。"""

import bpy
from bpy.props import EnumProperty
from bpy.types import Operator

from ..presets.aq_presets import (
    is_user_preset_id,
    list_aq_presets,
    load_aq_preset,
    save_user_preset,
)
from .blender_lifecycle import register_classes, unregister_classes
from ..utils.utils import HD2BTError


def _report_exception(operator, exc):
    message = str(exc) if isinstance(exc, HD2BTError) else f"{type(exc).__name__}: {exc}"
    operator.report({"ERROR"}, message)
    return {"CANCELLED"}


def _active_rows(settings):
    if settings.dictionary_editor_section == "SNAP":
        return settings.dictionary_snap_rows, "dictionary_snap_index"
    return settings.dictionary_rename_rows, "dictionary_rename_index"


def _replace_rows(collection, pairs):
    collection.clear()
    for source, target in pairs:
        row = collection.add()
        row.source_name = source
        row.target_name = target


def _rows_as_pairs(collection):
    return [(row.source_name, row.target_name) for row in collection]


class HD2BT_OT_dictionary_new(Operator):
    bl_idname = "hd2bt.dictionary_new"
    bl_label = "新建空白字典"
    bl_description = "清空编辑器并开始建立一个新的用户顶点组字典"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.hd2bt_settings
        settings.dictionary_snap_rows.clear()
        settings.dictionary_rename_rows.clear()
        settings.dictionary_snap_index = 0
        settings.dictionary_rename_index = 0
        settings.dictionary_editor_name = "新字典"
        settings.dictionary_editor_section = "SNAP"
        settings.dictionary_editor_expanded = True
        self.report({"INFO"}, "已新建空白字典；添加映射行后点击“保存用户字典”")
        return {"FINISHED"}


class HD2BT_OT_dictionary_load_current(Operator):
    bl_idname = "hd2bt.dictionary_load_current"
    bl_label = "载入当前字典到编辑器"
    bl_description = "把下拉框中的当前字典载入编辑器；内置 mmd通用 会另存为用户副本"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            settings = context.scene.hd2bt_settings
            profile = load_aq_preset(settings.aq_bone_preset)
            _replace_rows(settings.dictionary_snap_rows, profile["snap_pairs"])
            _replace_rows(settings.dictionary_rename_rows, profile["rename_pairs"])
            settings.dictionary_snap_index = 0
            settings.dictionary_rename_index = 0
            settings.dictionary_editor_name = (
                profile["name"]
                if is_user_preset_id(settings.aq_bone_preset)
                else f"{profile['name']}_自定义"
            )
            settings.dictionary_editor_expanded = True
            self.report(
                {"INFO"},
                f"已载入 {len(profile['snap_pairs'])} 条吸附、"
                f"{len(profile['rename_pairs'])} 条重命名映射",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_dictionary_add_row(Operator):
    bl_idname = "hd2bt.dictionary_add_row"
    bl_label = "添加映射行"
    bl_description = "在当前吸附或顶点组重命名列表末尾添加一行"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.hd2bt_settings
        rows, index_name = _active_rows(settings)
        rows.add()
        setattr(settings, index_name, len(rows) - 1)
        return {"FINISHED"}


class HD2BT_OT_dictionary_remove_row(Operator):
    bl_idname = "hd2bt.dictionary_remove_row"
    bl_label = "删除映射行"
    bl_description = "删除当前选中的映射行"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "hd2bt_settings", None)
        return settings is not None and bool(_active_rows(settings)[0])

    def execute(self, context):
        settings = context.scene.hd2bt_settings
        rows, index_name = _active_rows(settings)
        index = min(getattr(settings, index_name), len(rows) - 1)
        rows.remove(index)
        setattr(settings, index_name, max(0, min(index, len(rows) - 1)))
        return {"FINISHED"}


class HD2BT_OT_dictionary_move_row(Operator):
    bl_idname = "hd2bt.dictionary_move_row"
    bl_label = "移动映射行"
    bl_description = "上移或下移当前映射行"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(
        items=(("UP", "上移", ""), ("DOWN", "下移", "")),
        default="UP",
    )

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "hd2bt_settings", None)
        return settings is not None and len(_active_rows(settings)[0]) > 1

    def execute(self, context):
        settings = context.scene.hd2bt_settings
        rows, index_name = _active_rows(settings)
        index = min(getattr(settings, index_name), len(rows) - 1)
        target = index - 1 if self.direction == "UP" else index + 1
        target = max(0, min(target, len(rows) - 1))
        if target != index:
            rows.move(index, target)
            setattr(settings, index_name, target)
        return {"FINISHED"}


class HD2BT_OT_dictionary_save(Operator):
    bl_idname = "hd2bt.dictionary_save"
    bl_label = "保存用户字典"
    bl_description = "把编辑器内容保存到 Blender 用户配置目录；同名旧文件会保留 .bak"

    def execute(self, context):
        try:
            settings = context.scene.hd2bt_settings
            identifier, path = save_user_preset(
                settings.dictionary_editor_name,
                _rows_as_pairs(settings.dictionary_snap_rows),
                _rows_as_pairs(settings.dictionary_rename_rows),
            )
            # 先强制刷新动态枚举，再切换到刚保存的用户字典。
            list_aq_presets()
            settings.aq_bone_preset = identifier
            self.report({"INFO"}, f"已保存并选中用户字典：{path.stem}")
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


CLASSES = (
    HD2BT_OT_dictionary_new,
    HD2BT_OT_dictionary_load_current,
    HD2BT_OT_dictionary_add_row,
    HD2BT_OT_dictionary_remove_row,
    HD2BT_OT_dictionary_move_row,
    HD2BT_OT_dictionary_save,
)
_REGISTERED_CLASSES = []


def register():
    register_classes(CLASSES, _REGISTERED_CLASSES)


def unregister():
    unregister_classes(_REGISTERED_CLASSES)
