# -*- coding: utf-8 -*-
"""Lightweight DEM/DSM coverage hatching for the FOL preview canvas.

DEM and DSM use opposite single-line directions.  Each remains easy to see
on its own; where both rasters overlap, the two directions form a grid.  The
lines are generated only around the visible canvas extent so large rasters do
not create a large persistent vector layer.
"""

import math

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCsException,
    QgsGeometry,
    QgsLineSymbol,
    QgsPointXY,
    QgsProject,
    QgsSimpleLineSymbolLayer,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QColor


# Wider than QLAE's source-coverage mesh so the map behind it remains legible.
SPACING_PX = 20
LINE_PX = 0.8
OUTLINE_PX = 1.0
COVERAGE_HALO_PX = 0.08
COVERAGE_HALO_COLOR = "#e6e6e6"


def _line_symbol(color, width):
    layer = QgsSimpleLineSymbolLayer(color, width)
    layer.setWidthUnit(Qgis.RenderUnit.Pixels)
    return QgsLineSymbol([layer])


def loader_footprint(loader):
    """Return ``(polygon, CRS)`` for a DEMLoader-like object, else ``None``."""
    if loader is None:
        return None
    gt = getattr(loader, "gt", None)
    crs_wkt = getattr(loader, "crs_wkt", None)
    if gt is None or len(gt) != 6 or not crs_wkt:
        return None

    dataset = getattr(loader, "_ds", None)
    data = getattr(loader, "data", None)
    if dataset is not None:
        columns = int(dataset.RasterXSize)
        rows = int(dataset.RasterYSize)
    elif data is not None and getattr(data, "ndim", 0) == 2:
        rows, columns = map(int, data.shape)
    else:
        return None
    if rows <= 0 or columns <= 0:
        return None

    def point(column, row):
        x = gt[0] + column * gt[1] + row * gt[2]
        y = gt[3] + column * gt[4] + row * gt[5]
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("Non-finite raster footprint")
        return QgsPointXY(x, y)

    try:
        ring = [point(0, 0), point(columns, 0), point(columns, rows),
                point(0, rows)]
    except (TypeError, ValueError):
        return None
    ring.append(QgsPointXY(ring[0]))

    crs = QgsCoordinateReferenceSystem()
    crs.createFromWkt(crs_wkt)
    if not crs.isValid():
        return None
    return QgsGeometry.fromPolygonXY([ring]), crs


