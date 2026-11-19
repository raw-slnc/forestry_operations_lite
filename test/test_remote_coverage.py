"""Foreign source availability grids used by the yellow preview mesh."""

import unittest
from unittest.mock import patch

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
)

from forestry_operations_lite.remote_coverage import (
    GridCoverage,
    active_shape,
    available_shape_for_sentinel,
    coverage_key_for_sentinel,
)


class _MapSettings:
    def __init__(self, crs):
        self._crs = crs

    def destinationCrs(self):
        return self._crs


class _Canvas:
    def __init__(self, extent, crs):
        self._extent = extent
        self._settings = _MapSettings(crs)

    def extent(self):
        return self._extent

    def mapSettings(self):
        return self._settings


class RemoteCoverageTests(unittest.TestCase):

    def test_france_grid_covers_current_aiguille_du_midi_preview(self):
        coverage = GridCoverage.load("ign_mnt")
        wkt = coverage.shape_wkt(
            1001127.16, 6538238.61, 1001702.37, 6538588.79)
        self.assertTrue(wkt)
        geometry = QgsGeometry.fromWkt(wkt)
        self.assertTrue(geometry.contains(QgsGeometry.fromPointXY(
            QgsPointXY(1001414.77, 6538413.70))))

    def test_grid_outside_its_index_is_empty(self):
        coverage = GridCoverage.load("ign_mnt")
        self.assertEqual(coverage.shape_wkt(0, 0, 1000, 1000), "")

    def test_foreign_dem_and_dsm_share_provider_coverage(self):
        self.assertEqual(coverage_key_for_sentinel("__AHN_DTM__"), "ahn_dtm")
        self.assertEqual(coverage_key_for_sentinel("__AHN_DSM__"), "ahn_dtm")
        self.assertEqual(coverage_key_for_sentinel("__IGN_LIDARHD_MNT__"), "ign_mnt")
        self.assertEqual(coverage_key_for_sentinel("__IGN_LIDARHD_MNS__"), "ign_mnt")
        self.assertEqual(coverage_key_for_sentinel("__LINZ_DEM_1M__"), "linz_dem")
        self.assertIsNone(coverage_key_for_sentinel("__TERRARIUM_STD__"))

    def test_linz_grid_asset_loads(self):
        coverage = GridCoverage.load("linz_dem")
        self.assertEqual(coverage.epsg, 2193)
        self.assertEqual(coverage.mask.shape, (41, 41))
        self.assertTrue(coverage.mask.any())

    def test_browser_choice_and_yellow_mesh_use_the_same_coverage(self):
        canvas = _Canvas(
            QgsRectangle(1001127.16, 6538238.61, 1001702.37, 6538588.79),
            QgsCoordinateReferenceSystem("EPSG:2154"),
        )
        browser_choice = available_shape_for_sentinel(
            "__IGN_LIDARHD_MNT__", canvas)
        yellow_mesh = active_shape(canvas)

        self.assertIsNotNone(browser_choice)
        self.assertIsNotNone(yellow_mesh)
        self.assertEqual(browser_choice[0], "ign_mnt")
        self.assertIn(browser_choice[0], yellow_mesh[0])
        self.assertTrue(yellow_mesh[1][0].intersects(browser_choice[1][0]))

    def test_yellow_mesh_combines_every_available_source(self):
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        canvas = _Canvas(QgsRectangle(0, 0, 30, 10), crs)
        first = QgsGeometry.fromRect(QgsRectangle(1, 1, 5, 5))
        second = QgsGeometry.fromRect(QgsRectangle(20, 1, 25, 5))
        with patch(
            "forestry_operations_lite.remote_coverage.available_shapes",
            return_value=[
                ("first", (first, crs)),
                ("second", (second, crs)),
            ],
        ):
            keys, (combined, combined_crs) = active_shape(canvas)

        self.assertEqual(keys, ("first", "second"))
        self.assertEqual(combined_crs.authid(), "EPSG:4326")
        self.assertTrue(combined.contains(QgsGeometry.fromPointXY(QgsPointXY(2, 2))))
        self.assertTrue(combined.contains(QgsGeometry.fromPointXY(QgsPointXY(22, 2))))


if __name__ == "__main__":
    unittest.main()
