"""Metric DEM preparation and DSM alignment on the final analysis grid."""
import json
import unittest

import numpy as np
from osgeo import gdal, osr

from forestry_operations_lite.terrain.dem_loader import DEMLoader
from test_dsm_flow import raster


class DemPreparationTests(unittest.TestCase):
    def assert_metric_square(self, dem, epsg=None):
        srs = osr.SpatialReference(wkt=dem.crs_wkt)
        self.assertTrue(srs.IsProjected())
        self.assertAlmostEqual(srs.GetLinearUnits(), 1)
        self.assertGreater(dem.gt[1], 0)
        self.assertAlmostEqual(dem.gt[1], -dem.gt[5])
        self.assertEqual((dem.gt[2], dem.gt[4]), (0, 0))
        if epsg:
            self.assertEqual(srs.GetAuthorityCode(None), str(epsg))
        json.dumps(dem.preparation, allow_nan=False)

    def test_suitable_plane_grid_is_preserved(self):
        source = raster(np.arange(144).reshape(12, 12))
        result = source.prepare_for_analysis()
        self.assertIs(result, source)
        self.assertEqual(result.gt, source.gt)
        self.assert_metric_square(result, 6676)

    def test_geographic_dem_and_dsm_use_same_final_grid(self):
        gt = (138.1, .0001, 0, 35.1, 0, -.0001)
        dem = raster(np.full((20, 24), 100.), gt=gt, epsg=4326)
        dsm = raster(np.full((20, 24), 115.), gt=gt, epsg=4326)
        result = dem.prepare_for_analysis()
        self.assert_metric_square(result, 32654)
        aligned, method = dsm.read_aligned_to(result)
        self.assertEqual(method, "bilinear")
        self.assertEqual(aligned.shape, result.data.shape)
        valid = np.isfinite(aligned) & np.isfinite(result.data)
        self.assertTrue(valid.any())
        np.testing.assert_allclose(aligned[valid] - result.data[valid], 15)
        self.assertEqual(dem.gt, gt)
        np.testing.assert_equal(dem.data, 100)

    def test_web_mercator_is_actually_reprojected(self):
        source = raster(np.full((20, 20), 100.),
                        gt=(15370000, 10, 0, 4170000, 0, -10), epsg=3857)
        result = source.prepare_for_analysis()
        self.assert_metric_square(result, 32654)
        self.assertLess(result.cell_size, 9)

    def test_southern_hemisphere(self):
        source = raster(np.full((12, 12), 100.),
                        gt=(174, .0001, 0, -41, 0, -.0001), epsg=4326)
        self.assert_metric_square(source.prepare_for_analysis(), 32760)

    def test_nonmetric_projected_crs(self):
        source = raster(np.full((12, 12), 100.),
                        gt=(1000000, 10, 0, 200000, 0, -10), epsg=2263)
        self.assert_metric_square(source.prepare_for_analysis(), 32618)

    def test_rotated_and_rectangular_grids_become_square(self):
        for gt in ((0, 1, .2, 12, .1, -1), (0, 2, 0, 12, 0, -1)):
            with self.subTest(gt=gt):
                result = raster(np.full((12, 12), 100.), gt=gt).prepare_for_analysis()
                self.assert_metric_square(result, 6676)
                np.testing.assert_allclose(result.data[np.isfinite(result.data)], 100)

    def test_rotated_clip_keeps_correct_origin_and_covers_four_corners(self):
        source = raster(np.arange(400).reshape(20, 20), gt=(0, 1, .2, 30, .1, -1))
        clipped = source.clip_to_extent(6, 15, 12, 22)
        col, row = gdal.ApplyGeoTransform(gdal.InvGeoTransform(source.gt),
                                        clipped.gt[0], clipped.gt[3])
        self.assertAlmostEqual(col, round(col))
        self.assertAlmostEqual(row, round(row))
        r, c = round(row), round(col)
        h, w = clipped.data.shape
        np.testing.assert_equal(clipped.data, source.data[r:r+h, c:c+w])

    def test_downsampling_odd_dimensions_has_correct_georeferencing(self):
        source = raster(np.full((13, 17), 100.), gt=(0, .1, 0, 1.3, 0, -.1))
        result = source.prepare_for_analysis(target_cell_size=.5)
        self.assert_metric_square(result, 6676)
        self.assertAlmostEqual(result.cell_size, .5)
        self.assertEqual(result.preparation["resampling"], "average")
        self.assertEqual(result.gt, result._ds.GetGeoTransform())
        self.assertEqual(result.data.shape, (result._ds.RasterYSize, result._ds.RasterXSize))

    def test_dem_mask_and_dsm_missing_values_remain_missing(self):
        source = raster(np.full((12, 12), 100.))
        band = source._ds.GetRasterBand(1)
        band.CreateMaskBand(gdal.GMF_PER_DATASET)
        mask = np.full((12, 12), 255, dtype=np.uint8)
        mask[4:8, 4:8] = 0
        band.GetMaskBand().WriteArray(mask)
        clipped = source.clip_to_extent(0, 0, 12, 12).prepare_for_analysis()
        aligned, _ = raster(np.full((12, 12), 115.)).read_aligned_to(clipped)
        self.assertTrue(np.isnan(clipped.data[4:8, 4:8]).all())
        self.assertTrue(np.isnan(aligned[4:8, 4:8]).all())

    def test_declared_elevation_units_scale_and_offset(self):
        source = raster([[100., -9999], [200., 300.]])
        band = source._ds.GetRasterBand(1)
        band.SetUnitType("ft")
        band.SetScale(2)
        band.SetOffset(10)
        source.data = None
        source.read_data()
        self.assertAlmostEqual(source.data[0, 0], 210 * .3048)
        self.assertTrue(np.isnan(source.data[0, 1]))
        target = raster(np.zeros((2, 2)))
        aligned, _ = source.read_aligned_to(target)
        np.testing.assert_equal(aligned, source.data)

    def test_missing_crs_and_polar_inputs_stop(self):
        source = raster(np.ones((12, 12)))
        source.crs_wkt = ""
        with self.assertRaisesRegex(ValueError, "CRS is unknown"):
            source.prepare_for_analysis()
        source = raster(np.ones((12, 12)), gt=(0, .001, 0, 89, 0, -.001), epsg=4326)
        with self.assertRaisesRegex(ValueError, "UTM conversion"):
            source.prepare_for_analysis()

    def test_declared_different_height_datums_stop_alignment(self):
        dem = raster(np.ones((12, 12)))
        dsm = raster(np.ones((12, 12)))
        horizontal = osr.SpatialReference(wkt=dem.crs_wkt)
        for loader, epsg in ((dem, 5703), (dsm, 5701)):
            vertical = osr.SpatialReference()
            vertical.ImportFromEPSG(epsg)
            compound = osr.SpatialReference()
            compound.SetCompoundCS("test", horizontal, vertical)
            loader.crs_wkt = compound.ExportToWkt()
        with self.assertRaisesRegex(ValueError, "vertical datums"):
            dsm.read_aligned_to(dem.prepare_for_analysis())
