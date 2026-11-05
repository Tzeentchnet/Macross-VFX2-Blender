from __future__ import annotations

import argparse
from dataclasses import dataclass
import mmap
from pathlib import Path
import struct
import sys
import tempfile

import bpy
from mathutils import Vector


SECTOR_SIZE = 2352
USER_DATA_OFFSET = 24
USER_DATA_SIZE = 2048


@dataclass(frozen=True)
class Sample:
    name: str
    model_path: str
    texture_path: str | None = None
    form_index: int = 0
    view_direction: tuple[float, float, float] = (1.45, -1.7, 0.95)


SAMPLES = (
    Sample("vf11-mod", "MOD/VF11.MOD", view_direction=(1.5, -1.7, 0.8)),
    Sample(
        "ally010-tmd",
        "DATA01/ALLY010.TMD",
        "DATA01/IMAGE010.TIX",
        view_direction=(1.35, -1.6, 0.75),
    ),
    Sample(
        "enemy042-tmd",
        "DATA04/ENEMY042.TMD",
        "DATA04/IMAGE040.TIX",
        view_direction=(1.35, -1.55, 0.8),
    ),
    Sample(
        "enemy043-tmd",
        "DATA04/ENEMY043.TMD",
        "DATA04/IMAGE040.TIX",
        view_direction=(1.4, -1.6, 0.8),
    ),
)


class DiscFormatError(RuntimeError):
    pass


class Mode2Iso:
    def __init__(self, image: mmap.mmap) -> None:
        self.image = image
        self.files = self._file_extents()

    def _user_sector(self, lba: int) -> bytes:
        start = lba * SECTOR_SIZE + USER_DATA_OFFSET
        return self.image[start : start + USER_DATA_SIZE]

    def _user_range(self, lba: int, size: int) -> bytes:
        count = (size + USER_DATA_SIZE - 1) // USER_DATA_SIZE
        return b"".join(
            self._user_sector(lba + index) for index in range(count)
        )[:size]

    def _file_extents(self) -> dict[str, tuple[int, int]]:
        pvd = self._user_sector(16)
        if pvd[0] != 1 or pvd[1:6] != b"CD001" or pvd[6] != 1:
            raise DiscFormatError("Track 1 does not contain an ISO 9660 PVD")

        root_size = pvd[156]
        root = pvd[156 : 156 + root_size]
        root_lba = struct.unpack_from("<I", root, 2)[0]
        root_length = struct.unpack_from("<I", root, 10)[0]
        files: dict[str, tuple[int, int]] = {}
        visited: set[tuple[int, int]] = set()

        def walk(path: str, lba: int, size: int) -> None:
            key = (lba, size)
            if key in visited:
                return
            visited.add(key)
            directory = self._user_range(lba, size)
            offset = 0
            while offset < len(directory):
                record_size = directory[offset]
                if record_size == 0:
                    offset = (
                        (offset // USER_DATA_SIZE) + 1
                    ) * USER_DATA_SIZE
                    continue
                record = directory[offset : offset + record_size]
                offset += record_size
                if len(record) < 34:
                    raise DiscFormatError(
                        f"invalid ISO directory record under {path or '/'}"
                    )

                extent = struct.unpack_from("<I", record, 2)[0]
                length = struct.unpack_from("<I", record, 10)[0]
                flags = record[25]
                name_size = record[32]
                raw_name = bytes(record[33 : 33 + name_size])
                if raw_name in {b"\x00", b"\x01"}:
                    continue
                try:
                    name = raw_name.decode("ascii")
                except UnicodeDecodeError:
                    name = raw_name.decode("shift_jis")
                name = name.split(";", 1)[0]
                full_name = f"{path}/{name}" if path else name
                if flags & 2:
                    walk(full_name, extent, length)
                else:
                    files[full_name] = (extent, length)

        walk("", root_lba, root_length)
        return files

    def extract(self, disc_path: str, destination: Path) -> Path:
        try:
            lba, size = self.files[disc_path]
        except KeyError as error:
            raise DiscFormatError(f"asset is absent from the disc: {disc_path}") from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._user_range(lba, size))
        return destination


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render documentation samples from a legal VF-X2 Track 1 image"
    )
    parser.add_argument(
        "--disc-image",
        type=Path,
        required=True,
        help="Raw MODE2/2352 Track 1 BIN",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for rendered PNG files",
    )
    parser.add_argument(
        "--sample",
        action="append",
        choices=[sample.name for sample in SAMPLES],
        help="Render only the named sample; repeat for more than one",
    )
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    return parser.parse_args(arguments)


def _look_at(obj: bpy.types.Object, target: Vector) -> None:
    obj.rotation_euler = (target - obj.location).to_track_quat("-Z", "Y").to_euler()


def _add_area_light(
    name: str,
    location: Vector,
    target: Vector,
    energy: float,
    size: float,
    color: tuple[float, float, float],
) -> None:
    light_data = bpy.data.lights.new(name=name, type="AREA")
    light_data.energy = energy
    light_data.shape = "DISK"
    light_data.size = size
    light_data.color = color
    light = bpy.data.objects.new(name, light_data)
    bpy.context.scene.collection.objects.link(light)
    light.location = location
    _look_at(light, target)


