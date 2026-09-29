"""跨 HD2 制作工具共享的语义部位与差分元数据。

本模块不依赖 Blender，既方便单元测试，也让 AQSDK/运行时可以复用同一份
字段合同。Batch Tool 只负责把作者意图写到对象上；真实 Unit ID 由 AQSDK
在保存事务中根据当前 archive 临时解析。
"""

from __future__ import annotations

from dataclasses import dataclass


SCHEMA_NAME = "HD2AuthoringMetadata1"

PART_SLOTS = (
    "Head",
    "LeftArm",
    "RightArm",
    "Torso",
    "Torso_Armor",
    "Hips",
    "LeftLeg",
    "RightLeg",
)
HELMET_PART_SLOT = "Head"
BODY_PART_SLOTS = tuple(slot for slot in PART_SLOTS if slot != HELMET_PART_SLOT)

PART_LABELS = {
    "Head": "头盔",
    "LeftArm": "左臂",
    "RightArm": "右臂",
    "Torso": "胸部/上躯干",
    "Torso_Armor": "胸甲",
    "Hips": "胯部/下躯干",
    "LeftLeg": "左腿",
    "RightLeg": "右腿",
}

VARIANT_BASE = "BASE"
VARIANT_BODY_GROUP = "BODY_GROUP"
VARIANT_HELMET_SINGLE = "HELMET_SINGLE"
VARIANT_FREE_PART = "FREE_PART"
LOGIC_GROUP = "GROUP"
LOGIC_FREE = "FREE"
VARIANT_KINDS = (VARIANT_BASE, VARIANT_BODY_GROUP, VARIANT_HELMET_SINGLE, VARIANT_FREE_PART)

PROP_SCHEMA = "HD2BT_AuthoringSchema"
PROP_PART_SLOT = "HD2BT_PartSlot"
PROP_VARIANT_KIND = "HD2BT_VariantKind"
PROP_BASE_GROUP = "HD2BT_BaseGroup"
PROP_DIFFERENCE_GROUP = "HD2BT_DifferenceGroup"
PROP_DIFFERENCE_NAME = "HD2BT_DifferenceName"
PROP_DIFFERENCE_LOGIC = "HD2BT_DifferenceLogic"
PROP_DIFFERENCE_ID = "HD2BT_DifferenceId"
PROP_DIFFERENCE_ORDER = "HD2BT_DifferenceOrder"

# 这些字段把对象绑定到某一个真实 Unit。语义指定后必须清除，防止换 archive
# 时沿用旧目标。MeshInfoIndex/BoneInfoIndex 描述 Unit 内网格布局，暂由 AQSDK
# 的保存适配层处理，因此这里不擅自删除。
TARGET_ID_PROPERTIES = (
    "Z_ObjectID",
    "Z_SwapID",
    "Z_SwapID_0",
    "Z_SwapID_1",
    "Z_SwapID_2",
    "Z_SwapID_3",
    "Z_SwapID_4",
)


class MetadataError(ValueError):
    """作者元数据不满足封包合同。"""


@dataclass(frozen=True)
class Assignment:
    object_name: str
    part_slot: str
    variant_kind: str = VARIANT_BASE
    difference_group: str = ""
    difference_name: str = ""
    base_group: str = ""
    difference_logic: str = LOGIC_GROUP
    difference_id: str = ""
    difference_order: int = 0


def clean_display_name(value, field_label):
    value = str(value or "").strip()
    if not value:
        raise MetadataError(f"{field_label}不能为空")
    if len(value) > 64:
        raise MetadataError(f"{field_label}不能超过 64 个字符")
    if any(character in value for character in "\r\n\t"):
        raise MetadataError(f"{field_label}不能包含换行或制表符")
    return value


