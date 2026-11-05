from __future__ import annotations

import struct
import unittest

from io_scene_vfx2.formats import (
    MOD_MAGIC,
    PlayStationVram,
    VFX2FormatError,
    parse_mod,
    parse_tim,
    parse_tix,
    parse_tmd,
)


def _upload(
    x: int, y: int, width: int, height: int, words: tuple[int, ...]
) -> bytes:
    assert len(words) == width * height
    length = 12 + len(words) * 2
    return struct.pack("<I4H", length, x, y, width, height) + struct.pack(
        f"<{len(words)}H", *words
    )


def _texture_blocks() -> tuple[bytes, bytes]:
    palette = [0] * 16
    palette[1] = 0x001F
    palette[2] = 0x03E0
    clut = _upload(0, 1, 16, 1, tuple(palette))
    image = _upload(64, 0, 1, 1, (0x0021,))
    return clut, image


def _textured_triangle_tmd() -> bytes:
    primitive_packet = b"".join(
        (
            struct.pack("<2BH", 0, 0, 1 << 6),
            struct.pack("<2BH", 3, 0, 1),
            struct.pack("<2BH", 0, 3, 0),
            struct.pack("<4B", 128, 128, 128, 0),
            struct.pack("<4H", 0, 1, 2, 0),
        )
    )
    primitive = struct.pack("<4B", 7, 6, 1, 0x25) + primitive_packet
    vertices = b"".join(
        (
            struct.pack("<4h", 0, 0, 0, 0),
            struct.pack("<4h", 100, 0, 0, 0),
            struct.pack("<4h", 0, 100, 0, 0),
        )
    )
    header = struct.pack("<3I", 0x41, 0, 1)
    descriptor = struct.pack(
        "<6Ii",
        28 + len(primitive),
        3,
        28 + len(primitive) + len(vertices),
        0,
        28,
        1,
        0,
    )
    return header + descriptor + primitive + vertices


class TextureFormatTests(unittest.TestCase):
    def test_tix_upload_stream_and_4bpp_decode(self) -> None:
        clut, image = _texture_blocks()
        stream = parse_tix(struct.pack("<I2H", 0x10, 4, 2) + clut + image)

        self.assertEqual(len(stream.uploads), 2)
        vram = PlayStationVram()
        vram.apply_stream(stream)
        decoded = vram.decode_texture_page(1, 1 << 6, semitransparent=False)

        top_left = (255 * 256) * 4
        top_second = top_left + 4
        self.assertEqual(tuple(decoded.pixels[top_left : top_left + 4]), (1, 0, 0, 1))
        self.assertEqual(
            tuple(decoded.pixels[top_second : top_second + 4]), (0, 1, 0, 1)
        )

    def test_standard_tim_uses_same_upload_rectangles(self) -> None:
        clut, image = _texture_blocks()
        stream = parse_tim(struct.pack("<2I", 0x10, 8) + clut + image)

        self.assertEqual(stream.flags, 8)
        self.assertEqual(len(stream.uploads), 2)
        self.assertEqual(stream.uploads[1].x, 64)

    def test_tix_rejects_bad_rectangle_length(self) -> None:
        invalid = struct.pack("<I2H", 0x10, 4, 1) + struct.pack(
            "<I4H", 12, 0, 0, 1, 1
        )
        with self.assertRaisesRegex(VFX2FormatError, "expected 14"):
            parse_tix(invalid)


class ModelFormatTests(unittest.TestCase):
    def test_textured_triangle_tmd(self) -> None:
        model = parse_tmd(_textured_triangle_tmd(), require_eof=True)

        self.assertEqual(len(model.objects), 1)
        primitive = model.objects[0].primitives[0]
        self.assertEqual(primitive.vertex_indices, (0, 1, 2))
        self.assertEqual(primitive.uvs, ((0, 0), (3, 0), (0, 3)))
        self.assertEqual(primitive.clut, 1 << 6)
        self.assertEqual(primitive.tpage, 1)
        self.assertEqual(primitive.colors, ((128, 128, 128),) * 3)

    def test_mod_finds_embedded_tmd_and_tim(self) -> None:
        clut, image = _texture_blocks()
        tim = struct.pack("<2I", 0x10, 8) + clut + image
        mod = (
            struct.pack("<2I", MOD_MAGIC, 1)
            + b"\x00" * 24
            + _textured_triangle_tmd()
            + tim
        )

        container = parse_mod(mod)
        self.assertEqual(container.declared_model_count, 1)
        self.assertEqual(len(container.models), 1)
        self.assertEqual(len(container.embedded_textures), 1)

    def test_tmd_rejects_out_of_range_vertex(self) -> None:
        data = bytearray(_textured_triangle_tmd())
        primitive_packet_offset = 12 + 28 + 4
        struct.pack_into("<H", data, primitive_packet_offset + 16, 99)

        with self.assertRaisesRegex(VFX2FormatError, "outside"):
            parse_tmd(bytes(data))


if __name__ == "__main__":
    unittest.main()
