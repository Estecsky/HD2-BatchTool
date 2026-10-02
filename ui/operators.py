"""HD2BatchTool 的 Blender 操作符。"""

import os
import uuid
import bpy
import bmesh
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator

from ..presets.aq_presets import load_aq_preset, user_preset_dir
from ..workflows.armor_workflow import (
    auto_split_independent_character,
)
from ..core.authoring_metadata import (
    Assignment,
    MetadataError,
    PROP_PART_SLOT,
    VARIANT_BODY_GROUP,
    VARIANT_HELMET_SINGLE,
    VARIANT_FREE_PART,
    LOGIC_FREE,
    assignment_from_mapping,
    clear_assignment,
    validate_assignments,
    write_assignment,
)
from .blender_lifecycle import register_classes, unregister_classes
from ..core.weight_validation import mesh_weight_issues
from ..core.free_variants import active_option, ensure_groups, find_group, move_option, remove_option, validate_scene_free_lists
from ..workflows.mmd_mapping import (
    find_bound_meshes,
    inherit_game_export_metadata,
    rename_vertex_groups,
    resolve_mapping_bones,
    selected_mmd_and_hd2,
    snap_independent_to_hd2,
)
from ..utils.reference import import_reference_group
from ..utils.rigging import (
    convert_pose_space,
    solve_grip_bones,
)
from ..utils.utils import (
    HD2BTError,
    detect_rig_kind,
    is_hd2_armature,
)


def _active_mapping(settings):
    return load_aq_preset(settings.aq_bone_preset)


def _find_hd2_target(context):
    """优先按潜兵自定义属性识别，不要求用户固定选择顺序。"""

    candidates = []
    for obj in context.selected_objects:
        if is_hd2_armature(obj) and obj not in candidates:
            candidates.append(obj)
        if obj.type == "MESH":
            for modifier in obj.modifiers:
                if modifier.type == "ARMATURE" and is_hd2_armature(modifier.object):
                    if modifier.object not in candidates:
                        candidates.append(modifier.object)
    if context.active_object is not None and context.active_object.type == "MESH":
        for modifier in context.active_object.modifiers:
            if modifier.type == "ARMATURE" and is_hd2_armature(modifier.object):
                if modifier.object not in candidates:
                    candidates.append(modifier.object)
    if is_hd2_armature(context.active_object) and context.active_object not in candidates:
        candidates.append(context.active_object)
    if len(candidates) != 1:
        raise HD2BTError("请选择人物网格及唯一一套潜兵骨架；插件会按 BonesID/HD2BT 属性识别")
    return candidates[0]


def _report_exception(operator, exc):
    message = str(exc) if isinstance(exc, HD2BTError) else f"{type(exc).__name__}: {exc}"
    operator.report({"ERROR"}, message)
    return {"CANCELLED"}


