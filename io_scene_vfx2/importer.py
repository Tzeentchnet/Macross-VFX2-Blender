from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from .formats import (
    ModContainer,
    PlayStationVram,
    TmdModel,
    TmdObject,
    TmdPrimitive,
    VFX2FormatError,
    parse_mod,
    parse_texture_path,
    parse_tmd,
)


COLOR_ATTRIBUTE_NAME = "VFX2Color"


@dataclass(frozen=True)
class ImportSummary:
    model_count: int
    object_count: int
    face_count: int
    texture_sources: tuple[Path, ...]
    embedded_texture_count: int
    placeholder_material_count: int


def _transform_position(
    position: tuple[int, int, int], scale: float
) -> tuple[float, float, float]:
    x, y, z = position
    return x * scale, z * scale, -y * scale


def _transform_normal(normal: tuple[int, int, int]) -> tuple[float, float, float]:
    x, y, z = normal
    transformed = (float(x), float(z), float(-y))
    length = math.sqrt(sum(component * component for component in transformed))
    if length == 0.0:
        return 0.0, 0.0, 0.0
    return tuple(component / length for component in transformed)


def _texture_paths_in_directory(directory: Path) -> tuple[Path, ...]:
    tix_files = sorted(
        (path for path in directory.iterdir() if path.suffix.casefold() == ".tix"),
        key=lambda path: path.name.casefold(),
    )
    if tix_files:
        primary = max(
            tix_files,
            key=lambda path: (path.stat().st_size, path.name.casefold()),
        )
        return (primary,)

    return tuple(
        sorted(
            (
                path
                for path in directory.iterdir()
                if path.suffix.casefold() == ".tim"
            ),
            key=lambda path: path.name.casefold(),
        )
    )


def _resolve_texture_paths(
    model_path: Path, texture_path: str, auto_find_textures: bool
) -> tuple[Path, ...]:
    if texture_path.strip():
        explicit_path = Path(bpy.path.abspath(texture_path)).expanduser()
        if explicit_path.is_dir():
            paths = _texture_paths_in_directory(explicit_path)
            if not paths:
                raise VFX2FormatError(
                    f"texture directory contains no TIX or TIM files: {explicit_path}"
                )
            return paths
        if not explicit_path.is_file():
            raise VFX2FormatError(f"texture bank does not exist: {explicit_path}")
        return (explicit_path,)
    if auto_find_textures:
        return _texture_paths_in_directory(model_path.parent)
    return ()


def _material_key(primitive: TmdPrimitive) -> tuple[object, ...]:
    if primitive.textured:
        return (
            "textured",
            primitive.tpage,
            primitive.clut,
            primitive.semitransparent,
            primitive.double_sided,
        )
    return (
        "untextured",
        primitive.semitransparent,
        primitive.double_sided,
    )


def _corner_colors(
    primitive: TmdPrimitive,
) -> tuple[tuple[float, float, float, float], ...]:
    alpha = 0.5 if primitive.semitransparent and not primitive.textured else 1.0
    if primitive.textured and (primitive.raw_texture or not primitive.colors):
        return ((1.0, 1.0, 1.0, alpha),) * len(primitive.vertex_indices)

    divisor = 128.0 if primitive.textured else 255.0
    return tuple(
        (red / divisor, green / divisor, blue / divisor, alpha)
        for red, green, blue in primitive.colors
    )