def normalize_assignment(
    object_name,
    part_slot,
    variant_kind=VARIANT_BASE,
    difference_group="",
    difference_name="",
    base_group="",
    difference_logic=LOGIC_GROUP,
    difference_id="",
    difference_order=0,
):
    object_name = str(object_name or "").strip() or "<未命名对象>"
    part_slot = str(part_slot or "").strip()
    variant_kind = str(variant_kind or "").strip()
    if part_slot not in PART_SLOTS:
        raise MetadataError(f"未知部位：{part_slot or '<空>'}")
    if variant_kind not in VARIANT_KINDS:
        raise MetadataError(f"未知输出类型：{variant_kind or '<空>'}")
    if difference_logic not in (LOGIC_GROUP, LOGIC_FREE):
        raise MetadataError(f"未知差分逻辑：{difference_logic}")
    if variant_kind == VARIANT_FREE_PART:
        if difference_logic != LOGIC_FREE:
            raise MetadataError("自由搭配差分必须使用 FREE 差分逻辑")
        group = clean_display_name(difference_group, "差分部位名称")
        name = clean_display_name(difference_name, "差分名称")
        identity = clean_display_name(difference_id, "差分稳定标识")
        if isinstance(difference_order, bool) or not isinstance(difference_order, int) or difference_order < 0:
            raise MetadataError("差分顺序必须是非负整数")
        return Assignment(object_name, part_slot, variant_kind, group, name, "", LOGIC_FREE, identity, difference_order)
    if difference_logic == LOGIC_FREE and variant_kind != VARIANT_BASE:
        raise MetadataError("自由搭配模式只能包含基础部位与自由差分")

    if variant_kind == VARIANT_BASE:
        # 没有新字段的旧自动切割工程统一迁移到兼容名称；新 UI 会显式传入作者命名。
        group = clean_display_name(base_group or "基础组", "基础组名称")
        return Assignment(object_name, part_slot, variant_kind, "", "", group, difference_logic)
    if variant_kind == VARIANT_BODY_GROUP:
        if part_slot not in BODY_PART_SLOTS:
            raise MetadataError("身体差分组不能包含头盔；头盔请使用“头盔单差分”")
        group = clean_display_name(difference_group, "身体差分组名称")
        return Assignment(object_name, part_slot, variant_kind, group, "", "")

    if part_slot != HELMET_PART_SLOT:
        raise MetadataError("头盔单差分只能指定到 Head/头盔部位")
    name = clean_display_name(difference_name, "头盔差分名称")
    return Assignment(object_name, part_slot, variant_kind, "", name, "")


def validate_assignments(assignments):
    """验证整份工程并返回标准化记录。"""

    normalized = [
        normalize_assignment(
            row.object_name,
            row.part_slot,
            row.variant_kind,
            row.difference_group,
            row.difference_name,
            row.base_group,
            row.difference_logic,
            row.difference_id,
            row.difference_order,
        )
        for row in assignments
    ]
    body_members = {}
    helmet_names = {}
    base_members = {}
    base_names = set()
    domain_logics = {}
    free_groups = {}
    free_ids = {}
    free_names = {}
    free_orders = {}
    for row in normalized:
        domain = "头盔" if row.part_slot == HELMET_PART_SLOT else "身体"
        previous_logic = domain_logics.setdefault(domain, row.difference_logic)
        if previous_logic != row.difference_logic:
            raise MetadataError(f"{domain}域不能混合组差分与自由搭配差分；切换选单不会转换已标记对象")
        if row.variant_kind == VARIANT_BASE:
            base_names.add(row.base_group.casefold())
            previous = base_members.get(row.part_slot)
            if previous is not None:
                raise MetadataError(
                    f"基础组的 {PART_LABELS[row.part_slot]} 同时包含 {previous} 与 "
                    f"{row.object_name}；每个部位只能有一个基础对象"
                )
            base_members[row.part_slot] = row.object_name
        elif row.variant_kind == VARIANT_BODY_GROUP:
            key = (row.difference_group.casefold(), row.part_slot)
            previous = body_members.get(key)
            if previous is not None:
                raise MetadataError(
                    f"身体差分组“{row.difference_group}”的 {PART_LABELS[row.part_slot]} "
                    f"同时包含 {previous} 与 {row.object_name}；每个部位只能有一个差分"
                )
            body_members[key] = row.object_name
        elif row.variant_kind == VARIANT_HELMET_SINGLE:
            key = row.difference_name.casefold()
            previous = helmet_names.get(key)
            if previous is not None:
                raise MetadataError(
                    f"头盔差分名称“{row.difference_name}”同时用于 {previous} 与 "
                    f"{row.object_name}；每个具名头盔差分只能有一个对象"
                )
            helmet_names[key] = row.object_name
        elif row.variant_kind == VARIANT_FREE_PART:
            previous_group = free_groups.setdefault(row.part_slot, row.difference_group)
            if previous_group != row.difference_group:
                raise MetadataError(f"{PART_LABELS[row.part_slot]} 只能有一个自由差分部位组")
            if row.difference_id in free_ids:
                raise MetadataError("每个自由差分项只能标记一个网格；请先合并所选网格或添加另一个差分项")
            free_ids[row.difference_id] = row.object_name
            for target, key, label in (
                (free_names, (row.part_slot, row.difference_name.casefold()), "名称"),
                (free_orders, (row.part_slot, row.difference_order), "顺序"),
            ):
                if key in target:
                    raise MetadataError(f"{PART_LABELS[row.part_slot]} 的自由差分{label}重复")
                target[key] = row.object_name
    if len(base_names) > 1:
        raise MetadataError("一个工程只能有一个具名基础组")
    for row in normalized:
        if row.variant_kind not in (VARIANT_BASE, VARIANT_FREE_PART) and row.part_slot not in base_members:
            raise MetadataError(
                f"{row.object_name} 是 {PART_LABELS[row.part_slot]} 差分，但基础组没有该部位"
            )
    return tuple(normalized)


