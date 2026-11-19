# -*- coding: utf-8 -*-
"""Lightweight pre-fetch coverage for overseas elevation sources.

France and New Zealand use compact one-bit tile masks generated from the
providers' own published tile indexes.  AHN4 covers the Netherlands, so its
shape comes from the world map bundled with QGIS.  Only the small part below
the preview is polygonized; no elevation raster is downloaded here.

The coverage is a guide to fetchable tiles.  A published tile can still hold
NoData cells (for example restricted areas in France).
"""

import json
import math
import os
import warnings
import zlib

import numpy as np
from osgeo import gdal, ogr
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsProject,
)


_DATA_DIR = os.path.join(os.path.dirname(__file__), "data", "remote_coverage")

# FOL's browser sentinels -> the coverage used for the yellow pre-fetch mesh.
SENTINEL_KEYS = {
    "__AHN_DTM__": "ahn_dtm",
    "__AHN_DSM__": "ahn_dtm",
    "__IGN_LIDARHD_MNT__": "ign_mnt",
    "__IGN_LIDARHD_MNS__": "ign_mnt",
    "__LINZ_DEM_1M__": "linz_dem",
}

# Tile sizes in each source CRS.  The masks contain one bit per provider tile.
_GRIDS = {
    "ign_mnt": {"cell_w": 1000.0, "cell_h": 1000.0},
    "linz_dem": {"cell_w": 24000.0, "cell_h": 36000.0},
}

_SOURCE_ORDER = ("ahn_dtm", "ign_mnt", "linz_dem")
_cache = {}


class GridCoverage:
    """A provider tile grid whose boolean mask is stored packed and zipped."""

    def __init__(self, key, epsg, x0, y_top, cell_w, cell_h, mask):
        self.key = key
        self.epsg = int(epsg)
        self.x0 = float(x0)
        self.y_top = float(y_top)
        self.cell_w = float(cell_w)
        self.cell_h = float(cell_h)
        self.mask = mask.astype(bool)

    @classmethod
    def load(cls, key):
        spec = _GRIDS[key]
        with open(os.path.join(_DATA_DIR, key + ".json"), encoding="utf-8") as handle:
            data = json.load(handle)
        with open(os.path.join(_DATA_DIR, key + ".mask"), "rb") as handle:
            packed = handle.read()
        rows, cols = int(data["rows"]), int(data["cols"])
        bits = np.frombuffer(zlib.decompress(packed), dtype=np.uint8)
        mask = np.unpackbits(bits)[: rows * cols].reshape(rows, cols)
        return cls(key, data["epsg"], data["x0"], data["y_top"],
                   spec["cell_w"], spec["cell_h"], mask)

    def shape_wkt(self, xmin, ymin, xmax, ymax, margin_tiles=0):
        """Covered provider tiles within ``bbox`` as WKT, or an empty string."""
        rows, cols = self.mask.shape
        c0 = max(0, int(math.floor((xmin - self.x0) / self.cell_w)) - margin_tiles)
        c1 = min(cols, int(math.ceil((xmax - self.x0) / self.cell_w)) + margin_tiles)
        r0 = max(0, int(math.floor((self.y_top - ymax) / self.cell_h)) - margin_tiles)
        r1 = min(rows, int(math.ceil((self.y_top - ymin) / self.cell_h)) + margin_tiles)
        if c0 >= c1 or r0 >= r1:
            return ""
        window = self.mask[r0:r1, c0:c1]
        if not window.any():
            return ""

        raster = gdal.GetDriverByName("MEM").Create(
            "", c1 - c0, r1 - r0, 1, gdal.GDT_Byte)
        raster.SetGeoTransform((
            self.x0 + c0 * self.cell_w, self.cell_w, 0.0,
            self.y_top - r0 * self.cell_h, 0.0, -self.cell_h,
        ))
        band = raster.GetRasterBand(1)
        band.WriteArray(window.astype(np.uint8))

        vector_driver = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
        vectors = vector_driver.CreateDataSource("")
        layer = vectors.CreateLayer("coverage", geom_type=ogr.wkbPolygon)
        gdal.Polygonize(band, band, layer, -1, [])
        shapes = ogr.Geometry(ogr.wkbMultiPolygon)
        for feature in layer:
            geometry = feature.GetGeometryRef()
            if geometry is not None:
                shapes.AddGeometry(geometry.Clone())
        if not shapes.GetGeometryCount():
            return ""
        return shapes.UnionCascaded().ExportToWkt()


def coverage_key_for_sentinel(sentinel):
    return SENTINEL_KEYS.get(sentinel)


