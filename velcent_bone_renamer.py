bl_info = {
    "name": "Velcent Bone Renamer",
    "author": "Velcent Games",
    "version": (3, 1),
    "blender": (5, 0, 0),
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
from bpy.props import (StringProperty, BoolProperty, IntProperty, CollectionProperty, EnumProperty, FloatProperty)
from bpy.types import PropertyGroup, UIList, Operator, Panel

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
_SEP = r'[_.-]'

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

def find_mirror_bone_by_name(bone, all_bones):
    """
    Cari bone pasangan mirror.
    Kriteria match:
    - base_name sama
    - separator sama
    - is_prefix sama
    - side berlawanan (L ↔ R)
    Return: bone pasangan, atau None jika tidak ada.
    """
    base, sep, _case, is_prefix, side = parse_side_name(bone.name)
    if side is None:
        return None  # bone ini tidak punya token side → bukan mirror

    opposite_side = 'R' if side == 'L' else 'L'

    for other in all_bones:
        if other.name == bone.name:
            continue
        
        other_base, other_sep, _other_case, other_is_prefix, other_side = parse_side_name(other.name)
        
        if (other_base == base and
            other_side == opposite_side and
            other_sep == sep and
            other_is_prefix == is_prefix):
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
                base_vg, sep_vg, case_vg, _ = get_base_name_and_suffix(vg.name)
                for bone_name in bone_names:
                    base_bone, sep_bone, case_bone, _ = get_base_name_and_suffix(bone_name)
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

def get_active_armature(context):
    """
    Dapatkan armature aktif berdasarkan mode.
    Return: (armature_obj, mode_label) atau (None, None)
    - EDIT/POSE/OBJECT: armature_obj = context.object
    - WEIGHT_PAINT: armature_obj = mesh.parent (jika armature)
    """
    obj = context.object
    if not obj:
        return None, None

    if obj.type == 'ARMATURE':
        return obj, obj.mode
    elif obj.type == 'MESH' and obj.mode == 'WEIGHT_PAINT':
        if obj.parent and obj.parent.type == 'ARMATURE':
            return obj.parent, 'WEIGHT_PAINT'
        return None, None

    return None, None

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
            'is_prefix': self.is_prefix,
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
        self.is_prefix = data.get('is_prefix', False)
        
        if self.is_mirror:
            self.old_name_l = data.get('old_name_l', '')
            self.old_name_r = data.get('old_name_r', '')
            self.new_name_l = data.get('new_name_l', '')
            self.new_name_r = data.get('new_name_r', '')
        else:
            self.old_name = data.get('old_name', '')
            self.new_name = data.get('new_name', '')


def get_template_enum_items(self, context):
    """Callback untuk dropdown template"""
    scene = context.scene
    templates = scene.bone_renamer_template_list
    items = []
    for i, t in enumerate(templates):
        # identifier = index (string), label = nama, description = jumlah bone
        items.append((str(i), t.name, f"{t.bone_count} bones", 'PRESET', i))
    if not items:
        items.append(('NONE', 'Tidak ada template', '', 'INFO', 0))
    return items

def on_template_enum_change(self, context):
    """Dipanggil saat user ganti template via dropdown"""
    if self.bone_renamer_template_enum == 'NONE':
        return
    new_idx = int(self.bone_renamer_template_enum)
    if self.bone_renamer_active_template_index != new_idx:
        # Save dulu template lama, baru pindah
        save_scene_templates_to_file(self)
        self.bone_renamer_active_template_index = new_idx
        data = load_templates_data()
        templates = get_all_templates(self)
        if 0 <= new_idx < len(templates):
            data["active_template"] = templates[new_idx].name
            save_templates_data(data)


class TEMPLATE_ITEM(PropertyGroup):
    """Item untuk menyimpan 1 template"""
    name: StringProperty(name="Template Name")
    created: StringProperty(name="Created")
    modified: StringProperty(name="Modified")
    bone_count: IntProperty(name="Bone Count", default=0)
    
    # PERBAIKAN KRITIS: Simpan list bone langsung di dalam template item
    # Ini mencegah data hilang saat sinkronisasi antara memory Blender dan JSON
    bones: CollectionProperty(type=TEMPLATE_BONE_ITEM)


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
        
        # 2. Old Name
        if item.is_mirror:
            row.label(text=item.old_name_l)
        else:
            row.label(text=item.old_name)
            
        # 3. Tanda Panah (Kecil)
        row.label(text="", icon='RIGHTARROW')
        
        # 4. New Name (Input Field)
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
    template = get_active_template(scene)
    if template:
        return template.bones
    return None

def load_templates_to_scene(scene):
    """Load templates dari file ke scene properties"""
    data = load_templates_data()
    
    # Clear existing
    scene.bone_renamer_template_list.clear()
    
    templates = data.get("templates", {})
    active_name = data.get("active_template", "")
    active_idx = 0

    # Load semua template
    for i, (t_name, t_data) in enumerate(templates.items()):
        item = scene.bone_renamer_template_list.add()
        item.name = t_name
        item.created = t_data.get("created", "")
        item.modified = t_data.get("modified", "")
        
        # PERBAIKAN: Load bones langsung ke dalam masing-masing template item
        for bone_data in t_data.get("bones", []):
            bone_item = item.bones.add()
            bone_item.from_dict(bone_data)
            
        item.bone_count = len(item.bones)
        
        if t_name == active_name:
            active_idx = i

    scene.bone_renamer_active_template_index = active_idx

def save_scene_templates_to_file(scene):
    """Save scene templates ke file"""
    data = {"templates": {}, "active_template": ""}
    templates = get_all_templates(scene)
    active_idx = get_active_template_index(scene)
    
    for i, t_item in enumerate(templates):
        t_name = t_item.name
        
        # PERBAIKAN: Ambil bones langsung dari template item, bukan dari global scene list
        bones_data = [bone_item.to_dict() for bone_item in t_item.bones]
        
        if i == active_idx:
            data["active_template"] = t_name
            
        t_item.bone_count = len(bones_data)
        data["templates"][t_name] = {
            "created": t_item.created,
            "modified": t_item.modified,
            "bones": bones_data
        }
        
    save_templates_data(data)

def add_bones_to_active_template(scene, bones_to_add, armature_obj, all_bones):
    """
    Tambah bones ke template aktif dengan mirror detection BERBASIS NAMA.
    """
    template = get_active_template(scene)
    if not template:
        return 0, 0
        
    bones_collection = template.bones

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

        # Mirror detection
        mirror_bone = find_mirror_bone_by_name(bone, all_bones)

        # Skip jika bone ini sendiri sudah ada di template
        if bone.name in existing_bones:
            skipped_count += 1
            processed.add(bone.name)
            continue

        # Skip jika mirror dari bone ini sudah ada di template
        if mirror_bone and mirror_bone.name in existing_bones:
            skipped_count += 1
            processed.add(bone.name)
            continue

        # Parse nama bone
        base_name, separator, case_type, is_prefix, side = parse_side_name(bone.name)

        if mirror_bone:
            # Mirror pair ditemukan dan belum ada di template
            processed.add(bone.name)
            processed.add(mirror_bone.name)

            # Tentukan mana L dan mana R
            if side == 'L':
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
        save_scene_templates_to_file(scene) 
        
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
        
        # Save ke file
        save_scene_templates_to_file(scene)
        
        self.report({'INFO'}, f"Template '{self.name}' telah dibuat")
        return {'FINISHED'}


class TEMPLATE_OT_rename(bpy.types.Operator):
    """Rename template aktif"""
    bl_idname = "bone_renamer.rename_template"
    bl_label = "Rename Template"
    bl_options = {'REGISTER'}

    new_name: StringProperty(name="New Name", default="")

    def invoke(self, context, event):
        template = get_active_template(context.scene)
        if not template:
            self.report({'WARNING'}, "Tidak ada template aktif")
            return {'CANCELLED'}
        self.new_name = template.name
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "new_name")

    def execute(self, context):
        scene = context.scene
        template = get_active_template(scene)
        if not template:
            self.report({'WARNING'}, "Tidak ada template aktif")
            return {'CANCELLED'}

        new_name = self.new_name.strip()
        if not new_name:
            self.report({'ERROR'}, "Nama template tidak boleh kosong")
            return {'CANCELLED'}

        if new_name == template.name:
            self.report({'INFO'}, "Nama sama, tidak ada perubahan")
            return {'CANCELLED'}

        # Cek duplikat
        for t in get_all_templates(scene):
            if t == template:
                continue
            if t.name == new_name:
                self.report({'ERROR'}, f"Nama '{new_name}' sudah dipakai template lain")
                return {'CANCELLED'}

        old_name = template.name
        template.name = new_name
        template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_scene_templates_to_file(scene)

        self.report({'INFO'}, f"'{old_name}' → '{new_name}'")
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
        save_scene_templates_to_file(scene)
        
        templates = get_all_templates(scene)
        idx = get_active_template_index(scene)
        
        if idx < 0 or idx >= len(templates):
            self.report({'WARNING'}, "Tidak ada template aktif")
            return {'CANCELLED'}

        t_name = templates[idx].name
        
        # PERBAIKAN: Adjust active index dengan benar saat template dihapus
        old_active_idx = scene.bone_renamer_active_template_index
        templates.remove(idx)
        
        if len(templates) > 0:
            if idx < old_active_idx:
                scene.bone_renamer_active_template_index = old_active_idx - 1
            elif idx == old_active_idx:
                scene.bone_renamer_active_template_index = min(idx, len(templates) - 1)
            else:
                scene.bone_renamer_active_template_index = old_active_idx
        else:
            scene.bone_renamer_active_template_index = 0

        # Save ke file
        save_scene_templates_to_file(scene)
        
        self.report({'INFO'}, f"Template '{t_name}' telah dihapus")
        return {'FINISHED'}

