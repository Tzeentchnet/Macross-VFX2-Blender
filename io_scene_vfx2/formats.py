from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Iterable


TMD_MAGIC = 0x00000041
MOD_MAGIC = 0x11E1A300
TIM_MAGIC = 0x00000010
VRAM_WIDTH = 1024
VRAM_HEIGHT = 512
TEXTURE_PAGE_SIZE = 256


class VFX2FormatError(ValueError):
    pass


@dataclass(frozen=True)
class TextureUpload:
    offset: int
    x: int
    y: int
    width: int
    height: int
    words: tuple[int, ...]


@dataclass(frozen=True)
class TextureStream:
    offset: int
    end_offset: int
    format_name: str
    flags: int
    uploads: tuple[TextureUpload, ...]


@dataclass(frozen=True)
class TmdPrimitive:
    mode: int
    flag: int
    vertex_indices: tuple[int, ...]
    normal_indices: tuple[int, ...]
    uvs: tuple[tuple[int, int], ...]
    colors: tuple[tuple[int, int, int], ...]
    clut: int | None
    tpage: int | None

    @property
    def textured(self) -> bool:
        return bool(self.mode & 0x04)

    @property
    def semitransparent(self) -> bool:
        return bool(self.mode & 0x02)

    @property
    def raw_texture(self) -> bool:
        return bool(self.mode & 0x01)

    @property
    def gouraud(self) -> bool:
        return bool(self.mode & 0x10)

    @property
    def double_sided(self) -> bool:
        return bool(self.flag & 0x02)


@dataclass(frozen=True)
class TmdObject:
    vertices: tuple[tuple[int, int, int], ...]
    normals: tuple[tuple[int, int, int], ...]
    primitives: tuple[TmdPrimitive, ...]
    scale: int


@dataclass(frozen=True)
class TmdModel:
    offset: int
    end_offset: int
    objects: tuple[TmdObject, ...]


@dataclass(frozen=True)
class ModContainer:
    declared_model_count: int
    models: tuple[TmdModel, ...]
    embedded_textures: tuple[TextureStream, ...]


@dataclass(frozen=True)
class DecodedTexture:
    pixels: array
    missing_pixels: int
    has_transparency: bool


def _require(data: bytes, offset: int, size: int, description: str) -> None:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise VFX2FormatError(
            f"{description} at 0x{offset:X} exceeds the {len(data)}-byte input"
        )


def _unpack_from(
    format_string: str, data: bytes, offset: int, description: str
) -> tuple[int, ...]:
    size = struct.calcsize(format_string)
    _require(data, offset, size, description)
    return struct.unpack_from(format_string, data, offset)


def _parse_upload(data: bytes, offset: int) -> tuple[TextureUpload, int]:
    length, x, y, width, height = _unpack_from(
        "<I4H", data, offset, "texture upload header"
    )
    expected_length = 12 + width * height * 2
    if length != expected_length:
        raise VFX2FormatError(
            f"texture upload at 0x{offset:X} has length {length}, "
            f"expected {expected_length} for {width}x{height} words"
        )
    if width == 0 or height == 0:
        raise VFX2FormatError(f"texture upload at 0x{offset:X} has an empty rectangle")
    if x + width > VRAM_WIDTH or y + height > VRAM_HEIGHT:
        raise VFX2FormatError(
            f"texture upload at 0x{offset:X} exceeds PS1 VRAM: "
            f"({x}, {y}) {width}x{height}"
        )

    end_offset = offset + length
    _require(data, offset, length, "texture upload")
    words = tuple(
        value[0]
        for value in struct.iter_unpack("<H", data[offset + 12 : end_offset])
    )
    return TextureUpload(offset, x, y, width, height, words), end_offset