def assignment_from_mapping(object_name, mapping):
    return normalize_assignment(
        object_name,
        mapping.get(PROP_PART_SLOT, ""),
        mapping.get(PROP_VARIANT_KIND, VARIANT_BASE),
        mapping.get(PROP_DIFFERENCE_GROUP, ""),
        mapping.get(PROP_DIFFERENCE_NAME, ""),
        mapping.get(PROP_BASE_GROUP, ""),
        mapping.get(PROP_DIFFERENCE_LOGIC, LOGIC_GROUP),
        mapping.get(PROP_DIFFERENCE_ID, ""),
        mapping.get(PROP_DIFFERENCE_ORDER, 0),
    )


def write_assignment(mapping, assignment):
    """写入 Blender 风格映射并清除持久化目标 Unit ID。"""

    assignment = normalize_assignment(
        assignment.object_name,
        assignment.part_slot,
        assignment.variant_kind,
        assignment.difference_group,
        assignment.difference_name,
        assignment.base_group,
        assignment.difference_logic,
        assignment.difference_id,
        assignment.difference_order,
    )
    for key in tuple(mapping.keys()):
        if key in TARGET_ID_PROPERTIES or key.startswith("Z_SwapID_"):
            del mapping[key]
    mapping[PROP_SCHEMA] = SCHEMA_NAME
    mapping[PROP_PART_SLOT] = assignment.part_slot
    mapping[PROP_VARIANT_KIND] = assignment.variant_kind
    mapping[PROP_DIFFERENCE_LOGIC] = assignment.difference_logic
    if assignment.variant_kind == VARIANT_FREE_PART:
        mapping[PROP_DIFFERENCE_ID] = assignment.difference_id
        mapping[PROP_DIFFERENCE_ORDER] = assignment.difference_order
    else:
        for key in (PROP_DIFFERENCE_ID, PROP_DIFFERENCE_ORDER):
            if key in mapping:
                del mapping[key]
    if assignment.base_group:
        mapping[PROP_BASE_GROUP] = assignment.base_group
    elif PROP_BASE_GROUP in mapping:
        del mapping[PROP_BASE_GROUP]
    if assignment.difference_group:
        mapping[PROP_DIFFERENCE_GROUP] = assignment.difference_group
    elif PROP_DIFFERENCE_GROUP in mapping:
        del mapping[PROP_DIFFERENCE_GROUP]
    if assignment.difference_name:
        mapping[PROP_DIFFERENCE_NAME] = assignment.difference_name
    elif PROP_DIFFERENCE_NAME in mapping:
        del mapping[PROP_DIFFERENCE_NAME]
    return assignment


def clear_assignment(mapping):
    for key in (
        PROP_SCHEMA,
        PROP_PART_SLOT,
        PROP_VARIANT_KIND,
        PROP_BASE_GROUP,
        PROP_DIFFERENCE_GROUP,
        PROP_DIFFERENCE_NAME,
        PROP_DIFFERENCE_LOGIC,
        PROP_DIFFERENCE_ID,
        PROP_DIFFERENCE_ORDER,
    ):
        if key in mapping:
            del mapping[key]