class TEMPLATE_OT_add_selected_bones(bpy.types.Operator):
    """Tambahkan bone ke template aktif (Support Edit, Pose, Object, & Weight Paint)"""
    bl_idname = "bone_renamer.add_selected_bones"
    bl_label = "Add Bones to Template"
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        armature_obj, mode_label = get_active_armature(context)
        
        if not armature_obj:
            self.report({'ERROR'}, "Silakan pilih objek Armature")
            return {'CANCELLED'}

        template = get_active_template(scene)
        if not template:
            self.report({'ERROR'}, "Tidak ada template aktif. Buat template baru dulu.")
            return {'CANCELLED'}

        # Ambil list bone berdasarkan mode
        if mode_label == 'EDIT':
            all_bones = list(armature_obj.data.edit_bones)
            selected = [b for b in all_bones if b.select]
        elif mode_label == 'POSE':
            all_bones = list(armature_obj.pose.bones)
            selected = [b for b in all_bones if b.select]
        elif mode_label == 'WEIGHT_PAINT':
            all_bones = list(armature_obj.pose.bones)
            selected = [b for b in all_bones if b.select]
        else:  # OBJECT MODE → ambil SEMUA bone
            all_bones = list(armature_obj.data.bones)
            selected = all_bones

        if not selected:
            self.report({'WARNING'}, "Tidak ada bone yang terseleksi")
            return {'CANCELLED'}

        added_count, skipped_count = add_bones_to_active_template(
            scene, selected, armature_obj, all_bones
        )

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
        template = get_active_template(scene)
        if not template:
            self.report({'WARNING'}, "Tidak ada template aktif")
            return {'CANCELLED'}
            
        bones = template.bones
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
        template = get_active_template(scene)
        if template:
            template.bones.clear()
            scene.bone_renamer_active_bone_index = 0
            template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
        save_scene_templates_to_file(scene)
        self.report({'INFO'}, "Semua bone di template telah dihapus")
        return {'FINISHED'}