def parse_tim(
    data: bytes, offset: int = 0, *, require_eof: bool = True
) -> TextureStream:
    magic, flags = _unpack_from("<2I", data, offset, "TIM header")
    if magic != TIM_MAGIC:
        raise VFX2FormatError(
            f"expected TIM magic 0x{TIM_MAGIC:08X} at 0x{offset:X}, got 0x{magic:08X}"
        )
    if flags & ~0x0B or (flags & 0x07) > 3:
        raise VFX2FormatError(f"unsupported TIM flags 0x{flags:08X} at 0x{offset:X}")

    cursor = offset + 8
    upload_count = 2 if flags & 0x08 else 1
    uploads: list[TextureUpload] = []
    for _ in range(upload_count):
        upload, cursor = _parse_upload(data, cursor)
        uploads.append(upload)

    if require_eof and cursor != len(data):
        raise VFX2FormatError(
            f"TIM ends at 0x{cursor:X}, leaving {len(data) - cursor} trailing bytes"
        )
    return TextureStream(offset, cursor, "TIM", flags, tuple(uploads))


def parse_tix(data: bytes) -> TextureStream:
    magic, mode, rectangle_count = _unpack_from("<I2H", data, 0, "TIX header")
    if magic != TIM_MAGIC:
        raise VFX2FormatError(
            f"expected TIX magic 0x{TIM_MAGIC:08X}, got 0x{magic:08X}"
        )

    if mode == 2 and rectangle_count == 0:
        upload_count = 1
    elif mode == 4 and rectangle_count > 0:
        upload_count = rectangle_count
    else:
        raise VFX2FormatError(
            f"unsupported TIX mode/count combination {mode}/{rectangle_count}"
        )

    cursor = 8
    uploads: list[TextureUpload] = []
    for _ in range(upload_count):
        upload, cursor = _parse_upload(data, cursor)
        uploads.append(upload)
    if cursor != len(data):
        raise VFX2FormatError(
            f"TIX ends at 0x{cursor:X}, leaving {len(data) - cursor} trailing bytes"
        )

    flags = mode | (rectangle_count << 16)
    return TextureStream(0, cursor, "TIX", flags, tuple(uploads))


def parse_texture_path(path: Path) -> TextureStream:
    data = path.read_bytes()
    suffix = path.suffix.casefold()
    if suffix == ".tix":
        return parse_tix(data)
    if suffix == ".tim":
        return parse_tim(data)
    raise VFX2FormatError(f"unsupported texture extension {path.suffix or '<none>'}")


def find_embedded_tim_streams(data: bytes) -> tuple[TextureStream, ...]:
    signature = struct.pack("<I", TIM_MAGIC)
    streams: list[TextureStream] = []
    cursor = 0
    while True:
        offset = data.find(signature, cursor)
        if offset < 0:
            break
        try:
            stream = parse_tim(data, offset, require_eof=False)
        except VFX2FormatError:
            cursor = offset + 1
            continue
        streams.append(stream)
        cursor = stream.end_offset
    return tuple(streams)


def _read_color(
    packet: bytes, cursor: int, packet_offset: int
) -> tuple[tuple[int, int, int], int]:
    red, green, blue, _ = _unpack_from(
        "<4B", packet, cursor, f"primitive color from 0x{packet_offset:X}"
    )
    return (red, green, blue), cursor + 4


def _read_u16(
    packet: bytes, cursor: int, packet_offset: int, description: str
) -> tuple[int, int]:
    (value,) = _unpack_from(
        "<H", packet, cursor, f"{description} from primitive at 0x{packet_offset:X}"
    )
    return value, cursor + 2


