from __future__ import annotations

import unittest

try:
    import bpy  # type: ignore[import-not-found]  # noqa: F401
except ModuleNotFoundError:
    bpy = None

from io_scene_vfx2.formats import TmdPrimitive


@unittest.skipIf(bpy is None, "requires Blender's Python runtime")
class MeshCornerOrderTests(unittest.TestCase):
    def _primitive(self, vertex_count: int) -> TmdPrimitive:
        return TmdPrimitive(
            mode=0x28 if vertex_count == 4 else 0x20,
            flag=0,
            vertex_indices=tuple(range(vertex_count)),
            normal_indices=(),
            uvs=(),
            colors=((255, 255, 255),) * vertex_count,
            clut=None,
            tpage=None,
        )

    def test_triangle_reverses_playstation_winding(self) -> None:
        from io_scene_vfx2.importer import _mesh_corner_order

        self.assertEqual(_mesh_corner_order(self._primitive(3)), (2, 1, 0))

    def test_quad_converts_gpu_strip_to_mesh_perimeter(self) -> None:
        from io_scene_vfx2.importer import _mesh_corner_order

        self.assertEqual(_mesh_corner_order(self._primitive(4)), (2, 3, 1, 0))


if __name__ == "__main__":
    unittest.main()