class TEMPLATE_OT_select_bone(bpy.types.Operator):
    """Select bone dari list"""
    bl_idname = "bone_renamer.select_bone"
    bl_label = "Select Bone"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        armature_obj, mode_label = get_active_armature(context)
        
        if not armature_obj:
            self.report({'ERROR'}, "Pilih Armature atau Mesh dengan parent Armature")
            return {'CANCELLED'}

        if mode_label not in ('EDIT', 'POSE', 'WEIGHT_PAINT'):
            self.report({'ERROR'}, "Tidak bisa seleksi bone di mode ini")
            return {'CANCELLED'}

        template = get_active_template(scene)
        if not template:
            self.report({'WARNING'}, "Tidak ada template aktif")
            return {'CANCELLED'}
            
        bones = template.bones
        idx = scene.bone_renamer_active_bone_index
        
        if idx < 0 or idx >= len(bones):
            self.report({'WARNING'}, "Tidak ada bone yang terpilih")
            return {'CANCELLED'}

        bone_item = bones[idx]
        target_names = []
        
        if bone_item.is_mirror:
            target_names = [bone_item.old_name_l, bone_item.old_name_r]
        else:
            target_names = [bone_item.old_name]

        if mode_label == 'EDIT':
            bpy.ops.armature.select_all(action='DESELECT')
        else:
            bpy.ops.pose.select_all(action='DESELECT')

        selected_count = 0
        
        if mode_label == 'EDIT':
            for name in target_names:
                if name in armature_obj.data.edit_bones:
                    b = armature_obj.data.edit_bones[name]
                    b.select = True
                    b.select_head = True
                    b.select_tail = True
                    selected_count += 1
        else:
            for name in target_names:
                if name in armature_obj.pose.bones:
                    armature_obj.pose.bones[name].select = True
                    selected_count += 1

        self.report({'INFO'}, f"{selected_count} bone terseleksi")
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
        for bone_item in template.bones:
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
        save_scene_templates_to_file(scene)

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
        
        # Load bones langsung ke template item
        for bone_data in import_data.get("bones", []):
            bone_item = item.bones.add()
            bone_item.from_dict(bone_data)
            
        item.bone_count = len(item.bones)

        # Set sebagai aktif
        scene.bone_renamer_active_template_index = len(templates) - 1

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
        save_scene_templates_to_file(scene)
        
        scene.bone_renamer_active_template_index = self.index
        
        templates = get_all_templates(scene)
        if 0 <= self.index < len(templates):
            data = load_templates_data()
            data["active_template"] = templates[self.index].name
            save_templates_data(data)
            
        return {'FINISHED'}


