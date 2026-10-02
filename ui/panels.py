"""侧边栏面板与列表；注册事务由插件入口统一管理。"""

from bpy.types import Panel, UIList
from ..version import VERSION_TEXT


class HD2BT_UL_dictionary_rows(UIList):
    """两列表字典编辑器；左侧始终是来源名称，右侧始终是潜兵目标骨。"""

    bl_idname = "HD2BT_UL_dictionary_rows"

    def draw_item(
        self,
        _context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_property,
        _index=0,
        _flt_flag=0,
    ):
        row = layout.row()
        row.scale_y = 1.1
        row.prop(item, "source_name", text="", emboss=True)
        row.scale_x = 0.1
        row.label(text="→")
        row.scale_x = 1.0
        row.prop(item, "target_name", text="", emboss=True)


class HD2BT_UL_free_differences(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, index):
        row = layout.row(align=True)
        row.label(text="", icon="CHECKMARK" if index == 0 else "BLANK1")
        row.prop(item, "display_name", text="", emboss=False)


class HD2BT_PT_main(Panel):
    bl_label = f"HD2 Batch Tool {VERSION_TEXT}"
    bl_idname = "HD2BT_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "HD2 Batch"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.hd2bt_settings

        has_physics = any(obj.type == 'ARMATURE'
            and getattr(obj.data, 'hd2pb_project', None)
            and obj.data.hd2pb_project.schema_version
            for obj in context.scene.objects)

        box = layout.box()
        box.label(text="第三人称自定义骨架", icon="ARMATURE_DATA")
        box.scale_y = 1.2
        box.prop(settings, "publish_rig_gender")
        box.operator("hd2bt.import_reference", icon="IMPORT")
        row = box.row(align=True)
        op = row.operator("hd2bt.convert_pose", text="转为烘焙 T-pose")
        op.direction = "TO_TPOSE"
        op = row.operator("hd2bt.convert_pose", text="转回初态 + 原始骨轴")
        op.direction = "TO_INITIAL"

        box = layout.box()
        box.label(text="骨骼吸附与重命名", icon="MOD_ARMATURE")
        box.scale_y = 1.2
        box.prop(settings, "mmd_source_armature")
        row = box.row(align=True)
        row.prop(settings, "aq_bone_preset", text="顶点组字典")
        row.scale_x = 1.1
        row.operator("hd2bt.open_aq_preset_folder", text="", icon="FILE_FOLDER")
        row.operator("hd2bt.dictionary_new", text="", icon="ADD")
        row.prop(
            settings,
            "dictionary_editor_expanded",
            text="",
            icon="TRIA_DOWN" if settings.dictionary_editor_expanded else "TRIA_RIGHT",
            toggle=True,
        )
        if settings.dictionary_editor_expanded:
            editor = box.box()
            row = editor.row(align=True)
            row.operator("hd2bt.dictionary_new", text="新建空白", icon="FILE_NEW")
            row.operator(
                "hd2bt.dictionary_load_current",
                text="复制/编辑当前",
                icon="DUPLICATE",
            )
            editor.prop(settings, "dictionary_editor_name")
            editor.prop(settings, "dictionary_editor_section", expand=True)
            if settings.dictionary_editor_section == "SNAP":
                collection_name = "dictionary_snap_rows"
                index_name = "dictionary_snap_index"
                row_count = len(settings.dictionary_snap_rows)
            else:
                collection_name = "dictionary_rename_rows"
                index_name = "dictionary_rename_index"
                row_count = len(settings.dictionary_rename_rows)
            row = editor.row()
            row.template_list(
                "HD2BT_UL_dictionary_rows",
                settings.dictionary_editor_section,
                settings,
                collection_name,
                settings,
                index_name,
                rows=7,
            )
            controls = row.column(align=True)
            controls.operator("hd2bt.dictionary_add_row", text="", icon="ADD")
            controls.operator("hd2bt.dictionary_remove_row", text="", icon="REMOVE")
            controls.separator()
            op = controls.operator("hd2bt.dictionary_move_row", text="", icon="TRIA_UP")
            op.direction = "UP"
            op = controls.operator("hd2bt.dictionary_move_row", text="", icon="TRIA_DOWN")
            op.direction = "DOWN"
            editor.label(text=f"当前页 {row_count} 条；左为来源名称，右为潜兵目标骨")
            save_row = editor.row()
            save_row.scale_y = 1.2
            save_row.operator("hd2bt.dictionary_save", icon="FILE_TICK")
        box.operator("hd2bt.validate_mapping", text="检查字典与骨架", icon="CHECKMARK")
        box.prop(settings, "auto_scale_character")
        scale_options = box.column(align=True)
        scale_options.enabled = settings.auto_scale_character
        scale_options.prop(settings, "head_alignment_offset")
        box.prop(settings, "unmapped_weight_mode", text="未映射权重")
        row = box.row(align=True)
        row.operator("hd2bt.snap_bones", text="骨骼吸附")
        row.operator("hd2bt.rename_vertex_groups", text="顶点组重命名+重绑")
        box.prop(settings, "grip_adjust_rotation")
        box.scale_y = 1.3
        box.operator("hd2bt.fix_grip_bones", icon="CONSTRAINT_BONE")

        box = layout.box()
        box.label(text="额外工具", icon="TOOL_SETTINGS")
        box.scale_y = 1.3
        box.operator("hd2bt.check_irregular_vertices", icon="VIEWZOOM")
        row = box.row(align=True)
        row.prop(settings, "cut_normal_warning")
        row.prop(settings, "cut_normal_limit")
        box.label(text="超过警告值保留；超过阻断值取消", icon="INFO")
        toggle = box.operator(
            "hd2bt.configure_cut_marking", text="自动标记部位",
            icon="CHECKBOX_HLT" if settings.cut_auto_mark_parts else "CHECKBOX_DEHLT",
            depress=settings.cut_auto_mark_parts,
        )
        toggle.enable = not settings.cut_auto_mark_parts
        if settings.cut_auto_mark_parts:
            logic_label = "自由搭配差分" if settings.cut_difference_logic == "FREE" else "组差分"
            box.label(text=f"切割标记：{logic_label} / 基础部位")
            box.prop(settings, "base_group_name", text="基础组")
        else:
            box.label(text="只切割，不写入部位与差分标记")
        run_row = box.row()
        run_row.scale_y = 1.3
        run_row.operator(
            "hd2bt.prepare_armor_body",
            text="自动切割",
            icon="MOD_EXPLODE",
        )
        box.operator('hd2bt.prepare_armor_physics', text='按物理链切分', icon='PHYSICS')
        if has_physics:
            box.label(text='物理链切分可保留整链几何', icon='INFO')
            box.label(text='身体跨部位需新版共享物理配套')
            box.label(text='头盔 / 身体不能跨域共用链')
        if settings.last_cut_summary:
            box.label(text=settings.last_cut_summary, icon="ERROR" if settings.last_cut_warning else "CHECKMARK")

        box = layout.box()
        box.label(text="部位与差分指定", icon="GROUP_VERTEX")
        if has_physics:
            box.label(text='身体部位可消费同一条完整链', icon='INFO')
            box.label(text='改部位后重新保存模型与配套')
            box.label(text='由 SDK 检查链依赖和保存域')
        box.label(text="只写语义；AQSDK 封包时才临时匹配 Unit ID", icon="INFO")
        box.prop(settings, "authoring_difference_logic", text="差分逻辑")
        if settings.authoring_difference_logic == "FREE":
            from ..core.free_variants import find_group
            box.prop(settings, "free_variant_kind", text="类型")
            box.prop(settings, "authoring_part_slot", text="部位")
            if settings.free_variant_kind == "BASE":
                box.prop(settings, "base_group_name", text="基础组")
                box.label(text="基础部位常驻；各差分部位默认启用首项", icon="INFO")
            else:
                group = find_group(settings)
                if group is not None:
                    box.prop(group, "display_name", text="差分部位名称")
                    list_row = box.row()
                    list_row.template_list("HD2BT_UL_free_differences", group.part_slot,
                                           group, "options", group, "active_index", rows=4)
                    buttons = list_row.column(align=True)
                    for action, icon in (("ADD", "ADD"), ("REMOVE", "REMOVE"),
                                         ("UP", "TRIA_UP"), ("DOWN", "TRIA_DOWN")):
                        buttons.operator("hd2bt.free_difference_list", text="", icon=icon).action = action
                else:
                    box.operator("hd2bt.free_difference_list", text="添加差分", icon="ADD").action = "ADD"
                box.label(text="同部位互斥；首项默认显示，可直接重命名", icon="INFO")
                box.label(text="选中活动项后标记；每项仅一个网格")
                box.label(text="封包前全部列表项都须标记网格")
                box.label(text="删除项会清除关联对象标记（可撤销）")
            if settings.free_difference_error:
                box.label(text=settings.free_difference_error, icon="ERROR")
        else:
            box.prop(settings, "authoring_variant_kind", text="类型")
        if settings.authoring_difference_logic == "FREE":
            pass
        elif settings.authoring_variant_kind == "BASE":
            box.prop(settings, "base_group_name", text="基础组")
            box.prop(settings, "authoring_part_slot", text="部位")
            box.label(text="基础组是默认显示；每个部位只能有一个基础对象", icon="INFO")
        elif settings.authoring_variant_kind == "HELMET_SINGLE":
            box.label(text="指定部位：头盔（Head）")
            box.prop(settings, "helmet_difference_name", text="差分名称")
        else:
            box.prop(settings, "authoring_part_slot", text="部位")
            if settings.authoring_variant_kind == "BODY_GROUP":
                box.prop(settings, "body_difference_group_name", text="组名")
                box.label(text="同组每个部位最多一个差分；单部位组合法", icon="INFO")
        row = box.row(align=True)
        row.operator("hd2bt.assign_part_metadata", icon="CHECKMARK")
        row.operator("hd2bt.clear_part_metadata", icon="X")
        box.operator("hd2bt.validate_authoring_metadata", icon="VIEWZOOM")
        active = context.active_object
        if active is not None and active.type == "MESH":
            slot = active.get("HD2BT_PartSlot")
            if slot:
                summary = f"当前：{slot} / {active.get('HD2BT_VariantKind', 'BASE')}"
                group = active.get("HD2BT_DifferenceGroup")
                name = active.get("HD2BT_DifferenceName")
                base = active.get("HD2BT_BaseGroup")
                if base:
                    summary += f" / 基础组：{base}"
                if group:
                    summary += f" / 组：{group}"
                if name:
                    summary += f" / 差分：{name}"
                box.label(text=summary, icon="OBJECT_DATA")
CLASSES = (HD2BT_UL_dictionary_rows, HD2BT_UL_free_differences, HD2BT_PT_main)
