"""Scene 自由差分列表的持久数据与对象元数据同步（无 bpy 依赖）。"""

from .authoring_metadata import (
    Assignment, LOGIC_FREE, PART_LABELS, PART_SLOTS, PROP_DIFFERENCE_ID,
    PROP_PART_SLOT, VARIANT_FREE_PART, MetadataError, clean_display_name,
    clear_assignment, write_assignment,
    assignment_from_mapping,
)


def find_group(settings, slot=None):
    slot = slot or settings.authoring_part_slot
    return next((group for group in settings.free_difference_groups if group.part_slot == slot), None)


def ensure_groups(settings):
    for slot in PART_SLOTS:
        if find_group(settings, slot) is None:
            group = settings.free_difference_groups.add()
            group.part_slot = slot
            group.display_name = PART_LABELS[slot]
            group.active_index = -1


def active_option(group):
    if group is not None and 0 <= group.active_index < len(group.options):
        return group.options[group.active_index]
    return None


def validate_scene_free_lists(scene, assignments=None, part_slots=None):
    """保存前检查完整列表（含未标记尾项）；可传入当前封包域的记录和槽位。

    Scene.hd2bt_settings.free_difference_groups[]:
      part_slot / display_name / active_index / options[].identity / options[].display_name
    只读接口；未注册 Batch RNA 时返回，不把 SDK 独立使用变成硬依赖。
    """
    settings = getattr(scene, "hd2bt_settings", None)
    if settings is None:
        return
    if assignments is None:
        assignments = [assignment_from_mapping(obj.name, obj) for obj in scene.objects
                       if obj.type == "MESH" and obj.get(PROP_PART_SLOT)]
    rows = {row.difference_id: row for row in assignments if row.variant_kind == VARIANT_FREE_PART}
    allowed = set(PART_SLOTS if part_slots is None else part_slots)
    for group in settings.free_difference_groups:
        if group.part_slot not in allowed:
            continue
        for order, option in enumerate(group.options):
            row = rows.get(option.identity)
            if row is None:
                raise MetadataError(f"{group.display_name} / {option.display_name} 尚未标记网格；请标记或删除空列表项后封包")
            if (row.part_slot != group.part_slot or row.difference_group != group.display_name
                    or row.difference_name != option.display_name or row.difference_order != order):
                raise MetadataError(f"{group.display_name} / {option.display_name} 的列表与网格标记不同步，请重新指定")


def synchronize_group(scene, group):
    """按稳定 ID 更新本 Scene 全部关联网格，不依赖当前选择。"""
    group_name = clean_display_name(group.display_name, "差分部位名称")
    options = {}
    names = set()
    for index, option in enumerate(group.options):
        name = clean_display_name(option.display_name, "差分名称")
        if name.casefold() in names:
            raise MetadataError("同一部位的差分名称不能重复")
        names.add(name.casefold())
        options[option.identity] = (name, index)
    changes = []
    for obj in scene.objects:
        if obj.type != "MESH" or obj.get(PROP_PART_SLOT) != group.part_slot:
            continue
        identity = obj.get(PROP_DIFFERENCE_ID)
        if identity in options:
            name, index = options[identity]
            changes.append((obj, Assignment(obj.name, group.part_slot, VARIANT_FREE_PART,
                                            group_name, name, "", LOGIC_FREE, identity, index)))
    for obj, assignment in changes:
        write_assignment(obj, assignment)
    return len(changes)


def remove_option(scene, group, index):
    identity = group.options[index].identity
    cleared = 0
    for obj in scene.objects:
        if obj.type == "MESH" and obj.get(PROP_DIFFERENCE_ID) == identity:
            clear_assignment(obj)
            cleared += 1
    group.options.remove(index)
    group.active_index = min(index, len(group.options) - 1)
    synchronize_group(scene, group)
    return cleared


def move_option(scene, group, source, destination):
    group.options.move(source, destination)
    group.active_index = destination
    synchronize_group(scene, group)


def update_label(owner, _context):
    """RNA 文本编辑参与 Blender undo，失败时恢复最近一次有效名称。"""
    if owner.get("_updating_label", False):
        return
    scene = owner.id_data
    if not hasattr(scene, "hd2bt_settings"):
        return
    settings = scene.hd2bt_settings
    group = next((group for group in settings.free_difference_groups
                  if group == owner or any(option == owner for option in group.options)), None)
    if group is None:
        return
    # 新列表项先设置 ID 再赋名；未完成初始化的其他项无需同步。
    if any(not option.identity for option in group.options):
        return
    try:
        clean_display_name(owner.display_name, "名称")
        synchronize_group(scene, group)
        owner["_last_valid_name"] = owner.display_name
        settings.free_difference_error = ""
    except MetadataError as exc:
        owner["_updating_label"] = True
        try:
            owner.display_name = owner.get("_last_valid_name", "未命名")
        finally:
            owner["_updating_label"] = False
        settings.free_difference_error = str(exc)


def update_logic(settings, _context):
    if settings.authoring_difference_logic == LOGIC_FREE:
        ensure_groups(settings)
