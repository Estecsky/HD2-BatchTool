"""HD2BatchTool Blender 4.x 插件入口。"""

import importlib
import sys
import bpy

if "_version" in globals():
    _version = importlib.reload(_version)
else:
    from . import version as _version

VERSION = _version.VERSION
VERSION_TEXT = _version.VERSION_TEXT

bl_info = {
    "name": "Helldivers 2 Batch Tool",
    "author": "AQ-Echoo,WisA9",
    "version": (0, 9, 0),
    "blender": (4, 0, 0),
    "location": "3D View > 侧边栏 > HD2 Batch Tool",
    "description": "人物骨架吸附、权重重命名与身体七部位切分",
    "warning": "",
    "category": "Import-Export",
}

# 覆盖安装或显式 reload 前先让旧模块对象完成注销。0.8.0 尚无所有权列表，
# 因此兼容路径只清理 Blender 仍持有的旧 class object identity。
def _class_is_registered(cls):
    return bool(cls is not None and getattr(cls, "is_registered", False))


_old_unregister = globals().get("unregister")
_old_modules = [
    globals().get(name)
    for name in ("operators", "dictionary_editor", "properties")
]
_old_groups = [globals().get("CLASSES", ())]
_old_groups.extend(getattr(module, "CLASSES", ()) for module in _old_modules if module is not None)
_old_state_present = (
    globals().get("_REGISTERED", False)
    or globals().get("_DIRTY", False)
    or any(
        _class_is_registered(cls)
        for group in _old_groups
        for cls in group
    )
)
if callable(_old_unregister) and _old_state_present:
    try:
        _old_unregister()
    except Exception:
        # 兼容 0.8.0 的非事务注销：逐个清理由旧模块对象声明的 HD2BT 类。
        cleanup_errors = []
        old_properties = globals().get("properties")
        old_settings = getattr(old_properties, "HD2BT_Settings", None)
        if (
            _class_is_registered(old_settings)
            and hasattr(bpy.types.Scene, "hd2bt_settings")
        ):
            try:
                del bpy.types.Scene.hd2bt_settings
            except Exception as exc:
                cleanup_errors.append(f"Scene.hd2bt_settings: {exc}")
        for group in _old_groups:
            for cls in reversed(tuple(group)):
                if not _class_is_registered(cls):
                    continue
                try:
                    bpy.utils.unregister_class(cls)
                except Exception as exc:
                    cleanup_errors.append(f"{cls.__name__}: {exc}")
        if cleanup_errors:
            raise RuntimeError("覆盖安装前无法清理旧 Blender 类：" + "; ".join(cleanup_errors))
del _class_is_registered, _old_unregister, _old_modules, _old_groups, _old_state_present

# 中文注释：Blender 覆盖安装插件时，旧版子模块可能仍留在当前 Python 进程的
# sys.modules 中。这里按完整依赖顺序刷新发行包中的每个业务模块，避免新版 Operator
# 继续绑定旧版算法函数。

_SUBMODULE_ORDER = (
    "utils.constants",
    "utils.utils",
    "ui.blender_lifecycle",
    "presets.aq_presets",
    "presets.mapping_profiles",
    "utils.reference",
    "core.authoring_metadata",
    "core.free_variants",
    "core.normal_policy",
    "core.weight_validation",
    "workflows.physics_cut",
    "workflows.armor_workflow",
    "utils.rigging",
    "workflows.facial_controls",
    "workflows.mmd_mapping",
    "ui.properties",
    "ui.dictionary_editor",
    "ui.operators",
    "ui.panels",
)
for _module_name in _SUBMODULE_ORDER:
    _binding_name = _module_name.rsplit(".", 1)[-1]
    _qualified_name = f"{__name__}.{_module_name}"
    _module = sys.modules.get(_qualified_name)
    if _module is None:
        _module = importlib.import_module(f".{_module_name}", __name__)
    else:
        _module = importlib.reload(_module)
    globals()[_binding_name] = _module
del _binding_name, _qualified_name, _module_name, _module

# 外部 PhysBoneTool 仍使用旧模块路径；仅建立别名，不保留重复源码。
# 每次热重载后重新绑定，确保兼容路径与新路径指向同一个模块对象。
_COMPAT_MODULES = {
    "bone_snap": "presets",
    "aq_presets": "presets.aq_presets",
    "bone_snap.mapping_profiles": "presets.mapping_profiles",
    "bone_snap.dictionary_editor": "ui.dictionary_editor",
    "authoring_metadata": "core.authoring_metadata",
    "free_variants": "core.free_variants",
    "normal_policy": "core.normal_policy",
    "weight_validation": "core.weight_validation",
    "armor_workflow": "workflows.armor_workflow",
    "physics_cut": "workflows.physics_cut",
    "mmd_mapping": "workflows.mmd_mapping",
    "facial_controls": "workflows.facial_controls",
    "blender_lifecycle": "ui.blender_lifecycle",
    "operators": "ui.operators",
    "properties": "ui.properties"
}
for _old_path, _new_path in _COMPAT_MODULES.items():
    _alias_name = f"{__name__}.{_old_path}"
    _alias_module = sys.modules[f"{__name__}.{_new_path}"]
    sys.modules[_alias_name] = _alias_module
    _parent_name, _child_name = _alias_name.rsplit('.', 1)
    setattr(sys.modules[_parent_name], _child_name, _alias_module)
del _old_path, _new_path, _alias_name, _alias_module, _parent_name, _child_name


CLASSES = panels.CLASSES

_REGISTERED_CLASSES = []
_REGISTERED = False
_DIRTY = False


def _owned_state_present():
    if _REGISTERED_CLASSES:
        return True
    for module in (properties, dictionary_editor, operators):
        if getattr(module, "_REGISTERED_CLASSES", ()):
            return True
    return bool(
        getattr(properties, "_SCENE_SETTINGS_OWNED", False)
    )


def _cleanup_steps():
    return (
        lambda: blender_lifecycle.unregister_classes(_REGISTERED_CLASSES),
        operators.unregister,
        dictionary_editor.unregister,
        properties.unregister,
    )

def register():
    global _DIRTY, _REGISTERED
    if _DIRTY:
        raise RuntimeError("上次 HD2 Batch Tool 清理不完整，请先重试禁用或重启 Blender")
    if _REGISTERED:
        return
    try:
        properties.register()
        dictionary_editor.register()
        operators.register()
        blender_lifecycle.register_classes(CLASSES, _REGISTERED_CLASSES)
    except Exception as exc:
        rollback_errors = []
        for rollback in _cleanup_steps():
            try:
                rollback()
            except Exception as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        remaining = _owned_state_present()
        _REGISTERED = remaining
        _DIRTY = remaining
        if not remaining:
            aq_presets.clear_runtime_cache()
        if rollback_errors or remaining:
            details = " | ".join(rollback_errors) or "仍存在本模块拥有的 Blender 状态"
            raise RuntimeError(f"HD2 Batch Tool 注册失败且回滚不完整：{details}") from exc
        raise
    _REGISTERED = True
    _DIRTY = False


def unregister():
    global _DIRTY, _REGISTERED
    errors = []
    for cleanup in _cleanup_steps():
        try:
            cleanup()
        except Exception as exc:
            errors.append(str(exc))
    remaining = _owned_state_present()
    _REGISTERED = remaining
    _DIRTY = remaining
    if not remaining:
        aq_presets.clear_runtime_cache()
    if errors or remaining:
        details = " | ".join(errors) or "仍存在本模块拥有的 Blender 状态"
        raise RuntimeError("HD2 Batch Tool 注销不完整：" + details)


if __name__ == "__main__":
    register()