def _parse_polygon(
    packet: bytes, packet_offset: int, mode: int, flag: int
) -> TmdPrimitive:
    if mode & 0xE0 != 0x20:
        raise VFX2FormatError(
            f"unsupported non-polygon TMD primitive mode 0x{mode:02X} "
            f"at 0x{packet_offset - 4:X}"
        )

    vertex_count = 4 if mode & 0x08 else 3
    textured = bool(mode & 0x04)
    gouraud = bool(mode & 0x10)
    no_lighting = bool(flag & 0x01)
    gradient = bool(flag & 0x04) or (no_lighting and gouraud)
    cursor = 0
    uvs: list[tuple[int, int]] = []
    clut: int | None = None
    tpage: int | None = None

    if textured:
        for index in range(vertex_count):
            u, v, attribute = _unpack_from(
                "<2BH",
                packet,
                cursor,
                f"texture coordinates from primitive at 0x{packet_offset - 4:X}",
            )
            uvs.append((u, v))
            if index == 0:
                clut = attribute
            elif index == 1:
                tpage = attribute
            cursor += 4

    colors: list[tuple[int, int, int]] = []
    has_color = not textured or no_lighting
    if has_color:
        color_count = vertex_count if gradient else 1
        for _ in range(color_count):
            color, cursor = _read_color(packet, cursor, packet_offset)
            colors.append(color)
        if color_count == 1:
            colors *= vertex_count

    vertices: list[int] = []
    normals: list[int] = []
    if no_lighting:
        for _ in range(vertex_count):
            vertex, cursor = _read_u16(
                packet, cursor, packet_offset, "vertex index"
            )
            vertices.append(vertex)
        cursor = (cursor + 3) & ~3
    elif gouraud:
        for _ in range(vertex_count):
            normal, cursor = _read_u16(
                packet, cursor, packet_offset, "normal index"
            )
            vertex, cursor = _read_u16(
                packet, cursor, packet_offset, "vertex index"
            )
            normals.append(normal)
            vertices.append(vertex)
    else:
        normal, cursor = _read_u16(packet, cursor, packet_offset, "normal index")
        normals = [normal] * vertex_count
        for _ in range(vertex_count):
            vertex, cursor = _read_u16(
                packet, cursor, packet_offset, "vertex index"
            )
            vertices.append(vertex)
        cursor = (cursor + 3) & ~3

    if cursor != len(packet):
        raise VFX2FormatError(
            f"primitive mode 0x{mode:02X}/flag 0x{flag:02X} at "
            f"0x{packet_offset - 4:X} consumes {cursor} bytes, "
            f"but ilen supplies {len(packet)}"
        )

    return TmdPrimitive(
        mode=mode,
        flag=flag,
        vertex_indices=tuple(vertices),
        normal_indices=tuple(normals),
        uvs=tuple(uvs),
        colors=tuple(colors),
        clut=clut,
        tpage=tpage,
    )


