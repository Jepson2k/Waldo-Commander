"""Scene objects Waldo Commander adds to NiceGUI's: studio lighting, a fading floor, PBR meshes."""

from nicegui.elements.scene.scene_object3d import Object3D


class StudioLights(Object3D, component="studio_lights.js"):
    """Hemisphere ambient plus key, fill and rim lights; the key casts shadows within ``radius`` of the origin."""

    def __init__(self, radius: float) -> None:
        super().__init__(radius)


class Floor(Object3D, component="floor.js"):
    """A floor disc that fades out past ``reach``, under a polar grid whose lines fade with radius."""

    def __init__(
        self, reach: float, sectors: int, rings: int, color: str, grid_color: str
    ) -> None:
        super().__init__(reach, sectors, rings, color, grid_color)


class Stl(Object3D, component="stl.js"):
    """An STL mesh with a standard (PBR) material that casts and receives shadows and reports its load."""

    def __init__(self, url: str, wireframe: bool = False) -> None:
        super().__init__(url, wireframe)


__all__ = ["Floor", "Stl", "StudioLights"]