class _MaterialFactory:
    def __init__(self, vram: PlayStationVram, prefix: str) -> None:
        self.vram = vram
        self.prefix = prefix
        self.materials: dict[tuple[object, ...], bpy.types.Material] = {}
        self.images: dict[
            tuple[int, int, bool], tuple[bpy.types.Image, bool]
        ] = {}
        self.placeholder_material_count = 0

    def _new_node_material(
        self, name: str, *, semitransparent: bool, double_sided: bool
    ) -> tuple[
        bpy.types.Material,
        bpy.types.ShaderNodeBsdfPrincipled,
        bpy.types.ShaderNodeVertexColor,
    ]:
        material = bpy.data.materials.new(name=name)
        material.use_nodes = True
        material.use_backface_culling = not double_sided
        if semitransparent:
            material.surface_render_method = "DITHERED"

        nodes = material.node_tree.nodes
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (520.0, 0.0)
        shader = nodes.new("ShaderNodeBsdfPrincipled")
        shader.location = (260.0, 0.0)
        shader.inputs["Roughness"].default_value = 0.65
        specular = shader.inputs.get("Specular IOR Level")
        if specular is not None:
            specular.default_value = 0.15
        vertex_color = nodes.new("ShaderNodeVertexColor")
        vertex_color.layer_name = COLOR_ATTRIBUTE_NAME
        vertex_color.location = (-520.0, -80.0)
        material.node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
        return material, shader, vertex_color

    def _texture_image(
        self, tpage: int, clut: int, semitransparent: bool
    ) -> tuple[bpy.types.Image, bool]:
        key = (tpage, clut, semitransparent)
        cached = self.images.get(key)
        if cached is not None:
            return cached

        decoded = self.vram.decode_texture_page(
            tpage, clut, semitransparent=semitransparent
        )
        name = f"{self.prefix}_TP{tpage:04X}_CL{clut:04X}"
        image = bpy.data.images.new(name, width=256, height=256, alpha=True)
        image.alpha_mode = "STRAIGHT"
        image.pixels.foreach_set(decoded.pixels)
        image.update()
        image.pack()
        placeholder = decoded.missing_pixels == 256 * 256
        self.images[key] = (image, placeholder)
        return image, placeholder

    def get(self, primitive: TmdPrimitive) -> bpy.types.Material:
        key = _material_key(primitive)
        cached = self.materials.get(key)
        if cached is not None:
            return cached

        if not primitive.textured:
            suffix = "Semi" if primitive.semitransparent else "Opaque"
            material, shader, vertex_color = self._new_node_material(
                f"{self.prefix}_Color_{suffix}",
                semitransparent=primitive.semitransparent,
                double_sided=primitive.double_sided,
            )
            material.node_tree.links.new(
                vertex_color.outputs["Color"], shader.inputs["Base Color"]
            )
            material.node_tree.links.new(
                vertex_color.outputs["Alpha"], shader.inputs["Alpha"]
            )
            self.materials[key] = material
            return material

        if primitive.tpage is None or primitive.clut is None:
            raise VFX2FormatError("textured primitive is missing TPage or CLUT data")
        image, placeholder = self._texture_image(
            primitive.tpage, primitive.clut, primitive.semitransparent
        )
        if placeholder:
            self.placeholder_material_count += 1

        material, shader, vertex_color = self._new_node_material(
            (
                f"{self.prefix}_TP{primitive.tpage:04X}_CL{primitive.clut:04X}"
                f"_{'Semi' if primitive.semitransparent else 'Opaque'}"
            ),
            semitransparent=primitive.semitransparent,
            double_sided=primitive.double_sided,
        )
        nodes = material.node_tree.nodes
        texture = nodes.new("ShaderNodeTexImage")
        texture.image = image
        texture.interpolation = "Closest"
        texture.extension = "REPEAT"
        texture.location = (-520.0, 140.0)
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.blend_type = "MULTIPLY"
        multiply.inputs["Fac"].default_value = 1.0
        multiply.location = (-60.0, 80.0)
        alpha_multiply = nodes.new("ShaderNodeMath")
        alpha_multiply.operation = "MULTIPLY"
        alpha_multiply.location = (-60.0, -140.0)

        material.node_tree.links.new(texture.outputs["Color"], multiply.inputs[1])
        material.node_tree.links.new(vertex_color.outputs["Color"], multiply.inputs[2])
        material.node_tree.links.new(multiply.outputs["Color"], shader.inputs["Base Color"])
        material.node_tree.links.new(
            texture.outputs["Alpha"], alpha_multiply.inputs[0]
        )
        material.node_tree.links.new(
            vertex_color.outputs["Alpha"], alpha_multiply.inputs[1]
        )
        material.node_tree.links.new(alpha_multiply.outputs[0], shader.inputs["Alpha"])
        self.materials[key] = material
        return material


@dataclass(frozen=True)
class _FaceData:
    primitive: TmdPrimitive
    uvs: tuple[tuple[float, float], ...]
    colors: tuple[tuple[float, float, float, float], ...]
    normals: tuple[tuple[float, float, float], ...]


def _mesh_corner_order(primitive: TmdPrimitive) -> tuple[int, ...]:
    if len(primitive.vertex_indices) == 4:
        # The PS1 GPU stores quads as a triangle strip, not perimeter order.
        return 2, 3, 1, 0
    return tuple(range(len(primitive.vertex_indices) - 1, -1, -1))