def _grid(key):
    cache_key = "grid:" + key
    if cache_key not in _cache:
        try:
            _cache[cache_key] = GridCoverage.load(key)
        except (OSError, ValueError, KeyError, zlib.error):
            _cache[cache_key] = None
    return _cache[cache_key]


def _country_geometry(code):
    """A QGIS-bundled country geometry in WGS84, or None."""
    cache_key = "country:" + code
    if cache_key in _cache:
        return _cache[cache_key]
    found = None
    source = None
    try:
        path = os.path.join(
            QgsApplication.pkgDataPath(), "resources", "data", "world_map.gpkg")
        if os.path.isfile(path):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                source = ogr.Open(path)
        layer = source.GetLayer("countries") if source is not None else None
        if layer is not None:
            layer.SetAttributeFilter("iso_a2 = '{}'".format(code))
            shapes = []
            for feature in layer:
                geometry = feature.GetGeometryRef()
                if geometry is not None:
                    shapes.append(geometry.Clone())
            layer.SetAttributeFilter(None)
            if shapes:
                merged = shapes[0]
                for geometry in shapes[1:]:
                    merged = merged.Union(geometry)
                found = QgsGeometry.fromWkt(merged.ExportToWkt())
    except Exception:  # noqa: BLE001 - absence only means no yellow guide
        found = None
    _cache[cache_key] = found
    return found


def coverage_shape(key, canvas, margin_tiles=1):
    """Return ``(geometry, CRS)`` under the canvas, or None when unavailable."""
    if canvas is None:
        return None
    extent = canvas.extent()
    canvas_crs = canvas.mapSettings().destinationCrs()
    if extent.isEmpty() or not canvas_crs.isValid():
        return None

    if key == "ahn_dtm":
        geometry = _country_geometry("NL")
        if geometry is None or geometry.isEmpty():
            return None
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        try:
            box = QgsCoordinateTransform(
                canvas_crs, wgs84, QgsProject.instance().transformContext()
            ).transformBoundingBox(extent)
        except QgsCsException:
            return None
        visible = geometry.intersection(QgsGeometry.fromRect(box))
        return (visible, wgs84) if not visible.isEmpty() else None

    grid = _grid(key)
    if grid is None:
        return None
    source_crs = QgsCoordinateReferenceSystem("EPSG:{}".format(grid.epsg))
    try:
        box = QgsCoordinateTransform(
            canvas_crs, source_crs, QgsProject.instance().transformContext()
        ).transformBoundingBox(extent)
    except QgsCsException:
        return None
    wkt = grid.shape_wkt(
        box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum(),
        margin_tiles=margin_tiles,
    )
    return (QgsGeometry.fromWkt(wkt), source_crs) if wkt else None


def canvas_has_coverage(key, canvas):
    """True/False from the bundled index; None when it cannot be determined."""
    if key not in _SOURCE_ORDER:
        return None
    if key != "ahn_dtm" and _grid(key) is None:
        return None
    if key == "ahn_dtm" and _country_geometry("NL") is None:
        return None
    return coverage_shape(key, canvas, margin_tiles=0) is not None


def available_shape(key, canvas, margin_tiles=1):
    """Return a drawable shape only when the current preview has coverage."""
    if canvas_has_coverage(key, canvas) is not True:
        return None
    shape = coverage_shape(key, canvas, margin_tiles=margin_tiles)
    return (key, shape) if shape is not None else None


def available_shape_for_sentinel(sentinel, canvas, margin_tiles=1):
    """Use the same availability rule as the DEM/DSM browser choices."""
    key = coverage_key_for_sentinel(sentinel)
    if key is None:
        return None
    return available_shape(key, canvas, margin_tiles=margin_tiles)


def available_shapes(canvas):
    """All foreign source shapes available below the current preview."""
    found = []
    for key in _SOURCE_ORDER:
        item = available_shape(key, canvas, margin_tiles=1)
        if item is not None:
            found.append(item)
    return found


def active_shape(canvas):
    """Combined coverage of every foreign source below the current preview."""
    items = available_shapes(canvas)
    if not items:
        return None

    canvas_crs = canvas.mapSettings().destinationCrs()
    if not canvas_crs.isValid():
        return None
    geometries = []
    keys = []
    for key, (geometry, source_crs) in items:
        converted = QgsGeometry(geometry)
        try:
            if source_crs.isValid() and source_crs != canvas_crs:
                converted.transform(QgsCoordinateTransform(
                    source_crs, canvas_crs,
                    QgsProject.instance().transformContext()))
        except QgsCsException:
            continue
        if not converted.isEmpty():
            keys.append(key)
            geometries.append(converted)
    if not geometries:
        return None

    combined = geometries[0]
    for geometry in geometries[1:]:
        combined = combined.combine(geometry)
    return tuple(keys), (combined, canvas_crs)