class HD2BT_OT_check_irregular_vertices(Operator):
    bl_idname = "hd2bt.check_irregular_vertices"
    bl_label = "检查不规范顶点"
    bl_description = (
        "按当前 AQSDK 独立保存流程，在副本预处理权重后，仅选择确定阻止保存的顶点\n"
        "不报告 SDK 可自动处理的超过四项、未归一化或微小权重；不修改源权重。"
        "需要目标 Unit 才能判定的骨组/流问题另行提示，不误选顶点"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.mode in {'OBJECT', 'EDIT_MESH'} and any(
            obj.type == 'MESH' for obj in context.selected_objects
        )

    def execute(self, context):
        try:
            meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
            # 在切换模式或修改选择前完整检查权限及共享数据的骨组语义。
            # 不猜无骨架网格的骨名，也不更改其绑定、隐藏或可选择状态。
            entries = []
            shared = {}
            sdk_settings = getattr(context.scene, 'Hd2ToolPanelSettings', None)
            if sdk_settings is not None and getattr(sdk_settings, 'LegacyWeightNames', False):
                raise HD2BTError("旧式权重名称依赖目标 Unit 的局部骨表，不能仅凭源网格定位，请使用 SDK 保存校验")
            for obj in meshes:
                if (obj.library or obj.override_library or obj.data.library
                        or obj.data.override_library):
                    raise HD2BTError(f"网格“{obj.name}”是链接/覆盖数据，请先转为本地可编辑数据")
                if obj.hide_select or not obj.visible_get(view_layer=context.view_layer):
                    raise HD2BTError(f"网格“{obj.name}”不可见或被锁定选择，请先解除限制")
                if obj.data.shape_keys is not None:
                    raise HD2BTError(f"网格“{obj.name}”有形态键，SDK 会在网格级拒绝保存；这不是顶点权重错误")
                if any(modifier.type != 'ARMATURE' for modifier in obj.modifiers):
                    raise HD2BTError(f"网格“{obj.name}”有额外修改器，无法可靠映射导出顶点；请先处理修改器再检查")
                armatures = [modifier.object for modifier in obj.modifiers
                             if modifier.type == 'ARMATURE' and modifier.object is not None]
                if not armatures and obj.parent is not None and obj.parent.type == 'ARMATURE':
                    armatures = [obj.parent]
                if not armatures:
                    raise HD2BTError(f"网格“{obj.name}”未绑定骨架，无法可靠区分骨骼组与辅助组")
                if len({armature.as_pointer() for armature in armatures}) != 1:
                    raise HD2BTError(f"网格“{obj.name}”绑定多个骨架，请先明确导出骨架")
                bone_names = frozenset(bone.name for bone in armatures[0].data.bones)
                signature = (tuple((group.name, group.lock_weight) for group in obj.vertex_groups), bone_names)
                key = obj.data.as_pointer()
                if key in shared and shared[key] != signature:
                    raise HD2BTError(f"网格“{obj.name}”共享 Mesh 但骨组映射不同，请先将网格数据独立化")
                shared[key] = signature
                entries.append((obj, bone_names))
            for obj, _ in entries:
                if obj.mode == 'EDIT':
                    obj.update_from_editmode()
            issues = []
            unverified = []
            for obj, names in entries:
                notices = []
                issues.append((obj, mesh_weight_issues(obj, names, context=context, warnings=notices)))
                unverified.extend(f"{obj.name}：{notice}" for notice in notices)
            if context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for obj in context.selected_objects:
                obj.select_set(False)
            for obj in meshes:
                obj.select_set(True)
            context.view_layer.objects.active = meshes[0]
            bpy.ops.object.mode_set(mode='EDIT')
            context.tool_settings.mesh_select_mode = (True, False, False)
            processed = set()
            total = 0
            summaries = []
            for obj, problems in issues:
                summaries.append(f"{obj.name}：{len(problems)}")
                key = obj.data.as_pointer()
                if key in processed:
                    continue
                processed.add(key)
                total += len(problems)
                bm = bmesh.from_edit_mesh(obj.data)
                bm.verts.ensure_lookup_table()
                for face in bm.faces:
                    face.select_set(False)
                for edge in bm.edges:
                    edge.select_set(False)
                for vertex in bm.verts:
                    vertex.select_set(False)
                bm.select_history.clear()
                for index in problems:
                    vertex = bm.verts[index]
                    # 隐藏的问题点需要可见才能让用户定位；不移动或改写几何/权重。
                    vertex.hide_set(False)
                    vertex.select_set(True)
                bm.select_flush_mode()
                bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            for notice in unverified:
                self.report({'WARNING'}, notice)
            scope = "；存在需目标 Unit 验证的项目，不能据此保证整体保存成功" if unverified else ""
            self.report({'INFO'}, f"已确认的 SDK 权重阻断顶点 {total} 个（共享 Mesh 去重）；" + "；".join(summaries) + scope)
            return {'FINISHED'}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_import_reference(Operator):
    bl_idname = "hd2bt.import_reference"
    bl_label = "导入第三人称工作骨架"
    bl_description = "导入原版第三人称骨架与模板网格；它只作为当前人物的制作骨架"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            root, _objects, _collection = import_reference_group("TP_INITIAL")
            root["HD2BT_IndependentRig"] = True
            self.report({"INFO"}, f"已导入工作骨架：{root.name}")
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_convert_pose(Operator):
    bl_idname = "hd2bt.convert_pose"
    bl_label = "烘焙身体静止姿态"
    bl_description = "用逐骨旋转重建编辑模式静止骨位，并同步烘焙所有绑定网格"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(
        items=(
            (
                "TO_TPOSE",
                "转为烘焙 T-pose",
                "把身体骨架和绑定网格真正烘焙为便于编辑模式吸附的 T-pose 与对称骨轴",
            ),
            (
                "TO_INITIAL",
                "转回初始 Pose + 原始骨轴（破坏性）",
                "先把自定义 T-pose 逐骨折回初态并烘焙网格，再恢复原版骨轴；绑定模型须先应用或移除形态键；只能用 Undo 撤回",
            ),
        ),
        default="TO_TPOSE",
    )

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "ARMATURE"

    def invoke(self, context, _event):
        if self.direction == "TO_INITIAL":
            return context.window_manager.invoke_props_dialog(self, width=520)
        return self.execute(context)

    def draw(self, _context):
        layout = self.layout
        layout.label(text="这会重写静止骨架和所有绑定网格顶点。", icon="ERROR")
        layout.label(text="转回初始 Pose + 原始骨轴是破坏性操作，只能通过 Undo 撤回。")
        layout.label(text="请先保存工程副本，并确认当前活动对象是需要处理的自定义骨架。")
        layout.label(text="绑定模型若仍有形态键（包括 Basis），将报错并取消操作。")

    def execute(self, context):
        try:
            obj = context.active_object
            kind = detect_rig_kind(obj, context.scene.hd2bt_settings.rig_kind_override)
            count, error = convert_pose_space(obj, self.direction, kind=kind)
            label = "烘焙 T-pose" if self.direction == "TO_TPOSE" else "初始 Pose + 原始骨轴"
            self.report(
                {"INFO"},
                f"已转换为 {label}：处理 {count} 根骨骼，最大 head 误差 {error:.8f}",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_snap_bones(Operator):
    bl_idname = "hd2bt.snap_bones"
    bl_label = "骨骼吸附"
    bl_description = "按当前映射字典把潜兵工作骨架逐骨吸附到任意来源骨架"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return sum(obj.type == "ARMATURE" for obj in context.selected_objects) >= 2

    def execute(self, context):
        try:
            settings = context.scene.hd2bt_settings
            mapping = _active_mapping(settings)
            source, target = selected_mmd_and_hd2(
                context,
                settings.mmd_source_armature,
                settings.rig_kind_override,
            )
            changed, head_delta, equipment = snap_independent_to_hd2(
                source,
                target,
                head_offset=settings.head_alignment_offset,
                auto_scale=settings.auto_scale_character,
                mapping_profile=mapping,
            )
            self.report(
                {"INFO"},
                f"已按人物自身比例吸附 {changed} 根骨；映射={mapping['name']}；" +
                (f"人物统一缩放 {float(target.get('HD2BT_SourceScale', 1.0)):.4f} 倍；"
                 if settings.auto_scale_character else "人物统一缩放已关闭，来源位置与大小保持不变；") +
                f"接地={target.get('HD2BT_SourceGroundMethod', 'bone')}；"
                f"头部修正平移 {head_delta.length:.4f}m；背包={equipment['method']}",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_rename_vertex_groups(Operator):
    bl_idname = "hd2bt.rename_vertex_groups"
    bl_label = "顶点组重命名+重绑"
    bl_description = "需同时选择来源骨架和潜兵工作骨架（双选骨架）；\n按字典重命名顶点组，执行辅助骨/物理整合与权重整理，并将网格父级及骨架修改器重绑到潜兵工作骨架"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return sum(obj.type == "ARMATURE" for obj in context.selected_objects) >= 2

    def execute(self, context):
        try:
            settings = context.scene.hd2bt_settings
            mapping = _active_mapping(settings)
            source, target = selected_mmd_and_hd2(
                context,
                settings.mmd_source_armature,
                settings.rig_kind_override,
            )
            meshes = find_bound_meshes(source, context)
            stats = rename_vertex_groups(
                source,
                target,
                meshes,
                mapping_profile=mapping,
                unmapped_mode=settings.unmapped_weight_mode,
            )
            metadata_count, template_name = inherit_game_export_metadata(target, meshes)
            transfer_report = stats.get('physics_transfer')
            if transfer_report:
                removed = transfer_report['pruning']['counts']
                self.report({'INFO'}, f"物理转移保留 {transfer_report['chains']} 条链；"
                            f"清理 {removed['chains']} 条失效/无用链、{removed['colliders']} 个碰撞体、"
                            f"{removed['chain_links']} 个链引用；源物理未改写")
            metadata_text = (
                f"；已从 {template_name} 继承游戏导出元数据"
                if metadata_count else ""
            )
            self.report(
                {"INFO"},
                f"处理 {stats['meshes']} 个网格：重命名 {stats['renamed']}，"
                f"合并 {stats['merged']}，保留未映射组 {stats['untouched']}，"
                f"省略面部微表情骨 {len(stats.get('facial_controls_merged', []))}，"
                f"父链合并 {stats['custom_groups_collapsed']}，"
                f"清理空顶点组 {stats['utility_groups_removed']}，"
                f"按潜兵过渡修正手臂 {stats['arm_vertices_rebalanced']} 点，"
                f"重绑 {stats['rebound']}{metadata_text}",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_validate_mapping(Operator):
    bl_idname = "hd2bt.validate_mapping"
    bl_label = "检查当前映射"
    bl_description = "检查来源骨架能否解析当前字典中的目标骨"

    @classmethod
    def poll(cls, context):
        return sum(obj.type == "ARMATURE" for obj in context.selected_objects) >= 2

    def execute(self, context):
        try:
            settings = context.scene.hd2bt_settings
            profile = _active_mapping(settings)
            source, _target = selected_mmd_and_hd2(
                context, settings.mmd_source_armature, settings.rig_kind_override
            )
            resolved = resolve_mapping_bones(source, profile)
            required = {
                "hips", "spine1", "neck", "head", "l_clavicle", "r_clavicle",
                "l_shoulder", "r_shoulder", "l_elbow", "r_elbow", "l_hand", "r_hand",
                "l_thigh", "r_thigh", "l_knee", "r_knee", "l_foot", "r_foot",
            }
            missing = sorted((required - {"spine1"}) - set(resolved))
            if not ({"spine1", "spine2"} & set(resolved)):
                missing.append("spine1/spine2")
            if missing:
                raise HD2BTError("当前字典缺少必要映射：" + "、".join(missing))
            settings.last_mapping_summary = (
                f"{profile['name']}：识别 {len(resolved)}/{len(profile['snap_map'])} 个吸附目标"
            )
            self.report({"INFO"}, settings.last_mapping_summary)
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_open_aq_preset_folder(Operator):
    bl_idname = "hd2bt.open_aq_preset_folder"
    bl_label = "打开顶点组字典目录"
    bl_description = "打开可写的用户字典目录；放入 AQ 两列表 .py 字典后下拉框会自动刷新"

    def execute(self, _context):
        path = user_preset_dir(create=True)
        os.startfile(str(path))
        return {"FINISHED"}




class HD2BT_OT_configure_cut_marking(Operator):
    bl_idname = "hd2bt.configure_cut_marking"
    bl_label = "选择自动标记的差分逻辑"
    bl_description = "开关自动部位标记；开启时选择差分逻辑，取消对话框则保持关闭"
    bl_options = {"UNDO"}

    enable: BoolProperty(default=True, options={"SKIP_SAVE"})
    difference_logic: EnumProperty(
        name="差分逻辑",
        items=(("FREE", "自由搭配差分", "基础部位常驻，各部位差分独立选择"),
               ("GROUP", "组差分", "具名身体差分组与头盔单差分")),
        default="FREE", options={"SKIP_SAVE"},
    )

    def invoke(self, context, _event):
        if not self.enable:
            return self.execute(context)
        self.difference_logic = "FREE"
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        self.layout.prop(self, "difference_logic", expand=True)
        self.layout.label(text="七个切割结果将标记为基础部位")
        self.layout.label(text=f"基础组：{context.scene.hd2bt_settings.base_group_name}")

    def execute(self, context):
        settings = context.scene.hd2bt_settings
        if self.enable:
            settings.cut_difference_logic = self.difference_logic
            settings.authoring_difference_logic = self.difference_logic
        settings.cut_auto_mark_parts = self.enable
        return {"FINISHED"}


class _PrepareArmorMixin:
    """共享回调不注册为 RNA 类型，两个 Operator 各自注册。"""
    physics_aware = False

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "hd2bt_settings", None)
        return settings is not None

    def execute(self, context):
        try:
            target = _find_hd2_target(context)
            project = getattr(target.data, 'hd2pb_project', None)
            if not self.physics_aware and project is not None and project.schema_version:
                self.report({'WARNING'}, '检测到物理项目：头盔和身体不可共用一条链，切后请执行 SDK 保存预检')
            result = auto_split_independent_character(context, target, physics_aware=self.physics_aware)
            settings = context.scene.hd2bt_settings
            settings.last_cut_warning = result["normal_warning"]
            settings.last_cut_summary = (
                f"最大误差 {result['normal_max_error']:.6f}；"
                + ("超过警告值，结果已保留，请检查切口" if result["normal_warning"] else "法向检查通过")
            )
            if result.get('physics_report'):
                report = result['physics_report']
                settings.last_cut_summary += f"；{len(report['chain_owners'])} 条链整链归属；手动调整身体部位后须重新保存全部相关部位与配套"
            self.report(
                {"WARNING"} if result["normal_warning"] else {"INFO"},
                f"已在场景中切出 {len(result['parts'])} 个部位；"
                + ("已自动标记基础部位；" if result['marked_parts'] else "未标记部位，请按需手动指定；") +
                f"清理 {result['empty_groups_removed']} 个空顶点组；"
                f"{settings.last_cut_summary}；未存储、未封包",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_prepare_armor_body(_PrepareArmorMixin, Operator):
    bl_idname = "hd2bt.prepare_armor_body"
    bl_label = "自动切割七个身体部件"
    bl_description = "按权重、骨骼位置和连通块自动切割；是否指定部位由自动标记开关控制"
    bl_options = {"REGISTER", "UNDO"}


class HD2BT_OT_prepare_armor_physics(_PrepareArmorMixin, Operator):
    bl_idname = 'hd2bt.prepare_armor_physics'
    bl_label = '按物理链切分'
    bl_description = '按物理链正权重及链组连接整体分配部位；头盔与身体仍分别保存'
    bl_options = {"REGISTER", "UNDO"}
    physics_aware = True


def _scene_assignment_rows(scene=None):
    rows = []
    for obj in (scene or bpy.context.scene).objects:
        if obj.type != "MESH" or not obj.get(PROP_PART_SLOT):
            continue
        rows.append(assignment_from_mapping(obj.name, obj))
    return rows


class HD2BT_OT_assign_part_metadata(Operator):
    bl_idname = "hd2bt.assign_part_metadata"
    bl_label = "指定所选网格"
    bl_description = (
        "为所选网格写入语义部位与差分信息，并删除旧 Z_ObjectID/交换 ID；"
        "真实 Unit ID 留给 AQSDK 保存时临时解析"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = context.scene.hd2bt_settings
        if settings.authoring_difference_logic == LOGIC_FREE and settings.free_variant_kind == VARIANT_FREE_PART:
            if active_option(find_group(settings)) is None:
                cls.poll_message_set("请先添加并选择当前部位的活动差分项")
                return False
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        try:
            settings = context.scene.hd2bt_settings
            selected = [obj for obj in context.selected_objects if obj.type == "MESH"]
            slot = settings.authoring_part_slot
            kind = settings.authoring_variant_kind
            logic = settings.authoring_difference_logic
            group = None
            option = None
            if logic == LOGIC_FREE:
                kind = settings.free_variant_kind
                group = find_group(settings)
                option = active_option(group)
                if kind == VARIANT_FREE_PART and option is None:
                    raise MetadataError("请先添加并选择当前部位的活动差分项")
            if logic != LOGIC_FREE and kind == VARIANT_HELMET_SINGLE:
                slot = "Head"
            assignment_rows = [
                Assignment(
                    obj.name,
                    slot,
                    kind,
                    group.display_name if kind == VARIANT_FREE_PART else settings.body_difference_group_name
                    if kind in (VARIANT_BODY_GROUP, VARIANT_FREE_PART)
                    else "",
                    option.display_name if kind == VARIANT_FREE_PART else settings.helmet_difference_name
                    if kind == VARIANT_HELMET_SINGLE
                    else "",
                    settings.base_group_name if kind == "BASE" else "",
                    logic,
                    option.identity if kind == VARIANT_FREE_PART else "",
                    group.active_index if kind == VARIANT_FREE_PART else 0,
                )
                for obj in selected
            ]

            selected_names = {obj.name for obj in selected}
            proposed = [
                row for row in _scene_assignment_rows(context.scene) if row.object_name not in selected_names
            ]
            proposed.extend(assignment_rows)
            validate_assignments(proposed)

            for obj, row in zip(selected, assignment_rows):
                write_assignment(obj, row)
            self.report(
                {"INFO"},
                f"已指定 {len(selected)} 个网格；真实 Unit ID 将由 AQSDK 保存事务解析",
            )
            return {"FINISHED"}
        except (MetadataError, HD2BTError) as exc:
            return _report_exception(self, exc)
        except Exception as exc:
            return _report_exception(self, exc)


class HD2BT_OT_free_difference_list(Operator):
    bl_idname = "hd2bt.free_difference_list"
    bl_label = "编辑自由差分列表"
    bl_description = "添加、删除或调整差分顺序；删除会清除本 Scene 关联对象的部位标记，可撤销"
    bl_options = {"REGISTER", "UNDO"}
    action: EnumProperty(items=(("ADD", "添加", ""), ("REMOVE", "删除并清除关联标记", ""),
                                ("UP", "上移", ""), ("DOWN", "下移", "")))

    def execute(self, context):
        settings = context.scene.hd2bt_settings
        ensure_groups(settings)
        group = find_group(settings)
        if self.action == "ADD":
            existing = {option.display_name.casefold() for option in group.options}
            number = 1
            while f"差分 {number}".casefold() in existing:
                number += 1
            option = group.options.add()
            option.identity = str(uuid.uuid4())
            option.display_name = f"差分 {number}"
            group.active_index = len(group.options) - 1
        elif active_option(group) is None:
            self.report({"WARNING"}, "请先选中一个差分项")
            return {"CANCELLED"}
        elif self.action == "REMOVE":
            cleared = remove_option(context.scene, group, group.active_index)
            self.report({"INFO"}, f"已删除差分项并清除 {cleared} 个关联网格的部位标记（可撤销）")
        else:
            source = group.active_index
            destination = source + (-1 if self.action == "UP" else 1)
            if not 0 <= destination < len(group.options):
                return {"CANCELLED"}
            move_option(context.scene, group, source, destination)
        return {"FINISHED"}


class HD2BT_OT_clear_part_metadata(Operator):
    bl_idname = "hd2bt.clear_part_metadata"
    bl_label = "清除所选指定"
    bl_description = "清除所选网格的语义部位与差分信息；不会恢复旧 Z_ObjectID"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(
            obj.type == "MESH" and obj.get(PROP_PART_SLOT)
            for obj in context.selected_objects
        )

    def execute(self, context):
        selected = [
            obj
            for obj in context.selected_objects
            if obj.type == "MESH" and obj.get(PROP_PART_SLOT)
        ]
        for obj in selected:
            clear_assignment(obj)
        self.report({"INFO"}, f"已清除 {len(selected)} 个网格的部位/差分指定")
        return {"FINISHED"}


class HD2BT_OT_validate_authoring_metadata(Operator):
    bl_idname = "hd2bt.validate_authoring_metadata"
    bl_label = "检查全部部位与差分"
    bl_description = "检查身体差分组唯一性、头盔命名和部位类型是否自洽"

    def execute(self, context):
        try:
            rows = validate_assignments(_scene_assignment_rows(context.scene))
            if context.scene.hd2bt_settings.authoring_difference_logic == LOGIC_FREE or any(
                    row.variant_kind == VARIANT_FREE_PART for row in rows):
                validate_scene_free_lists(context.scene, rows)
            group_names = {
                row.difference_group
                for row in rows
                if row.variant_kind == VARIANT_BODY_GROUP
            }
            base_names = {row.base_group for row in rows if row.variant_kind == "BASE"}
            helmets = sum(row.variant_kind == VARIANT_HELMET_SINGLE for row in rows)
            free_groups = {row.part_slot for row in rows if row.variant_kind == VARIANT_FREE_PART}
            self.report(
                {"INFO"},
                f"检查通过：基础组 {next(iter(base_names), '未设置')}、{len(rows)} 个对象、"
                f"{len(group_names)} 个身体差分组、"
                f"{helmets} 个头盔差分、{len(free_groups)} 个自由差分部位",
            )
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)

class HD2BT_OT_fix_grip_bones(Operator):
    bl_idname = "hd2bt.fix_grip_bones"
    bl_label = "计算并调整持握骨"
    bl_description = "根据原版手掌定位关系调整 attach_hand_r/l 与 weapon_aim；仅第三人称\n此操作为自动计算的较优位置，你仍然可自行调整位置"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == "ARMATURE"

    def execute(self, context):
        try:
            distances = solve_grip_bones(
                context.active_object,
                adjust_rotation=context.scene.hd2bt_settings.grip_adjust_rotation,
            )
            summary = "，".join(f"{name}={value:.4f}" for name, value in distances.items())
            self.report({"INFO"}, f"持握骨修正完成（位移）：{summary}")
            return {"FINISHED"}
        except Exception as exc:
            return _report_exception(self, exc)


CLASSES = (
    HD2BT_OT_check_irregular_vertices,
    HD2BT_OT_import_reference,
    HD2BT_OT_convert_pose,
    HD2BT_OT_validate_mapping,
    HD2BT_OT_open_aq_preset_folder,
    HD2BT_OT_snap_bones,
    HD2BT_OT_rename_vertex_groups,
    HD2BT_OT_fix_grip_bones,
    HD2BT_OT_configure_cut_marking,
    HD2BT_OT_prepare_armor_body,
    HD2BT_OT_prepare_armor_physics,
    HD2BT_OT_assign_part_metadata,
    HD2BT_OT_free_difference_list,
    HD2BT_OT_clear_part_metadata,
    HD2BT_OT_validate_authoring_metadata,
)
_REGISTERED_CLASSES = []


def register():
    register_classes(CLASSES, _REGISTERED_CLASSES)


def unregister():
    unregister_classes(_REGISTERED_CLASSES)