def parse_tmd(
    data: bytes, offset: int = 0, *, require_eof: bool = False
) -> TmdModel:
    magic, flags, object_count = _unpack_from("<3I", data, offset, "TMD header")
    if magic != TMD_MAGIC:
        raise VFX2FormatError(
            f"expected TMD magic 0x{TMD_MAGIC:08X} at 0x{offset:X}, "
            f"got 0x{magic:08X}"
        )
    if flags != 0:
        raise VFX2FormatError(
            f"TMD at 0x{offset:X} uses unsupported absolute-address flag {flags}"
        )
    if not 1 <= object_count <= 4096:
        raise VFX2FormatError(
            f"TMD at 0x{offset:X} has unreasonable object count {object_count}"
        )

    base = offset + 12
    _require(data, base, object_count * 28, "TMD object table")
    model_end = base + object_count * 28
    objects: list[TmdObject] = []

    for object_index in range(object_count):
        descriptor_offset = base + object_index * 28
        (
            vertex_offset,
            vertex_count,
            normal_offset,
            normal_count,
            primitive_offset,
            primitive_count,
            scale,
        ) = _unpack_from(
            "<6Ii", data, descriptor_offset, f"TMD object {object_index} descriptor"
        )
        if vertex_count > 1_000_000 or normal_count > 1_000_000:
            raise VFX2FormatError(
                f"TMD object {object_index} has unreasonable vertex/normal counts"
            )
        if primitive_count > 1_000_000:
            raise VFX2FormatError(
                f"TMD object {object_index} has unreasonable primitive count"
            )

        vertex_start = base + vertex_offset
        normal_start = base + normal_offset
        primitive_start = base + primitive_offset
        _require(data, vertex_start, vertex_count * 8, f"object {object_index} vertices")
        _require(data, normal_start, normal_count * 8, f"object {object_index} normals")
        if primitive_count:
            _require(data, primitive_start, 4, f"object {object_index} primitives")

        vertices = tuple(
            struct.unpack_from("<3h", data, vertex_start + index * 8)
            for index in range(vertex_count)
        )
        normals = tuple(
            struct.unpack_from("<3h", data, normal_start + index * 8)
            for index in range(normal_count)
        )
        model_end = max(
            model_end,
            vertex_start + vertex_count * 8,
            normal_start + normal_count * 8,
        )

        primitives: list[TmdPrimitive] = []
        cursor = primitive_start
        for primitive_index in range(primitive_count):
            olen, ilen, flag, mode = _unpack_from(
                "<4B",
                data,
                cursor,
                f"object {object_index} primitive {primitive_index} header",
            )
            del olen
            if not 1 <= ilen <= 64:
                raise VFX2FormatError(
                    f"primitive at 0x{cursor:X} has invalid ilen {ilen}"
                )
            packet_offset = cursor + 4
            packet_size = ilen * 4
            _require(data, packet_offset, packet_size, "TMD primitive packet")
            primitive = _parse_polygon(
                data[packet_offset : packet_offset + packet_size],
                packet_offset,
                mode,
                flag,
            )
            if any(index >= vertex_count for index in primitive.vertex_indices):
                raise VFX2FormatError(
                    f"primitive at 0x{cursor:X} references a vertex outside "
                    f"object {object_index}'s {vertex_count} vertices"
                )
            if any(index >= normal_count for index in primitive.normal_indices):
                raise VFX2FormatError(
                    f"primitive at 0x{cursor:X} references a normal outside "
                    f"object {object_index}'s {normal_count} normals"
                )
            primitives.append(primitive)
            cursor = packet_offset + packet_size

        model_end = max(model_end, cursor)
        objects.append(TmdObject(vertices, normals, tuple(primitives), scale))

    if require_eof and model_end != len(data):
        raise VFX2FormatError(
            f"TMD data ends at 0x{model_end:X}, leaving "
            f"{len(data) - model_end} trailing bytes"
        )
    return TmdModel(offset, model_end, tuple(objects))


def find_tmd_models(data: bytes) -> tuple[TmdModel, ...]:
    signature = struct.pack("<2I", TMD_MAGIC, 0)
    models: list[TmdModel] = []
    cursor = 0
    while True:
        offset = data.find(signature, cursor)
        if offset < 0:
            break
        try:
            model = parse_tmd(data, offset)
        except VFX2FormatError:
            cursor = offset + 1
            continue
        models.append(model)
        cursor = offset + 1
    return tuple(models)


def parse_mod(data: bytes) -> ModContainer:
    magic, declared_model_count = _unpack_from("<2I", data, 0, "MOD header")
    if magic != MOD_MAGIC:
        raise VFX2FormatError(
            f"expected VFX2 MOD magic 0x{MOD_MAGIC:08X}, got 0x{magic:08X}"
        )
    if not 1 <= declared_model_count <= 256:
        raise VFX2FormatError(
            f"MOD has unreasonable declared model count {declared_model_count}"
        )

    models = find_tmd_models(data)
    if not models:
        raise VFX2FormatError("MOD contains no valid embedded TMD models")
    return ModContainer(
        declared_model_count=declared_model_count,
        models=models,
        embedded_textures=find_embedded_tim_streams(data),
    )