# ============================================================
# OPERATORS - SORT & MOVE BONES
# ============================================================

class TEMPLATE_OT_sort_bones(bpy.types.Operator):
    """Urutkan bones (Ascending/Descending)"""
    bl_idname = "bone_renamer.sort_bones"
    bl_label = "Sort Bones"
    bl_options = {'REGISTER', 'UNDO'}

    order: EnumProperty(
        items=[
            ('ASC', "Ascending (A-Z)", "Sort A to Z"),
            ('DESC', "Descending (Z-A)", "Sort Z to A")
        ],
        default='ASC'
    )

    def execute(self, context):
        scene = context.scene
        template = get_active_template(scene)
        if not template:
            return {'CANCELLED'}
            
        bones = template.bones
        if len(bones) < 2:
            return {'CANCELLED'}

        # Simpan nama bone yang sedang aktif agar tidak hilang setelah sort
        active_idx = scene.bone_renamer_active_bone_index
        active_name = ""
        if 0 <= active_idx < len(bones):
            item = bones[active_idx]
            active_name = item.old_name_l if item.is_mirror else item.old_name

        # Convert ke list of dict untuk di-sort
        bone_list = [item.to_dict() for item in bones]

        # Tentukan key untuk sorting
        def get_sort_key(b):
            if b['is_mirror']:
                return b['old_name_l']
            return b['old_name']

        reverse = (self.order == 'DESC')
        bone_list.sort(key=get_sort_key, reverse=reverse)

        # Clear dan re-add
        bones.clear()
        for b_data in bone_list:
            item = bones.add()
            item.from_dict(b_data)

        # Kembalikan active index ke bone yang tadi dipilih
        for i, item in enumerate(bones):
            name = item.old_name_l if item.is_mirror else item.old_name
            if name == active_name:
                scene.bone_renamer_active_bone_index = i
                break

        # Save ke file
        template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_scene_templates_to_file(scene)
        return {'FINISHED'}

