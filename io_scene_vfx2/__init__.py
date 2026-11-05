from __future__ import annotations


bl_info = {
    "name": "Macross VF-X2 Model Importer",
    "author": "Macross VF-X2 Recompiled contributors",
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "File > Import > Macross VF-X2 Model",
    "description": "Import Macross VF-X2 TMD and MOD models with TIM/TIX textures",
    "category": "Import-Export",
}


def register() -> None:
    from . import importer

    importer.register()


def unregister() -> None:
    from . import importer

    importer.unregister()
