"""DSM grid, fallback, provenance, and recovery regressions (unittest)."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import numpy as np
from osgeo import gdal, osr

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from forestry_operations_lite.terrain import analysis as ta
from forestry_operations_lite.terrain.dem_loader import DEMLoader
from forestry_operations_lite.analysis_worker import TerrainAnalysisWorker
from forestry_operations_lite import dem_worker
from forestry_operations_lite import forestry_operations_lite_dockwidget as ui


def raster(data, gt=(0, 1, 0, 12, 0, -1), epsg=6676, nodata=-9999):
    data = np.asarray(data, dtype=np.float64)
    ds = gdal.GetDriverByName("MEM").Create(
        "", data.shape[1], data.shape[0], 1, gdal.GDT_Float64)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    band.WriteArray(data)
    result = DEMLoader()
    result._ds, result.gt, result.crs_wkt = ds, gt, srs.ExportToWkt()
    result.cell_size, result.nodata = abs(gt[1]), nodata
    result.data = data.copy()
    if nodata is not None:
        result.data[result.data == nodata] = np.nan
    return result


class AlignmentTests(unittest.TestCase):
    def test_matching_grid_preserves_values_and_nodata(self):
        source = raster([[10, -9999], [0, 20]])
        aligned, method = source.read_aligned_to(source)
        np.testing.assert_equal(aligned, source.data)
        self.assertEqual(method, "none")

    def test_integer_offset_reads_only_the_matching_window(self):
        source = raster(np.arange(144).reshape(12, 12))
        target = raster(np.zeros((4, 5)), gt=(2, 1, 0, 9, 0, -1))
        aligned, method = source.read_aligned_to(target)
        np.testing.assert_equal(aligned, source.data[3:7, 2:7])
        self.assertEqual(method, "none")

    def test_shifted_grid_is_resampled_even_when_shape_matches(self):
        gt = (0.4, 1, 0, 12.4, 0, -1)
        yy, xx = np.indices((12, 12))
        heights = (gt[0] + xx + .5) + 2 * (gt[3] - yy - .5)
        source = raster(heights, gt=gt)
        target = raster(np.zeros((6, 6)), gt=(2, 1, 0, 10, 0, -1))
        aligned, method = source.read_aligned_to(target)
        ty, tx = np.indices(target.data.shape)
        expected = 2 + tx + .5 + 2 * (10 - ty - .5)
        np.testing.assert_allclose(aligned, expected, atol=1e-8)
        self.assertEqual(method, "bilinear")

    def test_resolution_matches_the_resampled_dem(self):
        source = raster(np.full((40, 40), 115.), gt=(0, .1, 0, 4, 0, -.1))
        target = raster(np.full((8, 8), 100.), gt=(0, .5, 0, 4, 0, -.5))
        aligned, method = source.read_aligned_to(target)
        self.assertEqual(aligned.shape, target.data.shape)
        np.testing.assert_allclose(aligned - target.data, 15)
        self.assertEqual(method, "bilinear")

    def test_partial_and_zero_overlap_stay_missing(self):
        source = raster(np.full((4, 4), 20.), gt=(2, 1, 0, 10, 0, -1))
        target = raster(np.zeros((8, 8)))
        aligned, _ = source.read_aligned_to(target)
        self.assertEqual(np.count_nonzero(np.isfinite(aligned)), 16)
        self.assertTrue(np.isnan(aligned[0, 0]))
        outside = raster(np.zeros((3, 3)), gt=(100, 1, 0, 100, 0, -1))
        aligned, _ = source.read_aligned_to(outside)
        self.assertTrue(np.isnan(aligned).all())

    def test_bilinear_does_not_turn_nodata_holes_into_bare_ground(self):
        data = np.full((12, 12), 110.)
        data[3:9, 3:9] = -9999
        source = raster(data)
        target = raster(np.zeros((24, 24)), gt=(0, .5, 0, 12, 0, -.5))
        aligned, _ = source.read_aligned_to(target)
        self.assertTrue(np.isnan(aligned[9:15, 9:15]).all())
        self.assertAlmostEqual(aligned[2, 2], 110.)

    def test_different_crs_is_reprojected_to_the_dem(self):
        source = raster(np.full((20, 20), 110.),
                        gt=(138, .001, 0, 36, 0, -.001), epsg=4326)
        src = osr.SpatialReference(wkt=source.crs_wkt)
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(3857)
        for srs in (src, dst):
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = osr.CoordinateTransformation(src, dst)
        x, y, _ = transform.TransformPoint(138.005, 35.995)
        target = raster(np.zeros((5, 5)), gt=(x, 50, 0, y, 0, -50), epsg=3857)
        aligned, method = source.read_aligned_to(target)
        np.testing.assert_allclose(aligned, 110.)
        self.assertEqual(method, "bilinear")

    def test_missing_crs_raises_instead_of_silently_ignoring_dsm(self):
        source = raster([[10.]])
        source.crs_wkt = ""
        with self.assertRaisesRegex(ValueError, "coordinate reference"):
            source.read_aligned_to(raster([[0.]]))

    def test_rotated_target_retains_its_pixel_locations(self):
        yy, xx = np.indices((12, 12))
        source = raster(xx + .5 + 2 * (12 - yy - .5))
        gt = (3, .8, .1, 9, .1, -.8)
        target = raster(np.zeros((4, 4)), gt=gt)
        aligned, _ = source.read_aligned_to(target)
        ty, tx = np.indices(target.data.shape)
        x = gt[0] + (tx + .5) * gt[1] + (ty + .5) * gt[2]
        y = gt[3] + (tx + .5) * gt[4] + (ty + .5) * gt[5]
        np.testing.assert_allclose(aligned, x + 2 * y, atol=1e-8)


class CoefficientTests(unittest.TestCase):
    def test_existing_canopy_curve_is_unchanged(self):
        cs = np.array([[0., 3., 6.5, 10., 25.]])
        c, v = ta.cs_to_flow_coefficients(cs)
        np.testing.assert_allclose(c, [[.55, .55, .35, .15, .15]])
        np.testing.assert_allclose(v, [[.60, .60, .45, .30, .30]])

    def test_mixed_coverage_uses_input_coefficients_only_in_gaps(self):
        dem = np.array([[100., 100., 100., np.nan]])
        dsm = np.array([[110., 100., np.nan, 110.]])
        c, v, record = ta.prepare_flow_coefficients(dem, dsm, .7, .4)
        np.testing.assert_allclose(c[0, :3], [.15, .55, .7])
        np.testing.assert_allclose(v[0, :3], [.3, .6, .4])
        self.assertEqual(record["mode"], "mixed")
        self.assertEqual(record["dem_valid_cells"], 3)
        self.assertEqual(record["dsm_valid_cells"], 2)
        self.assertAlmostEqual(record["dsm_coverage"], 2 / 3)
        json.dumps(record, allow_nan=False)

    def test_all_missing_and_absent_dsm_match_manual_results(self):
        dem = np.tile(1000. - np.arange(12) * 2., (6, 1))
        fd = ta.d8_flow_direction(dem)
        results = []
        for dsm in (None, np.full(dem.shape, np.nan)):
            c, v, record = ta.prepare_flow_coefficients(dem, dsm, .7, .4)
            self.assertEqual(record["mode"], "manual")
            tt = ta.compute_travel_time(dem, fd, 20, velocity_coef=v)
            tc = ta.compute_tc(dem, fd, tt)
            results.append(ta.time_area_flow_metrics(dem, fd, tt, tc, 20, c_grid=c))
        for a, b in zip(*results):
            np.testing.assert_equal(a, b)

    def test_infinity_is_missing_and_shape_mismatch_is_an_error(self):
        dem = np.zeros((1, 2))
        c, v, record = ta.prepare_flow_coefficients(dem, np.array([[10., np.inf]]), .7, .4)
        np.testing.assert_allclose(c, [[.15, .7]])
        self.assertEqual(record["mode"], "mixed")
        with self.assertRaises(ValueError):
            ta.prepare_flow_coefficients(dem, np.zeros((2, 1)))


class RecoveryAndRecordTests(unittest.TestCase):
    def test_remote_dem_cache_follows_analysis_references(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            remote = root / "remote_dem"
            run = root / "001"
            remote.mkdir()
            run.mkdir()
            dem = remote / "ahn_dtm_20261101_120000.tif"
            dsm = remote / "ahn_dsm_20261101_120000.tif"
            unused = remote / "ahn_dtm_20261101_130000.tif"
            for path in (dem, dsm, unused):
                path.write_bytes(b"test")
            (run / "params.json").write_text(json.dumps({
                "dem_path": str(dem),
                "flow_coefficients": {"dsm": {"path": str(dsm)}},
            }))
            dummy = SimpleNamespace(
                _terrain_output_dir=lambda: folder,
                _scan_analysis_numbers=lambda: ["001"] if run.exists() else [],
                _dem_actual_path="",
                _cs_map_dem_path="",
                _dsm_loader=None,
            )

            ui.ForestryOperationsLiteDockWidget._gc_dem_cache(dummy)
            self.assertTrue(dem.exists())
            self.assertTrue(dsm.exists())
            self.assertFalse(unused.exists())

            (run / "params.json").unlink()
            run.rmdir()
            ui.ForestryOperationsLiteDockWidget._gc_dem_cache(dummy)
            self.assertFalse(dem.exists())
            self.assertFalse(dsm.exists())

    def test_current_remote_dsm_is_not_collected(self):
        with tempfile.TemporaryDirectory() as folder:
            remote = Path(folder, "remote_dem")
            remote.mkdir()
            dsm = remote / "ign_lidarhd_mns_20261101_120000.tif"
            dsm.write_bytes(b"test")
            dummy = SimpleNamespace(
                _terrain_output_dir=lambda: folder,
                _scan_analysis_numbers=lambda: [],
                _dem_actual_path="",
                _cs_map_dem_path="",
                _dsm_loader=SimpleNamespace(path=str(dsm)),
            )

            ui.ForestryOperationsLiteDockWidget._gc_dem_cache(dummy)
            self.assertTrue(dsm.exists())

    def test_dsm_cancel_leaves_existing_overwrite_results_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = Path(folder, "001")
            previous.mkdir()
            old_result = previous / "params.json"
            old_result.write_text('{"old": true}')
            dummy = mock.Mock()
            dummy._analysis_worker = dummy._dem_worker = None
            dummy._analysis_after_refetch = dummy._overwrite_retried = False
            dummy._dem_path = "local-dem.tif"
            dummy.chkFlow.isChecked.return_value = True
            dummy.chkStability.isChecked.return_value = False
            dummy.chkValley.isChecked.return_value = False
            dummy.chkOverwrite.isChecked.return_value = True
            dummy.cmbAreaLimit.currentData.return_value = None
            dummy._terrain_loader = raster(np.full((8, 8), 100.))
            dummy._terrain_output_dir.return_value = folder
            dummy._canvas_outside_loader.return_value = False
            dummy.preview_canvas.extent.return_value = ui.QgsRectangle(0, 4, 8, 12)
            dummy.preview_canvas.mapSettings.return_value.destinationCrs.return_value = ui.QgsCoordinateReferenceSystem("EPSG:6676")
            dummy._clip_dsm_for_analysis.side_effect = ui._DsmPreparationCancelled()
            ui.ForestryOperationsLiteDockWidget._run_terrain_analysis(dummy)
            dummy._clip_dsm_for_analysis.assert_called_once()
            dummy._next_seq.assert_not_called()
            dummy.lblAnalysisStatus.setText.assert_called_with("Analysis cancelled.")
            self.assertEqual(old_result.read_text(), '{"old": true}')

    def test_worker_result_is_copied_before_the_worker_is_released(self):
        record = {"mode": "mixed"}
        dummy = mock.Mock()
        dummy._analysis_ctx = dict(btn_prog=mock.Mock(), is_overwrite=False, tmp_folder="unused")
        dummy._analysis_worker.flow_conditions = record
        ui.ForestryOperationsLiteDockWidget._on_analysis_done(dummy, [], False, "", "")
        saved_ctx = dummy._finish_analysis.call_args.args[0]
        self.assertEqual(saved_ctx["flow_conditions"], record)

    def test_coefficient_fields_stay_editable_and_labels_reset_on_clear(self):
        dummy = SimpleNamespace(
            _dsm_path="test.tif", spinRunoff=mock.Mock(), spinVelocityCoef=mock.Mock(),
            _lbl_runoff_coef=mock.Mock(), _lbl_vel_coef=mock.Mock())
        dummy._flow_coef_hints = {dummy.spinRunoff: (mock.Mock(), "C hint"),
                                 dummy.spinVelocityCoef: (mock.Mock(), "V hint")}
        ui.ForestryOperationsLiteDockWidget._update_flow_coef_state(dummy)
        dummy.spinRunoff.setEnabled.assert_called_with(True)
        dummy.spinVelocityCoef.setEnabled.assert_called_with(True)
        self.assertIn("Fallback", dummy._lbl_runoff_coef.setText.call_args.args[0])
        dummy._dsm_path = ""
        ui.ForestryOperationsLiteDockWidget._update_flow_coef_state(dummy)
        dummy._lbl_runoff_coef.setText.assert_called_with("Runoff coef. C")
        dummy._lbl_vel_coef.setText.assert_called_with("Flow velocity coef.")
        dummy._flow_coef_hints[dummy.spinRunoff][0].setText.assert_called_with("C hint")

    def test_continue_and_cancel_are_distinct(self):
        for action in ("continue", "cancel"):
            dummy = SimpleNamespace(
                _dsm_path="missing.tif", _dsm_loader=None,
                _ask_dsm_alignment_recovery=mock.Mock(return_value=action))
            if action == "cancel":
                with self.assertRaises(ui._DsmPreparationCancelled):
                    ui.ForestryOperationsLiteDockWidget._clip_dsm_for_analysis(dummy, None)
            else:
                data, info = ui.ForestryOperationsLiteDockWidget._clip_dsm_for_analysis(dummy, None)
                self.assertIsNone(data)
                self.assertEqual(info["status"], "unavailable")
                self.assertIn("error", info)

    def test_retry_reopens_then_uses_the_aligned_dsm(self):
        target = raster(np.full((2, 2), 100.))
        source = raster(np.full((2, 2), 110.))
        source.path = "repaired.tif"
        dummy = SimpleNamespace(
            _dsm_path="repaired.tif", _dsm_loader=None,
            _ask_dsm_alignment_recovery=mock.Mock(return_value="retry"))
        with mock.patch.object(DEMLoader, "open_metadata", side_effect=[RuntimeError("bad read"), source]):
            data, info = ui.ForestryOperationsLiteDockWidget._clip_dsm_for_analysis(dummy, target)
        np.testing.assert_equal(data, np.full((2, 2), 110.))
        self.assertEqual(info["status"], "aligned")
        self.assertEqual(info["path"], "repaired.tif")
        dummy._ask_dsm_alignment_recovery.assert_called_once()

    def test_worker_records_applied_conditions_and_ui_displays_saved_values(self):
        dem = raster(np.tile(1000. - np.arange(12) * 2., (6, 1)), gt=(0, 20, 0, 120, 0, -20))
        dsm = dem.data + 10.
        dsm[:, :3] = np.nan
        params = dict(flow_method="d8", stability=False, valley=False, flow=True,
                      runoff=.7, velocity_coef=.4, duration_h=6., rainfall_mmh=50.,
                      total_mm=100., fs_thresh=1.2, plugin_version="test",
                      dsm_info={"path": "/data/dsm.tif", "status": "aligned", "resampling": "none"})
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder, ".tmp_001")
            tmp.mkdir()
            worker = TerrainAnalysisWorker(dem, dsm, params, str(tmp))
            saved = []
            worker._compute(saved)
            record = worker.flow_conditions
            self.assertEqual(record["mode"], "mixed")
            self.assertEqual(record["dsm_coverage"], .75)
            self.assertEqual(record["grid"]["geotransform"], list(dem.gt))
            self.assertEqual(record["routing"], ta.FLOW_ROUTING_OPTIONS)
            self.assertEqual(record["canopy_coefficients"], ta.FLOW_CANOPY_COEFFICIENTS)
            dummy = mock.Mock()
            dummy._format_analysis_number.return_value = "001"
            dummy.cmbAnalysisNumber.findData.return_value = -1
            dummy.iface = None
            ctx = dict(tmp_folder=str(tmp), out_dir=folder, params=params,
                       dem_path="/data/dem.tif", flow_conditions=record, seq="001",
                       n_cells=dem.data.size, rate_key="test", prog=mock.Mock(),
                       is_overwrite=False, pre_run_display_state={})
            ctx["prog"].elapsed.return_value = 1.
            ui.ForestryOperationsLiteDockWidget._finish_analysis(dummy, ctx, saved, "")
            path = Path(folder, "001", "params.json")
            stored = json.loads(path.read_text())
            self.assertEqual(stored["flow_coefficients"], record)
            dummy._terrain_output_dir.return_value = folder
            ui.ForestryOperationsLiteDockWidget._update_analysis_condition_label(dummy, "001")
            text = dummy.lblAnalysisCondition.setText.call_args.args[0]
            self.assertIn("DSM auto: 75.0% / Manual fallback: 25.0%", text)
            self.assertIn("Fallback: C=0.7", text)
            self.assertIn("/data/dsm.tif", dummy.lblAnalysisCondition.setToolTip.call_args.args[0])
            stored.pop("flow_coefficients")
            path.write_text(json.dumps(stored))
            ui.ForestryOperationsLiteDockWidget._update_analysis_condition_label(dummy, "001")
            self.assertIn("not recorded (legacy run)", dummy.lblAnalysisCondition.setText.call_args.args[0])


class NaganoMissingCanopyTests(unittest.TestCase):
    def test_dchm_nodata_is_preserved_but_measured_zero_is_valid(self):
        dem = raster([[100., 100.], [-9999., 100.]])
        canopy = raster([[-9999., 0.], [10., 10.]])
        with tempfile.TemporaryDirectory() as folder:
            dem_path, canopy_path = str(Path(folder, "dem.tif")), str(Path(folder, "canopy.tif"))
            gdal.GetDriverByName("GTiff").CreateCopy(dem_path, dem._ds)
            gdal.GetDriverByName("GTiff").CreateCopy(canopy_path, canopy._ds)
            ctx = mock.Mock()
            ctx.cancelled.return_value = False
            with mock.patch("forestry_operations_lite.nagano_dchm.download_tile_tif", return_value=canopy_path), \
                    mock.patch.object(dem_worker, "_validate_dchm_tile", return_value=(True, "")):
                result = dem_worker.fetch_nagano_dchm_dsm(
                    ctx, ["test"], str(Path(folder, "dchm")), str(Path(folder, "dsm")), dem_path)
            self.assertEqual(result["status"], "ok", result)
            loader = result["loader"].read_data()
            np.testing.assert_equal(loader.data, [[np.nan, 100.], [np.nan, 110.]])
            loader._ds = None  # Windows requires closure before TemporaryDirectory cleanup.


class DsmExportIntegrityTests(unittest.TestCase):
    @staticmethod
    def _write_tif(path, loader):
        copy = gdal.GetDriverByName("GTiff").CreateCopy(str(path), loader._ds)
        if copy is None:
            raise RuntimeError(f"Could not create test raster: {path}")
        copy = None

    @staticmethod
    def _ctx():
        return SimpleNamespace(
            created_files=[], check=mock.Mock(), report=mock.Mock(),
            cancelled=mock.Mock(return_value=False))

    def test_flow_alignment_does_not_change_dsm_export_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dtm_path = root / "original_dtm.tif"
            dsm_path = root / "original_dsm.tif"
            self._write_tif(dtm_path, raster(np.full((4, 4), 100.)))
            self._write_tif(
                dsm_path,
                raster(np.arange(64.).reshape(8, 8), gt=(0, .5, 0, 4, 0, -.5)))

            original_bytes = dsm_path.read_bytes()
            original_mtime = dsm_path.stat().st_mtime_ns
            dsm_loader = DEMLoader().open_metadata(str(dsm_path))
            aligned, method = dsm_loader.read_aligned_to(
                raster(np.zeros((4, 4)), gt=(0, 1, 0, 4, 0, -1)))
            self.assertEqual(method, "bilinear")
            self.assertEqual(aligned.shape, (4, 4))

            result = dem_worker.vs_export_zip(
                self._ctx(), {}, str(root / "tmp"), str(root / "zip"),
                str(dtm_path), str(dsm_path), [], {}, False)

            self.assertEqual(dsm_path.read_bytes(), original_bytes)
            self.assertEqual(dsm_path.stat().st_mtime_ns, original_mtime)
            with zipfile.ZipFile(result["zip_path"]) as archive:
                self.assertEqual(archive.read("odm_dem/dsm.tif"), original_bytes)
            dsm_loader._ds = None

    def test_remote_export_keeps_dsm_bytes_and_source_attribution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dtm_path = root / "source_dtm.tif"
            dsm_path = root / "source_dsm.tif"
            self._write_tif(dtm_path, raster(np.full((2, 2), 100.)))
            self._write_tif(dsm_path, raster(np.full((2, 2), 112.)))
            original_bytes = dsm_path.read_bytes()
            original_mtime = dsm_path.stat().st_mtime_ns
            plan = {
                "country": "test", "bbox": (0., 0., 2., 2.), "epsg": 6676,
                "ortho": None, "notes": [],
                "items": [
                    dem_worker._plan_item(
                        "odm_dem/dtm.tif", "Original DTM", "2025",
                        "DTM source", "DTM licence", "DTM attribution", ""),
                    dem_worker._plan_item(
                        "odm_dem/dsm.tif", "Original DSM", "2026",
                        "DSM source", "DSM licence", "DSM attribution", "DSM service"),
                ],
            }

            result = dem_worker.remote_export_zip(
                self._ctx(), plan, str(root / "tmp"), str(root / "zip"),
                str(dtm_path), str(dsm_path))

            self.assertEqual(dsm_path.read_bytes(), original_bytes)
            self.assertEqual(dsm_path.stat().st_mtime_ns, original_mtime)
            with zipfile.ZipFile(result["zip_path"]) as archive:
                self.assertEqual(archive.read("odm_dem/dsm.tif"), original_bytes)
                sources = archive.read("DATA_SOURCES.txt").decode("utf-8")
            self.assertIn("Data:     Original DSM", sources)
            self.assertIn("Source:   DSM source", sources)
            self.assertIn("Licence:  DSM licence", sources)
            self.assertIn("Attribution: DSM attribution", sources)


if __name__ == "__main__":
    unittest.main()
