"""Blender 属性定义。所有用户可见文本保持中文。"""

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Object, PropertyGroup

from ..presets.aq_presets import list_aq_presets
from ..core.authoring_metadata import (
    PART_LABELS,
    PART_SLOTS,
    VARIANT_BASE,
    VARIANT_BODY_GROUP,
    VARIANT_HELMET_SINGLE,
)
from .blender_lifecycle import register_classes, unregister_classes
from ..core.normal_policy import DEFAULT_WARNING, DEFAULT_LIMIT
from ..core.free_variants import update_label, update_logic
from ..utils.constants import AUTO, THIRD_PERSON


def _armature_poll(_self, obj):
    return obj is not None and obj.type == "ARMATURE"


class HD2BT_DictionaryRow(PropertyGroup):
    """用户字典编辑器中的一行；方向固定为来源名称到潜兵目标骨。"""

    source_name: StringProperty(name="来源骨/顶点组", default="")
    target_name: StringProperty(name="潜兵目标骨", default="")


class HD2BT_FreeDifferenceOption(PropertyGroup):
    identity: StringProperty(name="稳定标识", default="")
    display_name: StringProperty(name="差分名称", default="差分", update=update_label)


class HD2BT_FreeDifferenceGroup(PropertyGroup):
    part_slot: StringProperty(default="")
    display_name: StringProperty(name="差分部位名称", default="差分部位", update=update_label)
    options: CollectionProperty(type=HD2BT_FreeDifferenceOption)
    active_index: IntProperty(default=-1, min=-1)