def _build_mesh_object(
    model_name: str,
    form_index: int,
    object_index: int,
    source: TmdObject,
    collection: bpy.types.Collection,
    material_factory: _MaterialFactory,
    scale: float,
) -> tuple[bpy.types.Object | None, int]:
    faces: list[tuple[int, ...]] = []
    face_data: list[_FaceData] = []
    material_keys: list[tuple[object, ...]] = []
    material_primitives: dict[tuple[object, ...], TmdPrimitive] = {}

    for primitive in source.primitives:
        if len(set(primitive.vertex_indices)) < 3:
            continue
        corner_order = _mesh_corner_order(primitive)
        faces.append(
            tuple(primitive.vertex_indices[index] for index in corner_order)
        )
        source_colors = _corner_colors(primitive)
        colors = tuple(source_colors[index] for index in corner_order)
        source_uvs = tuple(
            ((u + 0.5) / 256.0, 1.0 - (v + 0.5) / 256.0)
            for u, v in primitive.uvs
        )
        uvs = (
            tuple(source_uvs[index] for index in corner_order)
            if source_uvs
            else ()
        )
        source_normals = tuple(
            _transform_normal(source.normals[index])
            for index in primitive.normal_indices
        )
        normals = (
            tuple(source_normals[index] for index in corner_order)
            if source_normals
            else ()
        )
        face_data.append(_FaceData(primitive, uvs, colors, normals))
        key = _material_key(primitive)
        material_keys.append(key)
        material_primitives.setdefault(key, primitive)

    if not faces:
        return None, 0

    name = f"{model_name}_F{form_index + 1:02d}_O{object_index + 1:03d}"
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(
        [_transform_position(vertex, scale) for vertex in source.vertices],
        [],
        faces,
    )
    mesh.update()

    material_indices: dict[tuple[object, ...], int] = {}
    for key, primitive in material_primitives.items():
        material_indices[key] = len(mesh.materials)
        mesh.materials.append(material_factory.get(primitive))

    has_uvs = any(data.uvs for data in face_data)
    uv_layer = mesh.uv_layers.new(name="UVMap") if has_uvs else None
    color_attribute = mesh.color_attributes.new(
        name=COLOR_ATTRIBUTE_NAME, type="FLOAT_COLOR", domain="CORNER"
    )
    custom_normals: list[tuple[float, float, float]] = [
        (0.0, 0.0, 0.0)
    ] * len(mesh.loops)

    for polygon, data, material_key in zip(
        mesh.polygons, face_data, material_keys, strict=True
    ):
        polygon.material_index = material_indices[material_key]
        polygon.use_smooth = data.primitive.gouraud
        for corner, loop_index in enumerate(polygon.loop_indices):
            color_attribute.data[loop_index].color = data.colors[corner]
            if uv_layer is not None and data.uvs:
                uv_layer.data[loop_index].uv = data.uvs[corner]
            if data.normals:
                custom_normals[loop_index] = data.normals[corner]

    if any(normal != (0.0, 0.0, 0.0) for normal in custom_normals):
        mesh.normals_split_custom_set(custom_normals)

    obj = bpy.data.objects.new(name, mesh)
    obj["vfx2_object_index"] = object_index
    obj["vfx2_tmd_scale"] = source.scale
    collection.objects.link(obj)
    return obj, len(faces)