class TEMPLATE_OT_move_bone(bpy.types.Operator):
    """Pindah bone ke atas/bawah"""
    bl_idname = "bone_renamer.move_bone"
    bl_label = "Move Bone"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        items=[
            ('UP', "Up", "Move up"),
            ('DOWN', "Down", "Move down")
        ]
    )

    def execute(self, context):
        scene = context.scene
        template = get_active_template(scene)
        if not template:
            return {'CANCELLED'}
            
        bones = template.bones
        idx = scene.bone_renamer_active_bone_index
        
        if idx < 0 or idx >= len(bones):
            return {'CANCELLED'}

        if self.direction == 'UP' and idx > 0:
            bones.move(idx, idx - 1)
            scene.bone_renamer_active_bone_index = idx - 1
        elif self.direction == 'DOWN' and idx < len(bones) - 1:
            bones.move(idx, idx + 1)
            scene.bone_renamer_active_bone_index = idx + 1

        # Save ke file
        template.modified = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_scene_templates_to_file(scene)
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
        scene = context.scene

        # Info armature
        armature_obj, mode_label = get_active_armature(context)
        if armature_obj:
            if mode_label == 'WEIGHT_PAINT':
                layout.label(text=f"Mesh: {context.object.name}", icon='MESH_DATA')
                layout.label(text=f"Armature: {armature_obj.name}", icon='ARMATURE_DATA')
                layout.label(text="Mode: WEIGHT_PAINT", icon='WPAINT_HLT')
            else:
                layout.label(text=f"Armature: {armature_obj.name}", icon='ARMATURE_DATA')
                mode_icons = {
                    'EDIT': 'EDITMODE_HLT',
                    'POSE': 'POSE_HLT',
                    'OBJECT': 'OBJECT_DATA',
                }
                mode_icon = mode_icons.get(mode_label, 'OBJECT_DATA')
                layout.label(text=f"Mode: {mode_label}", icon=mode_icon)
        else:
            layout.label(text="Pilih Armature atau Mesh dengan parent Armature", icon='ERROR')

        layout.separator()

        # ===== TEMPLATES (Actions + Dropdown dalam 1 box) =====
        box = layout.box()
        box.label(text="Templates", icon='PRESET')

        # Row tombol actions
        row = box.row(align=True)
        row.operator("bone_renamer.new_template", text="", icon='ADD')
        row.operator("bone_renamer.rename_template", text="", icon='OUTLINER_DATA_FONT')
        row.operator("bone_renamer.delete_template", text="", icon='REMOVE')
        row.separator()
        row.operator("bone_renamer.export_template", text="", icon='EXPORT')
        row.operator("bone_renamer.import_template", text="", icon='IMPORT')
        row.separator()
        row.operator("bone_renamer.refresh", text="", icon='FILE_REFRESH')

        # Dropdown template
        box.separator()
        box.prop(scene, "bone_renamer_template_enum", text="")

        template = get_active_template(scene)

        # ===== BONE LIST =====
        if template:
            box = layout.box()
            box.label(text=f"Bones in '{template.name}'", icon='BONE_DATA')

            # UIList
            row = box.row()
            row.template_list(
                "TEMPLATE_UL_bone_list", "",
                template, "bones",
                scene, "bone_renamer_active_bone_index",
                rows=6
            )

            # Tombol manajemen bone (ikon di kanan)
            col = row.column(align=True)
            col.operator("bone_renamer.add_selected_bones", text="", icon='ADD')
            col.operator("bone_renamer.remove_bone", text="", icon='REMOVE')
            col.operator("bone_renamer.clear_bones", text="", icon='TRASH')
            col.separator()

            # --- SORT BUTTONS ---
            op_asc = col.operator("bone_renamer.sort_bones", text="", icon='SORT_ASC')
            op_asc.order = 'ASC'
            op_desc = col.operator("bone_renamer.sort_bones", text="", icon='SORT_DESC')
            op_desc.order = 'DESC'
            col.separator()

            # --- MOVE BONE ---
            col.operator("bone_renamer.move_bone", text="", icon='TRIA_UP').direction = 'UP'
            col.operator("bone_renamer.move_bone", text="", icon='TRIA_DOWN').direction = 'DOWN'
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
    TEMPLATE_OT_rename,
    TEMPLATE_OT_add_selected_bones,
    TEMPLATE_OT_remove_bone,
    TEMPLATE_OT_clear_bones,
    TEMPLATE_OT_select_bone,
    TEMPLATE_OT_export,
    TEMPLATE_OT_import,
    TEMPLATE_OT_refresh,
    TEMPLATE_OT_select_template,
    TEMPLATE_OT_sort_bones,
    TEMPLATE_OT_move_bone,
    VIEW3D_PT_bone_renamer,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Register scene properties
    bpy.types.Scene.bone_renamer_template_list = CollectionProperty(type=TEMPLATE_ITEM)
    bpy.types.Scene.bone_renamer_active_template_index = IntProperty(default=0)
    bpy.types.Scene.bone_renamer_active_bone_index = IntProperty(default=0)
    bpy.types.Scene.bone_renamer_template_enum = EnumProperty(
        name="Template",
        items=get_template_enum_items,
        update=on_template_enum_change,
    )

    # Tunda loading template sampai Blender sepenuhnya siap
    def delayed_load_templates():
        for scene in bpy.data.scenes:
            load_templates_to_scene(scene)
        return None  # Return None agar timer tidak diulang

    bpy.app.timers.register(delayed_load_templates, first_interval=0.1)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Scene.bone_renamer_template_enum   # ✅ BARU
    del bpy.types.Scene.bone_renamer_active_bone_index
    del bpy.types.Scene.bone_renamer_active_template_index
    del bpy.types.Scene.bone_renamer_template_list

if __name__ == "__main__":
    register()