class HD2BT_Settings(PropertyGroup):
    authoring_difference_logic: EnumProperty(
        name="差分逻辑", items=(("GROUP", "组差分", "具名身体组与头盔单差分"),
                                  ("FREE", "自由搭配差分", "各语义部位独立选择一个差分")),
        default="FREE", update=update_logic,
        description="选择制作方式；不会自动转换已标记对象，同一封包域不得混用两种逻辑",
    )
    free_variant_kind: EnumProperty(
        name="类型", items=(("BASE", "基础部位", "自由搭配时始终显示"),
                             ("FREE_PART", "差分部位", "需要选中本部位的活动差分项")),
        default="BASE",
    )
    free_difference_groups: CollectionProperty(type=HD2BT_FreeDifferenceGroup)
    free_difference_error: StringProperty(default="", options={"SKIP_SAVE"})
    publish_rig_gender: EnumProperty(
        name="发布骨架类型",
        items=(("UNSET", "未设置（保存前必选）", "未声明；最终保存 Mod 时会报错，不按外观猜测"),
               ("MALE", "男性", "最终包启用通用重定向，保持原版腿部动作；制作时与原潜兵站姿对齐"),
               ("FEMALE", "女性", "最终包启用通用重定向与女性腿部修正；仅支持当前直腿制作站姿")),
        default="UNSET",
        description="仅为发布声明，不改变导入的工作骨架；最终保存 Mod 时写入运行时包",
    )
    mmd_source_armature: PointerProperty(
        name="来源骨架",
        type=Object,
        poll=_armature_poll,
        description="任意来源骨架；留空时使用选中的另一个 Armature",
    )
    aq_bone_preset: EnumProperty(
        name="顶点组字典",
        items=list_aq_presets,
        description="读取内置 mmd通用与 Blender 用户配置目录中的自建两列表字典",
    )
    dictionary_editor_expanded: BoolProperty(
        name="管理顶点组字典",
        default=False,
        options={"SKIP_SAVE"},
        description="展开新建、编辑和保存用户顶点组字典的面板",
    )
    dictionary_editor_name: StringProperty(
        name="字典名称",
        default="新字典",
        options={"SKIP_SAVE"},
    )
    dictionary_editor_section: EnumProperty(
        name="编辑内容",
        items=(
            ("SNAP", "骨骼吸附", "编辑来源骨到潜兵目标骨的吸附映射"),
            ("RENAME", "顶点组重命名", "编辑来源顶点组到潜兵骨名的重命名映射"),
        ),
        default="SNAP",
        options={"SKIP_SAVE"},
    )
    dictionary_snap_rows: CollectionProperty(type=HD2BT_DictionaryRow)
    dictionary_rename_rows: CollectionProperty(type=HD2BT_DictionaryRow)
    dictionary_snap_index: IntProperty(default=0, min=0, options={"SKIP_SAVE"})
    dictionary_rename_index: IntProperty(default=0, min=0, options={"SKIP_SAVE"})
    unmapped_weight_mode: EnumProperty(
        name="未映射权重",
        items=(
            (
                "KEEP",
                "保留未映射组（用户手修）",
                "只重命名字典明确列出的组；其余顶点组原样保留",
            ),
            (
                "COLLAPSE_TO_PARENT",
                "合并到最近已映射父骨（偷懒专用）",
                "不添加骨骼；沿来源骨架父链把自定义骨权重合并到最近的潜兵骨",
            ),
        ),
        default="KEEP",
    )
    auto_scale_character: BoolProperty(
        name="人物统一缩放",
        default=False,
        description=(
            "吸附前按潜兵头高与脚底统一缩放人物；关闭时保留来源人物当前位置和大小，"
            "只让工作骨架吸附到人物，头部对齐偏移不生效。关闭不会撤销之前已做的缩放"
        ),
    )
    head_alignment_offset: FloatProperty(
        name="头部对齐偏移",
        default=-0.015,
        min=-0.10,
        max=0.05,
        subtype="DISTANCE",
        unit="LENGTH",
        description=(
            "锁定人物可见脚底后统一缩放，使 head 骨与原潜兵 head 骨对齐；"
            "负值通过缩放让头部略低，不会把脚压到地面以下"
        ),
    )
    rig_kind_override: EnumProperty(
        name="骨架类型",
        items=(
            (AUTO, "自动识别", "优先根据 BonesID / StateMachineID 识别"),
            (THIRD_PERSON, "第三人称", "强制把活动骨架视为第三人称骨架"),
        ),
        default=AUTO,
    )
    grip_adjust_rotation: BoolProperty(
        name="同时修正持握骨朝向",
        default=True,
        description="开启后也传递原版持握骨相对手掌的朝向，关闭后只修正位置。",
    )
    authoring_part_slot: EnumProperty(
        name="指定部位",
        # .blend 保存枚举数值：为旧七部位保留原序号，新增胸甲独占 7。
        items=tuple((slot, PART_LABELS[slot], slot,
                     {"Hips": 0, "LeftArm": 1, "RightArm": 2, "Torso": 3,
                      "LeftLeg": 4, "RightLeg": 5, "Head": 6, "Torso_Armor": 7}[slot])
                    for slot in PART_SLOTS),
        default="Torso",
        description="只保存语义部位；真实 Unit ID 由 AQSDK 根据当前护甲 archive 临时解析",
    )
    authoring_variant_kind: EnumProperty(
        name="输出类型",
        items=(
            (VARIANT_BASE, "基础部位", "当前部位的基础显示模型"),
            (
                VARIANT_BODY_GROUP,
                "身体差分组成员",
                "加入具名身体差分组；同组每个身体部位最多一个差分",
            ),
            (
                VARIANT_HELMET_SINGLE,
                "头盔单差分",
                "头盔只有一个 Unit，每个差分单独命名",
            ),
        ),
        default=VARIANT_BASE,
    )
    base_group_name: StringProperty(
        name="基础组名称",
        default="默认外观",
        description="唯一的默认显示组；差分开启时只替换其中与差分同部位的对象",
    )
    body_difference_group_name: StringProperty(
        name="身体差分组名称",
        default="",
        description="同名组的不同部位会在游戏面板中合并为一个统一开关",
    )
    helmet_difference_name: StringProperty(
        name="头盔差分名称",
        default="",
        description="游戏面板中显示的单个头盔差分名称",
    )
    # UI 运行状态不写入 .blend。
    cut_normal_warning: FloatProperty(
        name="法向警告值", default=DEFAULT_WARNING, min=1.0e-6, max=2.0, precision=6,
        description="原法向与写回单位法向之差的长度；超过时提示但保留切割结果。建议 0.001",
    )
    cut_normal_limit: FloatProperty(
        name="法向阻断值", default=DEFAULT_LIMIT, min=1.0e-6, max=2.0, precision=6,
        description="超过此误差才取消切割并恢复源对象；应不小于警告值。调大前检查切口接缝",
    )
    last_cut_summary: StringProperty(default="", options={"SKIP_SAVE"})
    last_cut_warning: BoolProperty(default=False, options={"SKIP_SAVE"})
    last_mapping_summary: StringProperty(default="", options={"SKIP_SAVE"})


CLASSES = (HD2BT_DictionaryRow, HD2BT_FreeDifferenceOption, HD2BT_FreeDifferenceGroup, HD2BT_Settings)
_REGISTERED_CLASSES = []
_SCENE_SETTINGS_OWNED = False


def register():
    global _SCENE_SETTINGS_OWNED
    if hasattr(bpy.types.Scene, "hd2bt_settings"):
        raise RuntimeError("Scene.hd2bt_settings 已存在，拒绝重复安装或双模块名同时启用")
    register_classes(CLASSES, _REGISTERED_CLASSES)
    try:
        bpy.types.Scene.hd2bt_settings = PointerProperty(type=HD2BT_Settings)
        _SCENE_SETTINGS_OWNED = True
    except Exception:
        unregister_classes(_REGISTERED_CLASSES)
        raise


def unregister():
    global _SCENE_SETTINGS_OWNED
    if _SCENE_SETTINGS_OWNED:
        if not hasattr(bpy.types.Scene, "hd2bt_settings"):
            raise RuntimeError("本模块记录的 Scene.hd2bt_settings 已被外部删除")
        del bpy.types.Scene.hd2bt_settings
        _SCENE_SETTINGS_OWNED = False
    unregister_classes(_REGISTERED_CLASSES)
