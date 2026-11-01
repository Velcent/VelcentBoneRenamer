bl_info = {
    "name": "Velcent Bone Renamer",
    "author": "Velcent Games",
    "version": (3, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Bone Renamer",
    "description": "Rename bones and vertex groups with mirror",
    "category": "Rigging",
}

import bpy
import re
import mathutils
import json
import os
from datetime import datetime
from bpy.props import (StringProperty, BoolProperty, IntProperty,
                       CollectionProperty, EnumProperty, FloatProperty)
from bpy.types import PropertyGroup, UIList, Operator, Panel

# Toleransi pencocokan posisi
TOLERANCE = 0.001

# Path untuk menyimpan file template
ADDON_DIR = os.path.dirname(os.path.realpath(__file__))
TEMPLATES_DIR = os.path.join(ADDON_DIR, "bone_renamer_templates")
TEMPLATES_FILE = os.path.join(TEMPLATES_DIR, "templates.json")

if not os.path.exists(TEMPLATES_DIR):
    os.makedirs(TEMPLATES_DIR)

# ============================================================
# HELPER FUNCTIONS
# ============================================================

# ============================================================
# SIDE DETECTION (L/R) — 1 sumber kebenaran untuk semua fungsi
# ============================================================
SIDE_TOKEN = "{side}"          # placeholder posisi L/R di dalam base_name

# Token untuk semua variasi side
_TOKEN = r'(?:LEFT|RIGHT|Left|Right|left|right|L|R|l|r)'
_SEP = r'[_.\-]'

# Token khusus untuk prefix/middle CamelCase (tanpa separator)
_CAMEL_TOKEN = r'(?:LEFT|RIGHT|Left|Right|L|R)'

# Urutan penting: yang paling spesifik dulu
_SIDE_PATTERNS = (
    # 1. Suffix dengan separator: Bone_L, Bone.LEFT, Bone.left
    re.compile(r'(?P<sep>' + _SEP + r')(?P<side>' + _TOKEN + r')$'),
    
    # 2. Prefix dengan separator: L_Bone, LEFT.Bone, left_Bone
    re.compile(r'^(?P<side>' + _TOKEN + r')(?P<sep>' + _SEP + r')'),
    
    # 3. Middle dengan separator: Bone_L_Tip, Bone_LEFT_Tip
    re.compile(r'(?P<sep>' + _SEP + r')(?P<side>' + _TOKEN + r')(?=' + _SEP + r')'),
    
    # 4. Suffix tanpa separator: upperarmL, upperarmLEFT, upperarmLeft
    re.compile(r'(?<=[a-z0-9])(?P<side>' + _TOKEN + r')$'),
    
    # 5. Prefix tanpa separator (CamelCase): LUpperarm, LEFTUpperarm, LeftUpperarm
    re.compile(r'^(?P<side>' + _CAMEL_TOKEN + r')(?=[A-Z][a-z])'),
    
    # 6. Middle tanpa separator (CamelCase): upperarmLTip, upperarmLEFTTip
    re.compile(r'(?<=[a-z0-9])(?P<side>' + _CAMEL_TOKEN + r')(?=[A-Z][a-z])'),
)

def get_case_type(suffix):
    """Tentukan case type dari token side"""
    if suffix in ('L', 'R'):
        return 'upper'
    if suffix in ('l', 'r'):
        return 'lower'
    if suffix in ('Left', 'Right'):
        return 'title'
    if suffix in ('left', 'right'):
        return 'lower_title'
    if suffix in ('LEFT', 'RIGHT'):
        return 'upper_title'
    return 'upper'

def parse_side_name(name):
    """
    Parser UTAMA. Return:
    (base_name, separator, case_type, is_prefix, side)
    - base_name mengandung placeholder {side} di posisi token aslinya
    - side = 'L' / 'R' / None (None = tidak ada side valid)
    """
    for pattern in _SIDE_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        token = m.group('side')
        base_name = name[:m.start('side')] + SIDE_TOKEN + name[m.end('side'):]
        separator = m.groupdict().get('sep') or ''
        
        # Penentuan side L/R lebih bersih dan mendukung semua variasi (L, l, Left, LEFT, dll)
        side = 'L' if token.upper().startswith('L') else 'R'
        is_prefix = (m.start('side') == 0)
        
        return base_name, separator, get_case_type(token), is_prefix, side
    return name, '', 'upper', False, None

def get_base_name_and_suffix(name):
    """Wrapper backward-compatible (4 nilai, tanpa side)"""
    base, sep, case, is_prefix, _side = parse_side_name(name)
    return base, sep, case, is_prefix

def format_side(side, base_name, separator, case_type, is_prefix=False):
    """
    1 helper untuk semua format. Kalau base_name punya {side},
    posisi token otomatis ikut (depan / tengah / belakang).
    """
    if case_type == 'lower':
        label = side.lower()
    elif case_type == 'title':
        label = 'Left' if side == 'L' else 'Right'
    elif case_type == 'lower_title':
        label = 'left' if side == 'L' else 'right'
    elif case_type == 'upper_title':
        label = 'LEFT' if side == 'L' else 'RIGHT'
    else:
        label = side

    if SIDE_TOKEN in base_name:
        return base_name.replace(SIDE_TOKEN, label)

    # fallback untuk template JSON lama (base_name tanpa placeholder)
    if is_prefix:
        return f"{label}{separator}{base_name}"
    return f"{base_name}{separator}{label}"

def format_suffix(side, separator, case_type):
    """Wrapper lama, biar kode lain tidak error"""
    return format_side(side, '', separator, case_type, is_prefix=False)

def find_mirror_bone(bone, all_bones):
    """Cari bone pasangan mirror"""
    target_head = mathutils.Vector((-bone.head.x, bone.head.y, bone.head.z))
    target_tail = mathutils.Vector((-bone.tail.x, bone.tail.y, bone.tail.z))

    for other in all_bones:
        if other == bone:
            continue
        if (other.head - target_head).length < TOLERANCE and \
           (other.tail - target_tail).length < TOLERANCE:
            return other
    return None

def update_vertex_groups(armature):
    """Update nama vertex group di semua mesh child"""
    mesh_objects = [obj for obj in bpy.data.objects
                    if obj.type == 'MESH' and obj.parent == armature]

    if not mesh_objects:
        return 0

    bone_names = set(bone.name for bone in armature.data.bones)
    updated_count = 0

    for mesh_obj in mesh_objects:
        if not mesh_obj.vertex_groups:
            continue

        vg_to_rename = []
        for vg in mesh_obj.vertex_groups:
            if vg.name not in bone_names:
                base_vg, sep_vg, case_vg, _ = get_base_name_and_suffix(vg.name)  # ← tambah _
                for bone_name in bone_names:
                    base_bone, sep_bone, case_bone, _ = get_base_name_and_suffix(bone_name)  # ← tambah _
                    if base_vg == base_bone:
                        vg_to_rename.append((vg.name, bone_name))
                        break

        for old_name, new_name in vg_to_rename:
            if old_name in mesh_obj.vertex_groups:
                mesh_obj.vertex_groups[old_name].name = new_name
                updated_count += 1

    return updated_count

def update_new_name_l(self, context):
    """
    Auto-mirror new_name_r dari new_name_l.
    Wajib side L. Kalau tidak ada side / side-nya R → rollback.
    """
    if self.get("_mirror_updating", False):
        return

    name = self.new_name_l
    base, sep, case, is_prefix, side = parse_side_name(name)

    self["_mirror_updating"] = True
    try:
        if side == 'L':
            l_name = format_side('L', base, sep, case, is_prefix)
            r_name = format_side('R', base, sep, case, is_prefix)

            self["last_valid_new_name_l"] = l_name
            if self.new_name_l != l_name:
                self.new_name_l = l_name
            if self.new_name_r != r_name:
                self.new_name_r = r_name
        else:
            reason = ("memakai side R (kanan), field ini untuk side L"
                      if side == 'R' else
                      "tidak punya token L/l/Left/left yang valid")
            print(f"[Bone Renamer] DITOLAK: '{name}' {reason}. Perubahan dibatalkan.")

            fallback = self.get("last_valid_new_name_l", self.old_name_l)
            if self.new_name_l != fallback:
                self.new_name_l = fallback
    finally:
        self["_mirror_updating"] = False

# ============================================================
# TEMPLATE FILE MANAGEMENT
# ============================================================

def load_templates_data():
    """Load template data dari file JSON"""
    if os.path.exists(TEMPLATES_FILE):
        try:
            with open(TEMPLATES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading templates: {e}")
    return {"templates": {}, "active_template": ""}

def save_templates_data(data):
    """Save template data ke file JSON"""
    try:
        with open(TEMPLATES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving templates: {e}")
        return False

# ============================================================
# PROPERTY GROUPS
# ============================================================

class TEMPLATE_BONE_ITEM(PropertyGroup):
    """Item untuk menyimpan data 1 bone (atau 1 mirror pair) di template"""
    base_name: StringProperty(name="Base Name")
    separator: StringProperty(name="Separator")
    case_type: StringProperty(name="Case Type")
    is_mirror: BoolProperty(name="Is Mirror", default=False)
    is_prefix: BoolProperty(name="Is Prefix", default=False)

    # Untuk single bone (non-mirror)
    old_name: StringProperty(name="Old Name")
    new_name: StringProperty(name="New Name")

    # Untuk mirror pair
    old_name_l: StringProperty(name="Old Name L")
    old_name_r: StringProperty(name="Old Name R")
    new_name_l: StringProperty(name="New Name L", update=update_new_name_l)
    new_name_r: StringProperty(name="New Name R")

    def to_dict(self):
        """Convert ke dictionary untuk save ke JSON"""
        data = {
            'base_name': self.base_name,
            'separator': self.separator,
            'case_type': self.case_type,
            'is_mirror': self.is_mirror,
            'is_prefix': self.is_prefix,  # ← simpan ke JSON
        }
        if self.is_mirror:
            data['old_name_l'] = self.old_name_l
            data['old_name_r'] = self.old_name_r
            data['new_name_l'] = self.new_name_l
            data['new_name_r'] = self.new_name_r
        else:
            data['old_name'] = self.old_name
            data['new_name'] = self.new_name
        return data

    def from_dict(self, data):
        """Load dari dictionary"""
        self.base_name = data.get('base_name', '')
        self.separator = data.get('separator', '')
        self.case_type = data.get('case_type', 'upper')
        self.is_mirror = data.get('is_mirror', False)
        self.is_prefix = data.get('is_prefix', False)  # ← load dari JSON

        if self.is_mirror:
            self.old_name_l = data.get('old_name_l', '')
            self.old_name_r = data.get('old_name_r', '')
            self.new_name_l = data.get('new_name_l', '')
            self.new_name_r = data.get('new_name_r', '')
        else:
            self.old_name = data.get('old_name', '')
            self.new_name = data.get('new_name', '')

class TEMPLATE_ITEM(PropertyGroup):
    """Item untuk menyimpan 1 template"""
    name: StringProperty(name="Template Name")
    created: StringProperty(name="Created")
    modified: StringProperty(name="Modified")

# ============================================================
# UI LIST
# ============================================================

class TEMPLATE_UL_bone_list(UIList):
    """UIList untuk menampilkan bone di template"""
    bl_idname = "TEMPLATE_UL_bone_list"
    
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Layout utama
        row = layout.row(align=True)
        
        # 1. Icon Mirror / Single (Kiri)
        if item.is_mirror:
            row.label(text="", icon='MOD_MIRROR')
        else:
            row.label(text="", icon='BONE_DATA')
            
        # 2. Old Name (Dijadikan Label agar kolomnya kecil & rapi, hanya selebar teks)
        if item.is_mirror:
            row.label(text=item.old_name_l)
        else:
            row.label(text=item.old_name)
            
        # 3. Tanda Panah (Kecil)
        row.label(text="", icon='RIGHTARROW')
        
        # 4. New Name (Input Field, mengambil seluruh sisa ruang yang ada)
        if item.is_mirror:
            row.prop(item, "new_name_l", text="")
        else:
            row.prop(item, "new_name", text="")

# ============================================================
# TEMPLATE DATA MANAGEMENT (Runtime)
# ============================================================

def get_all_templates(scene):
    """Ambil list semua template"""
    return scene.bone_renamer_template_list

def get_active_template_index(scene):
    """Ambil index template aktif"""
    return scene.bone_renamer_active_template_index

def get_active_template(scene):
    """Ambil template aktif"""
    templates = get_all_templates(scene)
    idx = get_active_template_index(scene)
    if 0 <= idx < len(templates):
        return templates[idx]
    return None

def get_active_template_bones_collection(scene):
    """Ambil collection bone dari template aktif"""
    return scene.bone_renamer_bone_list

def load_templates_to_scene(scene):
    """Load templates dari file ke scene properties"""
    data = load_templates_data()

    # Clear existing
    scene.bone_renamer_template_list.clear()
    scene.bone_renamer_bone_list.clear()

    templates = data.get("templates", {})
    active_name = data.get("active_template", "")
    active_idx = 0

    # Load semua template
    for i, (t_name, t_data) in enumerate(templates.items()):
        item = scene.bone_renamer_template_list.add()
        item.name = t_name
        item.created = t_data.get("created", "")
        item.modified = t_data.get("modified", "")

        if t_name == active_name:
            active_idx = i

    scene.bone_renamer_active_template_index = active_idx

    # Load bone list dari template aktif
    load_active_template_bones(scene)

def save_scene_templates_to_file(scene):
    """Save scene templates ke file"""
    data = {"templates": {}, "active_template": ""}
    templates = get_all_templates(scene)
    active_idx = get_active_template_index(scene)

    for i, t_item in enumerate(templates):
        t_name = t_item.name
        bones_collection = scene.bone_renamer_bone_list if i == active_idx else []

        # Convert bones ke list of dict
        bones_data = []
        for bone_item in bones_collection:
            bones_data.append(bone_item.to_dict())

        data["templates"][t_name] = {
            "created": t_item.created,
            "modified": t_item.modified,
            "bones": bones_data
        }

        if i == active_idx:
            data["active_template"] = t_name

    save_templates_data(data)

def load_active_template_bones(scene):
    """Load bone list dari template aktif ke scene"""
    scene.bone_renamer_bone_list.clear()
    template = get_active_template(scene)
    if not template:
        return

    data = load_templates_data()
    t_name = template.name
    if t_name not in data.get("templates", {}):
        return

    bones_data = data["templates"][t_name].get("bones", [])
    for bone_data in bones_data:
        bone_item = scene.bone_renamer_bone_list.add()
        bone_item.from_dict(bone_data)

def add_bones_to_active_template(scene, bones_to_add, armature_obj):
    """
    Tambah bones ke template aktif dengan mirror detection
    bones_to_add: list of edit_bones yang terseleksi
    armature_obj: objek armature aktif
    """
    template = get_active_template(scene)
    if not template:
        return 0, 0

    bones_collection = scene.bone_renamer_bone_list
    
    # PERBAIKAN: Ambil SEMUA edit bones dari armature, bukan hanya yang terseleksi
    all_edit_bones = armature_obj.data.edit_bones

    # Buat set dari bone yang sudah ada di template
    existing_bones = set()
    for item in bones_collection:
        if item.is_mirror:
            existing_bones.add(item.old_name_l)
            existing_bones.add(item.old_name_r)
        else:
            existing_bones.add(item.old_name)

    processed = set()
    added_count = 0
    skipped_count = 0

    for bone in bones_to_add:
        if bone.name in processed:
            continue

        # PERBAIKAN: Cari mirror pair di SELURUH armature
        mirror_bone = find_mirror_bone(bone, all_edit_bones)

        # Skip jika bone ini sendiri sudah ada di template
        if bone.name in existing_bones:
            skipped_count += 1
            processed.add(bone.name)
            continue

        # PERBAIKAN BARU: Skip jika mirror dari bone ini sudah ada di template
        if mirror_bone and mirror_bone.name in existing_bones:
            skipped_count += 1
            processed.add(bone.name)
            continue

        # Parse nama bone
        base_name, separator, case_type, is_prefix = get_base_name_and_suffix(bone.name)

        if mirror_bone:
            # Mirror pair ditemukan dan belum ada di template
            processed.add(bone.name)
            processed.add(mirror_bone.name)

            # Tentukan mana L dan mana R berdasarkan posisi X
            if bone.head.x > 0:
                l_bone, r_bone = bone, mirror_bone
            else:
                l_bone, r_bone = mirror_bone, bone

            # Buat bone item baru sebagai mirror pair
            bone_item = bones_collection.add()
            bone_item.base_name = base_name
            bone_item.separator = separator
            bone_item.case_type = case_type
            bone_item.is_mirror = True
            bone_item.is_prefix = is_prefix
            bone_item.old_name_l = l_bone.name
            bone_item.old_name_r = r_bone.name
            bone_item.new_name_l = l_bone.name
            bone_item.new_name_r = r_bone.name
            added_count += 1
        else:
            # Single bone (no mirror)
            processed.add(bone.name)
            bone_item = bones_collection.add()
            bone_item.base_name = base_name
            bone_item.separator = separator
            bone_item.case_type = case_type
            bone_item.is_mirror = False
            bone_item.old_name = bone.name
            bone_item.new_name = bone.name
            added_count += 1

    # Update modified time
    template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Save ke file
    save_scene_templates_to_file(scene)

    # Return jumlah yang ditambah dan yang di-skip
    return added_count, skipped_count

# ============================================================
# OPERATORS - TEMPLATE MANAGEMENT
# ============================================================

class TEMPLATE_OT_new(bpy.types.Operator):
    """Buat template baru"""
    bl_idname = "bone_renamer.new_template"
    bl_label = "New Template"
    bl_options = {'REGISTER'}

    name: StringProperty(name="Template Name", default="New Template")

    def invoke(self, context, event):
        wm = context.window_manager
        return wm.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "name")

    def execute(self, context):
        if not self.name.strip():
            self.report({'ERROR'}, "Nama template tidak boleh kosong")
            return {'CANCELLED'}

        scene = context.scene
        templates = get_all_templates(scene)

        # Cek duplikat
        for t in templates:
            if t.name == self.name:
                self.report({'ERROR'}, f"Template '{self.name}' sudah ada")
                return {'CANCELLED'}

        # Tambah template baru
        item = templates.add()
        item.name = self.name
        item.created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item.modified = item.created

        # Set sebagai aktif
        scene.bone_renamer_active_template_index = len(templates) - 1
        scene.bone_renamer_bone_list.clear()

        # Save ke file
        save_scene_templates_to_file(scene)

        self.report({'INFO'}, f"Template '{self.name}' telah dibuat")
        return {'FINISHED'}

class TEMPLATE_OT_delete(bpy.types.Operator):
    """Hapus template aktif"""
    bl_idname = "bone_renamer.delete_template"
    bl_label = "Delete Template"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        template = get_active_template(context.scene)
        if not template:
            self.report({'WARNING'}, "Tidak ada template aktif")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        templates = get_all_templates(scene)
        idx = get_active_template_index(scene)

        if idx < 0 or idx >= len(templates):
            self.report({'WARNING'}, "Tidak ada template aktif")
            return {'CANCELLED'}

        t_name = templates[idx].name
        templates.remove(idx)

        # Adjust active index
        if len(templates) > 0:
            scene.bone_renamer_active_template_index = min(idx, len(templates) - 1)
        else:
            scene.bone_renamer_active_template_index = 0

        scene.bone_renamer_bone_list.clear()

        # Save ke file
        save_scene_templates_to_file(scene)

        self.report({'INFO'}, f"Template '{t_name}' telah dihapus")
        return {'FINISHED'}

class TEMPLATE_OT_add_selected_bones(bpy.types.Operator):
    """Tambahkan bone yang terseleksi ke template aktif (dengan auto mirror detection)"""
    bl_idname = "bone_renamer.add_selected_bones"
    bl_label = "Add Selected Bones to Template"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = context.object
        scene = context.scene
        
        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Silakan pilih objek Armature")
            return {'CANCELLED'}
        if obj.mode != 'EDIT':
            self.report({'ERROR'}, "Harus di Edit Mode")
            return {'CANCELLED'}
            
        template = get_active_template(scene)
        if not template:
            self.report({'ERROR'}, "Tidak ada template aktif. Buat template baru dulu.")
            return {'CANCELLED'}

        selected = [b for b in obj.data.edit_bones if b.select]
        if not selected:
            self.report({'WARNING'}, "Tidak ada bone yang terseleksi")
            return {'CANCELLED'}

        # PERBAIKAN: Kirim objek armature (obj) ke fungsi helper
        added_count, skipped_count = add_bones_to_active_template(scene, selected, obj)

        # Laporan hasil yang lebih detail
        msg = f"{added_count} bone entry ditambahkan ke template '{template.name}'."
        if skipped_count > 0:
            msg += f" ({skipped_count} bone dilewati karena sudah ada di template atau mirrornya sudah ada)."
            
        self.report({'INFO'}, msg)
        return {'FINISHED'}

class TEMPLATE_OT_remove_bone(bpy.types.Operator):
    """Hapus bone yang terpilih dari template"""
    bl_idname = "bone_renamer.remove_bone"
    bl_label = "Remove Bone from Template"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        bones = scene.bone_renamer_bone_list
        idx = scene.bone_renamer_active_bone_index

        if idx < 0 or idx >= len(bones):
            self.report({'WARNING'}, "Tidak ada bone yang terpilih")
            return {'CANCELLED'}

        bones.remove(idx)

        # Adjust index
        if len(bones) > 0:
            scene.bone_renamer_active_bone_index = min(idx, len(bones) - 1)
        else:
            scene.bone_renamer_active_bone_index = 0

        # Save ke file
        template = get_active_template(scene)
        if template:
            template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_scene_templates_to_file(scene)

        self.report({'INFO'}, "Bone dihapus dari template")
        return {'FINISHED'}

class TEMPLATE_OT_clear_bones(bpy.types.Operator):
    """Kosongkan semua bone di template aktif"""
    bl_idname = "bone_renamer.clear_bones"
    bl_label = "Clear All Bones"
    bl_options = {'REGISTER'}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        scene = context.scene
        scene.bone_renamer_bone_list.clear()
        scene.bone_renamer_active_bone_index = 0

        template = get_active_template(scene)
        if template:
            template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_scene_templates_to_file(scene)

        self.report({'INFO'}, "Semua bone di template telah dihapus")
        return {'FINISHED'}

class TEMPLATE_OT_select_bone(bpy.types.Operator):
    """Select bone di armature berdasarkan template item yang aktif (bisa di Edit/Pose Mode)"""
    bl_idname = "bone_renamer.select_bone"
    bl_label = "Select Bone in Armature"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        scene = context.scene

        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Silakan pilih objek Armature")
            return {'CANCELLED'}

        if obj.mode not in ('EDIT', 'POSE'):
            self.report({'ERROR'}, "Harus di Edit Mode atau Pose Mode")
            return {'CANCELLED'}

        bones = scene.bone_renamer_bone_list
        idx = scene.bone_renamer_active_bone_index

        if idx < 0 or idx >= len(bones):
            self.report({'WARNING'}, "Tidak ada bone yang terpilih")
            return {'CANCELLED'}

        bone_item = bones[idx]

        # Kumpulkan nama bone yang akan di-select (tunggal atau mirror pair)
        target_names = []
        if bone_item.is_mirror:
            target_names = [bone_item.old_name_l, bone_item.old_name_r]
        else:
            target_names = [bone_item.old_name]

        # ===== PERBAIKAN: DESELECT SEMUA BONE MENGGUNAKAN OPERATOR =====
        # Ini 100% aman dan menghindari error 'Bone object has no attribute select'
        if obj.mode == 'EDIT':
            bpy.ops.armature.select_all(action='DESELECT')
        else:  # POSE MODE
            bpy.ops.pose.select_all(action='DESELECT')

        # ===== SELECT BONE TARGET =====
        selected_count = 0
        
        if obj.mode == 'EDIT':
            for name in target_names:
                if name in obj.data.edit_bones:
                    b = obj.data.edit_bones[name]
                    b.select = True
                    b.select_head = True
                    b.select_tail = True
                    selected_count += 1
        else:  # POSE MODE
            for name in target_names:
                if name in obj.data.bones:
                    # Select bone spesifik di pose mode
                    obj.pose.bones[name].select = True
                    selected_count += 1

        self.report({'INFO'}, f"{selected_count} bone terseleksi")
        return {'FINISHED'}

class TEMPLATE_OT_rename_from_template(bpy.types.Operator):
    """Rename bone berdasarkan template aktif + update vertex groups"""
    bl_idname = "bone_renamer.rename_from_template"
    bl_label = "Apply Rename from Template"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.object
        scene = context.scene

        if not obj or obj.type != 'ARMATURE':
            self.report({'ERROR'}, "Silakan pilih objek Armature")
            return {'CANCELLED'}

        if obj.mode != 'EDIT':
            self.report({'ERROR'}, "Harus di Edit Mode")
            return {'CANCELLED'}

        template = get_active_template(scene)
        if not template:
            self.report({'ERROR'}, "Tidak ada template aktif")
            return {'CANCELLED'}

        bones = scene.bone_renamer_bone_list
        if len(bones) == 0:
            self.report({'WARNING'}, "Template kosong")
            return {'CANCELLED'}

        renamed_count = 0
        edit_bones = obj.data.edit_bones

        # Rename semua bone di template
        for bone_item in bones:
            if bone_item.is_mirror:
                # Rename L
                if bone_item.old_name_l in edit_bones and \
                   bone_item.old_name_l != bone_item.new_name_l:
                    # Cek nama baru tidak bentrok
                    if bone_item.new_name_l not in edit_bones:
                        edit_bones[bone_item.old_name_l].name = bone_item.new_name_l
                        renamed_count += 1
                # Rename R
                if bone_item.old_name_r in edit_bones and \
                   bone_item.old_name_r != bone_item.new_name_r:
                    if bone_item.new_name_r not in edit_bones:
                        edit_bones[bone_item.old_name_r].name = bone_item.new_name_r
                        renamed_count += 1
            else:
                # Single bone
                if bone_item.old_name in edit_bones and \
                   bone_item.old_name != bone_item.new_name:
                    if bone_item.new_name not in edit_bones:
                        edit_bones[bone_item.old_name].name = bone_item.new_name
                        renamed_count += 1

        self.report({'INFO'}, f"{renamed_count} bone telah di-rename")

        # Update vertex groups
        bpy.ops.object.mode_set(mode='OBJECT')
        vg_count = update_vertex_groups(obj)
        bpy.ops.object.mode_set(mode='EDIT')

        if vg_count > 0:
            self.report({'INFO'}, f"{vg_count} vertex group telah diupdate")

        # Update template dengan nama baru
        for bone_item in bones:
            if bone_item.is_mirror:
                bone_item.old_name_l = bone_item.new_name_l
                bone_item.old_name_r = bone_item.new_name_r
            else:
                bone_item.old_name = bone_item.new_name

        template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_scene_templates_to_file(scene)

        return {'FINISHED'}

class TEMPLATE_OT_export(bpy.types.Operator):
    """Export template aktif ke file JSON"""
    bl_idname = "bone_renamer.export_template"
    bl_label = "Export Template"
    filename_ext = ".json"

    filepath: StringProperty(subtype='FILE_PATH', default="template.json")

    def invoke(self, context, event):
        template = get_active_template(context.scene)
        if not template:
            self.report({'ERROR'}, "Tidak ada template aktif")
            return {'CANCELLED'}
        self.filepath = f"{template.name}.json"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        scene = context.scene
        template = get_active_template(scene)
        if not template:
            self.report({'ERROR'}, "Tidak ada template aktif")
            return {'CANCELLED'}

        # Build export data
        bones_data = []
        for bone_item in scene.bone_renamer_bone_list:
            bones_data.append(bone_item.to_dict())

        export_data = {
            "template_name": template.name,
            "created": template.created,
            "modified": template.modified,
            "bones": bones_data
        }

        filepath = self.filepath
        if not filepath.lower().endswith('.json'):
            filepath += '.json'

        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2, ensure_ascii=False)
            self.report({'INFO'}, f"Template '{template.name}' telah di-export")
        except Exception as e:
            self.report({'ERROR'}, f"Error: {str(e)}")
            return {'CANCELLED'}

        return {'FINISHED'}

class TEMPLATE_OT_import(bpy.types.Operator):
    """Import template dari file JSON"""
    bl_idname = "bone_renamer.import_template"
    bl_label = "Import Template"
    filename_ext = ".json"

    filepath: StringProperty(subtype='FILE_PATH')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        scene = context.scene
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                import_data = json.load(f)
        except Exception as e:
            self.report({'ERROR'}, f"Error membaca file: {str(e)}")
            return {'CANCELLED'}

        # Validasi
        if "template_name" not in import_data or "bones" not in import_data:
            self.report({'ERROR'}, "Format file tidak valid")
            return {'CANCELLED'}

        template_name = import_data["template_name"]

        # Cek duplikat nama
        templates = get_all_templates(scene)
        for t in templates:
            if t.name == template_name:
                # Tambah suffix
                i = 1
                new_name = f"{template_name}_{i}"
                exists = True
                while exists:
                    exists = False
                    for t2 in templates:
                        if t2.name == new_name:
                            exists = True
                            i += 1
                            new_name = f"{template_name}_{i}"
                            break
                template_name = new_name
                self.report({'INFO'}, f"Nama template sudah ada, di-import sebagai '{new_name}'")
                break

        # Tambah template baru
        item = templates.add()
        item.name = template_name
        item.created = import_data.get("created", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        item.modified = import_data.get("modified", item.created)

        # Set sebagai aktif
        scene.bone_renamer_active_template_index = len(templates) - 1

        # Load bones
        scene.bone_renamer_bone_list.clear()
        for bone_data in import_data.get("bones", []):
            bone_item = scene.bone_renamer_bone_list.add()
            bone_item.from_dict(bone_data)

        # Save ke file
        save_scene_templates_to_file(scene)

        self.report({'INFO'}, f"Template '{template_name}' telah di-import")
        return {'FINISHED'}

class TEMPLATE_OT_refresh(bpy.types.Operator):
    """Reload templates dari file"""
    bl_idname = "bone_renamer.refresh"
    bl_label = "Refresh"
    bl_options = {'REGISTER'}

    def execute(self, context):
        load_templates_to_scene(context.scene)
        self.report({'INFO'}, "Templates telah di-reload")
        return {'FINISHED'}

class TEMPLATE_OT_select_template(bpy.types.Operator):
    """Pilih template sebagai aktif"""
    bl_idname = "bone_renamer.select_template"
    bl_label = "Select Template"
    bl_options = {'REGISTER'}

    index: IntProperty()

    def execute(self, context):
        scene = context.scene
        scene.bone_renamer_active_template_index = self.index
        load_active_template_bones(scene)
        return {'FINISHED'}

# ============================================================
# UI PANEL
# ============================================================

class VIEW3D_PT_bone_renamer(Panel):
    """Panel untuk Velcent Bone Renamer"""
    bl_label = "Velcent Bone Renamer"
    bl_idname = "VIEW3D_PT_velcent_bone_renamer"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Bone Renamer'

    def draw(self, context):
        layout = self.layout
        obj = context.object
        scene = context.scene

        # Info armature
        if obj and obj.type == 'ARMATURE':
            layout.label(text=f"Armature: {obj.name}", icon='ARMATURE_DATA')
            layout.label(text=f"Mode: {obj.mode}", icon='EDITMODE_HLT' if obj.mode == 'EDIT' else 'OBJECT_DATA')
        else:
            layout.label(text="Pilih Armature", icon='ERROR')

        layout.separator()

        # ===== TEMPLATE LIST =====
        box = layout.box()
        box.label(text="Templates", icon='PRESET')

        templates = get_all_templates(scene)
        active_idx = get_active_template_index(scene)

        # List template dengan radio button
        if len(templates) > 0:
            col = box.column(align=True)
            for i, t in enumerate(templates):
                row = col.row(align=True)
                # Radio button
                if i == active_idx:
                    row.label(text="", icon='RADIOBUT_ON')
                else:
                    op = row.operator("bone_renamer.select_template", text="", icon='RADIOBUT_OFF')
                    op.index = i
                # Nama template
                row.label(text=t.name)
                # Jumlah bone
                bone_count = len(scene.bone_renamer_bone_list) if i == active_idx else "?"
                row.label(text=f"({bone_count})", icon='BONE_DATA')
        else:
            box.label(text="Belum ada template", icon='INFO')

        # Tombol manajemen template
        row = box.row(align=True)
        row.operator("bone_renamer.new_template", text="New", icon='ADD')
        row.operator("bone_renamer.delete_template", text="Delete", icon='REMOVE')
        row.operator("bone_renamer.refresh", text="", icon='FILE_REFRESH')

        # Import/Export
        row = box.row(align=True)
        row.operator("bone_renamer.export_template", text="Export", icon='EXPORT')
        row.operator("bone_renamer.import_template", text="Import", icon='IMPORT')

        layout.separator()

        # ===== BONE LIST =====
        template = get_active_template(scene)
        if template:
            box = layout.box()
            box.label(text=f"Bones in '{template.name}'", icon='BONE_DATA')

            bones = scene.bone_renamer_bone_list

            # UIList
            row = box.row()
            row.template_list(
                "TEMPLATE_UL_bone_list", "",
                scene, "bone_renamer_bone_list",
                scene, "bone_renamer_active_bone_index",
                rows=6
            )

            # Tombol manajemen bone
            col = row.column(align=True)
            col.operator("bone_renamer.add_selected_bones", text="", icon='ADD')
            col.operator("bone_renamer.remove_bone", text="", icon='REMOVE')
            col.operator("bone_renamer.clear_bones", text="", icon='TRASH')
            col.separator()
            col.operator("bone_renamer.select_bone", text="", icon='RESTRICT_SELECT_OFF')

            # ===== APPLY RENAME =====
            layout.separator()
            box = layout.box()
            box.label(text="Apply Rename", icon='CHECKMARK')
            box.operator("bone_renamer.rename_from_template",
                        text="Apply Rename + Update Vertex Groups",
                        icon='OUTLINER_DATA_ARMATURE')
        else:
            layout.label(text="Pilih atau buat template", icon='INFO')

# ============================================================
# REGISTER
# ============================================================

classes = (
    TEMPLATE_BONE_ITEM,
    TEMPLATE_ITEM,
    TEMPLATE_UL_bone_list,
    TEMPLATE_OT_new,
    TEMPLATE_OT_delete,
    TEMPLATE_OT_add_selected_bones,
    TEMPLATE_OT_remove_bone,
    TEMPLATE_OT_clear_bones,
    TEMPLATE_OT_select_bone,
    TEMPLATE_OT_rename_from_template,
    TEMPLATE_OT_export,
    TEMPLATE_OT_import,
    TEMPLATE_OT_refresh,
    TEMPLATE_OT_select_template,
    VIEW3D_PT_bone_renamer,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Register scene properties
    bpy.types.Scene.bone_renamer_template_list = CollectionProperty(type=TEMPLATE_ITEM)
    bpy.types.Scene.bone_renamer_active_template_index = IntProperty(default=0)
    bpy.types.Scene.bone_renamer_bone_list = CollectionProperty(type=TEMPLATE_BONE_ITEM)
    bpy.types.Scene.bone_renamer_active_bone_index = IntProperty(default=0)

    # Tunda loading template sampai Blender sepenuhnya siap (bpy.data tidak lagi restricted)
    def delayed_load_templates():
        for scene in bpy.data.scenes:
            load_templates_to_scene(scene)
        return None  # Return None agar timer tidak diulang

    bpy.app.timers.register(delayed_load_templates, first_interval=0.1)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.bone_renamer_active_bone_index
    del bpy.types.Scene.bone_renamer_bone_list
    del bpy.types.Scene.bone_renamer_active_template_index
    del bpy.types.Scene.bone_renamer_template_list

if __name__ == "__main__":
    register()
