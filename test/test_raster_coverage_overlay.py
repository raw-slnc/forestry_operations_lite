"""DEM/DSM preview coverage footprint and hatch regressions."""

import unittest

import numpy as np
from qgis.core import QgsCoordinateReferenceSystem, QgsGeometry, QgsRectangle
from qgis.gui import QgsMapCanvas
from qgis.testing import start_app

from forestry_operations_lite.raster_coverage_overlay import (
    COVERAGE_HALO_COLOR,
    COVERAGE_HALO_PX,
    LINE_PX,
    RasterCoverageOverlay,
    loader_footprint,
)


QGIS_APP = start_app()


class _Loader:
    def __init__(self, gt, shape, crs_wkt):
        self.gt = gt
        self.data = np.zeros(shape, dtype=np.float32)
        self.crs_wkt = crs_wkt
        self._ds = None


class RasterCoverageOverlayTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.crs = QgsCoordinateReferenceSystem("EPSG:6676")

    def test_loader_footprint_uses_full_geotransform(self):
        loader = _Loader(
            (100.0, 2.0, 0.5, 300.0, 0.25, -2.0),
            (10, 20),
            self.crs.toWkt(),
        )
        geometry, crs = loader_footprint(loader)
        self.assertEqual(crs.authid(), "EPSG:6676")
        points = geometry.asPolygon()[0]
        expected = [
            (100.0, 300.0),
            (140.0, 305.0),
            (145.0, 285.0),
            (105.0, 280.0),
            (100.0, 300.0),
        ]
        self.assertEqual([(point.x(), point.y()) for point in points], expected)

    def test_loader_without_grid_has_no_footprint(self):
        loader = _Loader((0, 1, 0, 0, 0, -1), (1, 1), self.crs.toWkt())
        loader.data = None
        self.assertIsNone(loader_footprint(loader))

    def test_opposite_directions_produce_opposite_slopes(self):
        box = QgsRectangle(0, 0, 100, 100)
        positive = RasterCoverageOverlay._diagonal_lines(box, 20, 1).asMultiPolyline()
        negative = RasterCoverageOverlay._diagonal_lines(box, 20, -1).asMultiPolyline()
        positive_dy = positive[0][-1].y() - positive[0][0].y()
        negative_dy = negative[0][-1].y() - negative[0][0].y()
        self.assertGreater(positive_dy, 0)
        self.assertLess(negative_dy, 0)

    def test_yellow_mesh_can_combine_both_directions(self):
        box = QgsRectangle(0, 0, 100, 100)
        polylines = []
        for direction in (1, -1):
            polylines.extend(
                RasterCoverageOverlay._diagonal_lines(box, 20, direction).asMultiPolyline())
        slopes = [line[-1].y() - line[0].y() for line in polylines]
        self.assertTrue(any(value > 0 for value in slopes))
        self.assertTrue(any(value < 0 for value in slopes))

    def _canvas(self):
        canvas = QgsMapCanvas()
        canvas.resize(400, 300)
        canvas.setDestinationCrs(self.crs)
        canvas.setExtent(QgsRectangle(0, 0, 100, 100))
        return canvas

    def test_remote_mesh_draws_yellow_lines_and_gray_halo(self):
        canvas = self._canvas()
        overlay = RasterCoverageOverlay(
            canvas,
            "#e0ae00",
            directions=(1, -1),
            line_halo_color=COVERAGE_HALO_COLOR,
            line_halo_px=COVERAGE_HALO_PX,
        )
        overlay.set_geometry(
            QgsGeometry.fromRect(QgsRectangle(10, 10, 90, 90)), self.crs)

        self.assertFalse(overlay._lines.asGeometry().isEmpty())
        self.assertFalse(overlay._line_halo.asGeometry().isEmpty())
        self.assertTrue(overlay._lines.asGeometry().isGeosEqual(
            overlay._line_halo.asGeometry()))
        self.assertAlmostEqual(
            overlay._lines.symbol().symbolLayer(0).width(), LINE_PX)
        self.assertAlmostEqual(
            overlay._line_halo.symbol().symbolLayer(0).width(),
            LINE_PX + 2.0 * COVERAGE_HALO_PX,
        )
        self.assertEqual(
            overlay._line_halo.symbol().symbolLayer(0).color().name(),
            COVERAGE_HALO_COLOR,
        )

    def test_dem_and_dsm_lines_draw_with_gray_halo(self):
        for direction in (1, -1):
            canvas = self._canvas()
            overlay = RasterCoverageOverlay(
                canvas,
                "#2474d8",
                direction=direction,
                line_halo_color=COVERAGE_HALO_COLOR,
                line_halo_px=COVERAGE_HALO_PX,
            )
            overlay.set_geometry(
                QgsGeometry.fromRect(QgsRectangle(10, 10, 90, 90)), self.crs)

            self.assertFalse(overlay._lines.asGeometry().isEmpty())
            self.assertFalse(overlay._line_halo.asGeometry().isEmpty())
            self.assertTrue(overlay._lines.asGeometry().isGeosEqual(
                overlay._line_halo.asGeometry()))
            self.assertAlmostEqual(
                overlay._line_halo.symbol().symbolLayer(0).width(),
                LINE_PX + 2.0 * COVERAGE_HALO_PX,
            )
            self.assertEqual(
                overlay._line_halo.symbol().symbolLayer(0).color().name(),
                COVERAGE_HALO_COLOR,
            )

    def test_fetched_footprint_removes_remote_mesh(self):
        canvas = self._canvas()
        overlay = RasterCoverageOverlay(
            canvas,
            "#e0ae00",
            directions=(1, -1),
            line_halo_color=COVERAGE_HALO_COLOR,
            line_halo_px=COVERAGE_HALO_PX,
        )
        overlay.set_geometry(
            QgsGeometry.fromRect(QgsRectangle(0, 0, 100, 100)), self.crs)
        fetched = _Loader(
            (0.0, 1.0, 0.0, 100.0, 0.0, -1.0),
            (100, 100),
            self.crs.toWkt(),
        )

        overlay.set_exclusion_loaders((fetched,))

        self.assertTrue(overlay._lines.asGeometry().isEmpty())
        self.assertTrue(overlay._line_halo.asGeometry().isEmpty())

if __name__ == "__main__":
    unittest.main()