class RasterCoverageOverlay:
    """Draw diagonal coverage lines and an outline over a geometry.

    DEM/DSM callers use one direction and a raster loader.  The pre-fetch
    availability guide uses both directions and a provider coverage geometry.
    """

    def __init__(self, canvas, color, direction=None, spacing_px=SPACING_PX,
                 directions=None, line_halo_color=None, line_halo_px=0.0):
        if directions is None:
            directions = (direction,)
        directions = tuple(directions)
        if not directions or any(value not in (-1, 1) for value in directions):
            raise ValueError("directions must contain only -1 and 1")
        self._canvas = canvas
        self._directions = directions
        self._direction = directions[0]  # compatibility for existing callers/tests
        self._spacing_px = spacing_px
        self._geometry = None
        self._crs = None
        self._exclusion_loaders = ()

        line_color = QColor(color)
        line_color.setAlpha(145)
        outline_color = QColor(color)
        outline_color.setAlpha(205)
        self._line_halo = None
        if line_halo_color is not None and line_halo_px > 0:
            halo_color = QColor(line_halo_color)
            self._line_halo = QgsRubberBand(canvas, Qgis.GeometryType.Line)
            self._line_halo.setSymbol(
                _line_symbol(halo_color, LINE_PX + 2.0 * line_halo_px))
        self._lines = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self._lines.setSymbol(_line_symbol(line_color, LINE_PX))
        self._outline = QgsRubberBand(canvas, Qgis.GeometryType.Line)
        self._outline.setSymbol(_line_symbol(outline_color, OUTLINE_PX))

        self._timer = QTimer(canvas)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.redraw)
        canvas.extentsChanged.connect(self._timer.start)

    def set_loader(self, loader):
        footprint = loader_footprint(loader)
        if footprint is None:
            self.reset()
            return
        self.set_geometry(*footprint)

    def set_geometry(self, geometry, crs):
        """Set an arbitrary coverage shape and its CRS."""
        if geometry is None or geometry.isEmpty() or crs is None or not crs.isValid():
            self.reset()
            return
        self._geometry = QgsGeometry(geometry)
        self._crs = crs
        self.redraw()

    def set_exclusion_loaders(self, loaders):
        """Hide this guide beneath already fetched raster footprints."""
        self._exclusion_loaders = tuple(loader for loader in loaders if loader is not None)
        self.redraw()

    def reset(self):
        self._geometry = None
        self._crs = None
        if self._line_halo is not None:
            self._line_halo.reset(Qgis.GeometryType.Line)
        self._lines.reset(Qgis.GeometryType.Line)
        self._outline.reset(Qgis.GeometryType.Line)

    def redraw(self):
        if self._line_halo is not None:
            self._line_halo.reset(Qgis.GeometryType.Line)
        self._lines.reset(Qgis.GeometryType.Line)
        self._outline.reset(Qgis.GeometryType.Line)
        if self._geometry is None or self._geometry.isEmpty():
            return

        area = QgsGeometry(self._geometry)
        canvas_crs = self._canvas.mapSettings().destinationCrs()
        try:
            if self._crs.isValid() and canvas_crs.isValid() and self._crs != canvas_crs:
                area.transform(QgsCoordinateTransform(
                    self._crs, canvas_crs,
                    QgsProject.instance().transformContext()))
        except QgsCsException:
            return

        # The yellow pre-fetch mesh remains only where data has not already
        # been fetched.  Blue DEM and green DSM footprints therefore stay
        # legible instead of forming a three-colour grid.
        for loader in self._exclusion_loaders:
            footprint = loader_footprint(loader)
            if footprint is None:
                continue
            excluded, excluded_crs = footprint
            try:
                if (excluded_crs.isValid() and canvas_crs.isValid()
                        and excluded_crs != canvas_crs):
                    excluded.transform(QgsCoordinateTransform(
                        excluded_crs, canvas_crs,
                        QgsProject.instance().transformContext()))
            except QgsCsException:
                continue
            area = area.difference(excluded)
            if area.isEmpty():
                return

        extent = self._canvas.extent()
        map_units_per_pixel = self._canvas.mapUnitsPerPixel()
        if extent.isEmpty() or map_units_per_pixel <= 0:
            return
        view = extent.buffered(map_units_per_pixel * self._spacing_px * 2)
        visible_area = area.clipped(view)
        if visible_area.isEmpty():
            return

        boundary = visible_area.constGet().boundary()
        if boundary is not None:
            self._outline.setToGeometry(QgsGeometry(boundary))
        spacing = map_units_per_pixel * self._spacing_px
        polylines = []
        for direction in self._directions:
            polylines.extend(self._diagonal_lines(view, spacing, direction).asMultiPolyline())
        lines = QgsGeometry.fromMultiPolylineXY(polylines)
        clipped_lines = lines.intersection(visible_area)
        if self._line_halo is not None:
            self._line_halo.setToGeometry(clipped_lines)
        self._lines.setToGeometry(clipped_lines)

    @staticmethod
    def _diagonal_lines(box, spacing, direction):
        """Return fixed-grid parallel lines across ``box``."""
        offset_spacing = spacing * math.sqrt(2.0)
        corners = [
            box.yMinimum() - direction * box.xMinimum(),
            box.yMaximum() - direction * box.xMinimum(),
            box.yMinimum() - direction * box.xMaximum(),
            box.yMaximum() - direction * box.xMaximum(),
        ]
        first = math.floor(min(corners) / offset_spacing)
        last = math.ceil(max(corners) / offset_spacing)
        lines = []
        for index in range(first, last + 1):
            intercept = index * offset_spacing
            lines.append([
                QgsPointXY(box.xMinimum(), direction * box.xMinimum() + intercept),
                QgsPointXY(box.xMaximum(), direction * box.xMaximum() + intercept),
            ])
        return QgsGeometry.fromMultiPolylineXY(lines)