def import_vfx2(
    context: bpy.types.Context,
    model_path: Path,
    *,
    texture_path: str,
    auto_find_textures: bool,
    scale: float,
    show_all_forms: bool,
) -> ImportSummary:
    model_path = model_path.resolve()
    data = model_path.read_bytes()
    suffix = model_path.suffix.casefold()
    embedded_texture_count = 0

    if suffix == ".tmd":
        models = (parse_tmd(data, require_eof=True),)
        container: ModContainer | None = None
    elif suffix == ".mod":
        container = parse_mod(data)
        models = container.models
        embedded_texture_count = len(container.embedded_textures)
    else:
        raise VFX2FormatError(
            f"unsupported model extension {model_path.suffix or '<none>'}"
        )

    external_texture_paths = _resolve_texture_paths(
        model_path, texture_path, auto_find_textures
    )
    vram = PlayStationVram()
    for path in external_texture_paths:
        vram.apply_stream(parse_texture_path(path))
    if container is not None:
        vram.apply_streams(container.embedded_textures)

    root_collection = bpy.data.collections.new(model_path.stem)
    root_collection["vfx2_source"] = str(model_path)
    root_collection["vfx2_model_count"] = len(models)
    context.scene.collection.children.link(root_collection)
    material_factory = _MaterialFactory(vram, model_path.stem)

    imported_objects: list[bpy.types.Object] = []
    face_count = 0
    for form_index, model in enumerate(models):
        form_collection = bpy.data.collections.new(
            f"{model_path.stem}_Form_{form_index + 1:02d}"
        )
        form_collection["vfx2_tmd_offset"] = model.offset
        root_collection.children.link(form_collection)
        if form_index > 0 and not show_all_forms:
            form_collection.hide_viewport = True
            form_collection.hide_render = True

        for object_index, source_object in enumerate(model.objects):
            obj, object_faces = _build_mesh_object(
                model_path.stem,
                form_index,
                object_index,
                source_object,
                form_collection,
                material_factory,
                scale,
            )
            if obj is not None:
                imported_objects.append(obj)
                face_count += object_faces

    if not imported_objects:
        raise VFX2FormatError("model contains no non-degenerate polygon geometry")

    for selected in context.selected_objects:
        selected.select_set(False)
    visible_objects: list[bpy.types.Object] = []
    for obj in imported_objects:
        if not obj.users_collection[0].hide_viewport:
            obj.select_set(True)
            visible_objects.append(obj)
    context.view_layer.objects.active = (
        visible_objects[0] if visible_objects else imported_objects[0]
    )

    return ImportSummary(
        model_count=len(models),
        object_count=len(imported_objects),
        face_count=face_count,
        texture_sources=external_texture_paths,
        embedded_texture_count=embedded_texture_count,
        placeholder_material_count=material_factory.placeholder_material_count,
    )


class IMPORT_SCENE_OT_vfx2(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.vfx2"
    bl_label = "Import Macross VF-X2 Model"
    bl_options = {"PRESET", "UNDO"}

    filename_ext = ".tmd"
    filter_glob: StringProperty(
        default="*.tmd;*.TMD;*.mod;*.MOD",
        options={"HIDDEN"},
    )
    texture_path: StringProperty(
        name="Texture Bank",
        description=(
            "Optional TIX/TIM file or directory. External uploads are applied "
            "before any TIM textures embedded in a MOD"
        ),
        subtype="FILE_PATH",
        default="",
    )
    auto_find_textures: BoolProperty(
        name="Auto-find Textures",
        description=(
            "Use the largest TIX beside the model, or all adjacent TIM files "
            "when no texture bank is selected"
        ),
        default=True,
    )
    scale: FloatProperty(
        name="Scale",
        description="Blender units per PlayStation model unit",
        default=0.01,
        min=0.000001,
        soft_max=1.0,
        precision=5,
    )
    show_all_forms: BoolProperty(
        name="Show All MOD Forms",
        description=(
            "Leave every embedded TMD form visible instead of hiding alternatives "
            "after the first"
        ),
        default=False,
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            summary = import_vfx2(
                context,
                Path(self.filepath),
                texture_path=self.texture_path,
                auto_find_textures=self.auto_find_textures,
                scale=self.scale,
                show_all_forms=self.show_all_forms,
            )
        except (OSError, VFX2FormatError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        texture_description = (
            f"{summary.embedded_texture_count} embedded TIM"
            if summary.embedded_texture_count
            else "no embedded TIM"
        )
        if summary.texture_sources:
            texture_description += (
                f", {len(summary.texture_sources)} external texture source(s)"
            )
        self.report(
            {"INFO"},
            (
                f"Imported {summary.model_count} form(s), "
                f"{summary.object_count} object(s), {summary.face_count} face(s); "
                f"{texture_description}"
            ),
        )
        if summary.placeholder_material_count:
            self.report(
                {"WARNING"},
                (
                    f"{summary.placeholder_material_count} material(s) have no "
                    "matching VRAM data and use a magenta placeholder"
                ),
            )
        return {"FINISHED"}

    def draw(self, context: bpy.types.Context) -> None:
        del context
        layout = self.layout
        layout.prop(self, "texture_path")
        layout.prop(self, "auto_find_textures")
        layout.prop(self, "scale")
        layout.prop(self, "show_all_forms")


def _menu_import(self: bpy.types.Menu, context: bpy.types.Context) -> None:
    del context
    self.layout.operator(
        IMPORT_SCENE_OT_vfx2.bl_idname,
        text="Macross VF-X2 Model (.tmd/.mod)",
    )


_CLASSES = (IMPORT_SCENE_OT_vfx2,)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