class PlayStationVram:
    def __init__(self) -> None:
        self.words = array("H", [0]) * (VRAM_WIDTH * VRAM_HEIGHT)
        self.initialized = bytearray(VRAM_WIDTH * VRAM_HEIGHT)

    def apply_upload(self, upload: TextureUpload) -> None:
        for row in range(upload.height):
            source_start = row * upload.width
            source_end = source_start + upload.width
            target_start = (upload.y + row) * VRAM_WIDTH + upload.x
            target_end = target_start + upload.width
            self.words[target_start:target_end] = array(
                "H", upload.words[source_start:source_end]
            )
            self.initialized[target_start:target_end] = b"\x01" * upload.width

    def apply_stream(self, stream: TextureStream) -> None:
        for upload in stream.uploads:
            self.apply_upload(upload)

    def apply_streams(self, streams: Iterable[TextureStream]) -> None:
        for stream in streams:
            self.apply_stream(stream)

    def _word(self, x: int, y: int) -> tuple[int, bool]:
        if not (0 <= x < VRAM_WIDTH and 0 <= y < VRAM_HEIGHT):
            return 0, False
        index = y * VRAM_WIDTH + x
        return self.words[index], bool(self.initialized[index])

    def decode_texture_page(
        self, tpage: int, clut: int, *, semitransparent: bool
    ) -> DecodedTexture:
        depth = (tpage >> 7) & 0x03
        page_x = (tpage & 0x0F) * 64
        page_y = ((tpage >> 4) & 0x01) * 256
        clut_x = (clut & 0x3F) * 16
        clut_y = (clut >> 6) & 0x1FF
        pixels = array("f", [0.0]) * (
            TEXTURE_PAGE_SIZE * TEXTURE_PAGE_SIZE * 4
        )
        missing_pixels = 0
        has_transparency = False

        for v in range(TEXTURE_PAGE_SIZE):
            destination_y = TEXTURE_PAGE_SIZE - 1 - v
            for u in range(TEXTURE_PAGE_SIZE):
                color_word = 0
                initialized = True
                if depth == 0:
                    packed, initialized = self._word(page_x + (u >> 2), page_y + v)
                    palette_index = (packed >> ((u & 0x03) * 4)) & 0x0F
                    color_word, palette_initialized = self._word(
                        clut_x + palette_index, clut_y
                    )
                    initialized = initialized and palette_initialized
                elif depth == 1:
                    packed, initialized = self._word(page_x + (u >> 1), page_y + v)
                    palette_index = (packed >> ((u & 0x01) * 8)) & 0xFF
                    color_word, palette_initialized = self._word(
                        clut_x + palette_index, clut_y
                    )
                    initialized = initialized and palette_initialized
                elif depth == 2:
                    color_word, initialized = self._word(page_x + u, page_y + v)
                else:
                    initialized = False

                destination = (
                    (destination_y * TEXTURE_PAGE_SIZE + u) * 4
                )
                if not initialized:
                    missing_pixels += 1
                    checker = ((u >> 4) ^ (v >> 4)) & 1
                    pixels[destination : destination + 4] = array(
                        "f", (1.0, 0.0 if checker else 0.2, 1.0, 1.0)
                    )
                    continue

                red = color_word & 0x1F
                green = (color_word >> 5) & 0x1F
                blue = (color_word >> 10) & 0x1F
                alpha = 1.0
                if color_word == 0:
                    alpha = 0.0
                elif semitransparent and color_word & 0x8000:
                    alpha = 0.5
                if alpha < 1.0:
                    has_transparency = True

                pixels[destination : destination + 4] = array(
                    "f",
                    (
                        ((red << 3) | (red >> 2)) / 255.0,
                        ((green << 3) | (green >> 2)) / 255.0,
                        ((blue << 3) | (blue >> 2)) / 255.0,
                        alpha,
                    ),
                )

        return DecodedTexture(pixels, missing_pixels, has_transparency)