def _visible_form(
    root_collection: bpy.types.Collection, form_index: int
) -> bpy.types.Collection:
    forms = sorted(root_collection.children, key=lambda collection: collection.name)
    if not 0 <= form_index < len(forms):
        raise RuntimeError(
            f"{root_collection.name} has {len(forms)} forms, not form {form_index + 1}"
        )
    for index, collection in enumerate(forms):
        visible = index == form_index
        collection.hide_render = not visible
        collection.hide_viewport = not visible
    return forms[form_index]


def _bounds(objects: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    corners = [
        obj.matrix_world @ Vector(corner)
        for obj in objects
        if obj.type == "MESH"
        for corner in obj.bound_box
    ]
    if not corners:
        raise RuntimeError("import produced no visible mesh bounds")
    minimum = Vector(
        tuple(min(corner[axis] for corner in corners) for axis in range(3))
    )
    maximum = Vector(
        tuple(max(corner[axis] for corner in corners) for axis in range(3))
    )
    return minimum, maximum


def _studio_scene(
    objects: list[bpy.types.Object],
    view_direction: tuple[float, float, float],
    output_path: Path,
) -> None:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = 1280
    scene.render.resolution_y = 720
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 20
    scene.render.film_transparent = False
    scene.render.filepath = str(output_path)

    world = bpy.data.worlds.new("Sample World")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (
        0.006,
        0.012,
        0.025,
        1.0,
    )
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.5
    scene.world = world

    minimum, maximum = _bounds(objects)
    center = (minimum + maximum) * 0.5
    dimensions = maximum - minimum
    radius = max(dimensions.length * 0.5, 0.25)
    target = center + Vector((0.0, 0.0, dimensions.z * 0.04))

    camera_data = bpy.data.cameras.new("Sample Camera")
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = radius * 2.3
    camera_data.lens = 55.0
    camera = bpy.data.objects.new("Sample Camera", camera_data)
    scene.collection.objects.link(camera)
    direction = Vector(view_direction).normalized()
    camera.location = target + direction * radius * 4.0
    _look_at(camera, target)
    scene.camera = camera

    energy_scale = max(radius, 0.5) ** 2
    _add_area_light(
        "Key",
        target + Vector((2.3, -2.0, 3.0)) * radius,
        target,
        1150.0 * energy_scale,
        radius * 2.2,
        (1.0, 0.86, 0.72),
    )
    _add_area_light(
        "Fill",
        target + Vector((-2.5, -0.5, 1.3)) * radius,
        target,
        650.0 * energy_scale,
        radius * 2.8,
        (0.45, 0.65, 1.0),
    )
    _add_area_light(
        "Rim",
        target + Vector((0.2, 2.8, 2.4)) * radius,
        target,
        900.0 * energy_scale,
        radius * 2.0,
        (0.55, 0.75, 1.0),
    )

    bpy.ops.mesh.primitive_plane_add(
        size=radius * 10.0,
        location=(center.x, center.y, minimum.z - radius * 0.04),
    )
    ground = bpy.context.object
    ground.name = "Studio Ground"
    ground_material = bpy.data.materials.new("Studio Ground")
    ground_material.diffuse_color = (0.012, 0.022, 0.045, 1.0)
    ground_material.use_nodes = True
    shader = ground_material.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = (0.012, 0.022, 0.045, 1.0)
    shader.inputs["Roughness"].default_value = 0.7
    ground.data.materials.append(ground_material)

    scene.view_settings.look = "AgX - Medium High Contrast"
    bpy.ops.render.render(write_still=True)


def _render_sample(
    sample: Sample,
    iso: Mode2Iso,
    temporary_directory: Path,
    output_directory: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repository_root))
    from io_scene_vfx2.importer import import_vfx2

    bpy.ops.wm.read_factory_settings(use_empty=True)
    sample_directory = temporary_directory / sample.name
    model_path = iso.extract(
        sample.model_path, sample_directory / Path(sample.model_path).name
    )
    texture_path = ""
    if sample.texture_path is not None:
        texture_path = str(
            iso.extract(
                sample.texture_path,
                sample_directory / Path(sample.texture_path).name,
            )
        )

    summary = import_vfx2(
        bpy.context,
        model_path,
        texture_path=texture_path,
        auto_find_textures=False,
        scale=0.01,
        show_all_forms=True,
    )
    root_collection = bpy.data.collections[model_path.stem]
    form_collection = _visible_form(root_collection, sample.form_index)
    objects = list(form_collection.objects)
    output_path = output_directory / f"{sample.name}.png"
    _studio_scene(objects, sample.view_direction, output_path)
    print(
        f"Rendered {sample.name}: {summary.object_count} imported objects, "
        f"{summary.face_count} faces -> {output_path}"
    )


def main() -> None:
    arguments = _arguments()
    disc_image = arguments.disc_image.expanduser().resolve()
    output_directory = arguments.output_dir.expanduser().resolve()
    if not disc_image.is_file():
        raise FileNotFoundError(f"disc image does not exist: {disc_image}")
    output_directory.mkdir(parents=True, exist_ok=True)
    selected_names = set(arguments.sample or ())
    samples = tuple(
        sample
        for sample in SAMPLES
        if not selected_names or sample.name in selected_names
    )

    with (
        disc_image.open("rb") as stream,
        mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as image,
        tempfile.TemporaryDirectory(prefix="vfx2-render-") as temporary,
    ):
        iso = Mode2Iso(image)
        temporary_directory = Path(temporary)
        for sample in samples:
            _render_sample(
                sample, iso, temporary_directory, output_directory
            )


if __name__ == "__main__":
    main()
