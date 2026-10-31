"""
DEM の取得をワーカースレッドで行う。

- DemJobWorker: 取得処理（job）を受け取って別スレッドで動かす共通の入れ物。
  job(ctx) は画面（ウィジェット・QgsProject）に触らず、状況は ctx.report(text)、
  中止の確認は ctx.check() / ctx.cancelled() で行う。作ったファイルは
  ctx.created_files に入れておくと、中止・エラー時に dockwidget 側が消す。
- fetch_tile_dem: 国土地理院 / Terrarium の標高タイルを取得して GeoTIFF にし、
  UTM へ投影し直して DEMLoader で読み込む（_load_gsi_dem の取得部分）。

取得結果を画面へ反映する（_terrain_loader の差し替え・ラベル表示）のは、
done を受けた dockwidget 側（メインスレッド）。
"""

import datetime
import os
import string
import traceback

import numpy as np
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import Qgis, QgsMessageLog


class DemFetchCancelled(Exception):
    """DEM 取得の中止（ワーカー内部用）。"""


class DemFetchError(Exception):
    """利用者に見せる取得エラー（文言をそのまま lblDemInfo に出す）。"""


class DemJobWorker(QThread):
    """DEM 取得の job をバックグラウンドで行う。"""

    # 状況の文言（lblDemInfo に出す）
    message = pyqtSignal(str)
    # (result, cancelled, error)  error は利用者に見せる文言
    done = pyqtSignal(object, bool, str)

    def __init__(self, job, parent=None):
        super().__init__(parent)
        self._job = job
        self._cancelled = False
        self.created_files = []
        self._watched_dirs = []

    # ── 中止・エラー時に消すファイル ──
    def watch_dir(self, path):
        """path の今の中身を控える。中止・エラー時は、その後に増えたファイル
        （途中まで落とした ZIP を含む）を消す。前からあるキャッシュは消さない。"""
        os.makedirs(path, exist_ok=True)
        self._watched_dirs.append((path, set(os.listdir(path))))

    def new_files(self):
        """この取得で作ったファイル（created_files と watch_dir で増えた分）。
        ワーカーが終わった後にメインスレッドから呼ぶ。"""
        files = list(self.created_files)
        for path, before in self._watched_dirs:
            try:
                names = set(os.listdir(path)) - before
            except OSError:
                continue
            files.extend(os.path.join(path, n) for n in sorted(names)
                         if os.path.isfile(os.path.join(path, n)))
        return files

    # ── job から使う ──
    def cancel(self):
        self._cancelled = True

    def cancelled(self):
        return self._cancelled

    def check(self):
        if self._cancelled:
            raise DemFetchCancelled()

    def report(self, text):
        self.message.emit(text)

    # ── スレッド本体 ──
    def run(self):
        try:
            result = self._job(self)
            self.check()
        except DemFetchCancelled:
            self.done.emit(None, True, "")
            return
        except DemFetchError as exc:
            self.done.emit(None, False, str(exc))
            return
        except Exception as exc:  # noqa: BLE001  ワーカーの外へ例外を出さない
            QgsMessageLog.logMessage(
                "DEM fetch failed:\n" + traceback.format_exc(),
                "Forestry Operations Lite", Qgis.MessageLevel.Warning)
            self.done.emit(None, False, f"⚠ {exc}")
            return
        self.done.emit(result, False, "")


def fetch_tile_dem(ctx, sources, ext84, out_dir, fname_prefix, fetch_msg, reproject):
    """国土地理院 / Terrarium のタイルを ext84（WGS84 の範囲）で取得する。

    ext84     : (lon_min, lat_min, lon_max, lat_max)
    reproject : (tif_path, centre_lon, centre_lat) -> 投影し直した tif_path
    戻り値    : {"loader": DEMLoader, "tif_path": str}
    """
    from .terrain.dem_loader import DEMLoader, GSITileDEMLoader, save_as_geotiff

    lon_min, lat_min, lon_max, lat_max = ext84
    ctx.report(fetch_msg)

    def _progress(done, total):
        if total > 1:
            ctx.report(f"{fetch_msg} {done}/{total} tiles")

    gsi_loader = GSITileDEMLoader()
    try:
        gsi_loader.fetch_for_extent(
            lon_min, lat_min, lon_max, lat_max,
            sources=sources,
            cancel_cb=ctx.cancelled,
            progress_cb=_progress,
        )
    except MemoryError as e:
        raise DemFetchError(f"⚠ {e}")

    if getattr(gsi_loader, "_cancelled", False):
        raise DemFetchCancelled()

    if gsi_loader.data is None or np.all(np.isnan(gsi_loader.data)):
        errs = getattr(gsi_loader, "last_errors", [])
        detail = errs[0] if errs else "out of range or connection error"
        raise DemFetchError(f"⚠ Tile fetch failed: {detail}")

    # GeoTIFF として保存
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tif_path = os.path.join(out_dir, f"{fname_prefix}_{ts}.tif")

    ctx.check()
    ctx.report("Converting to GeoTIFF...")
    try:
        ctx.created_files.append(tif_path)
        save_as_geotiff(gsi_loader, tif_path)
    except Exception as e:
        raise DemFetchError(f"⚠ GeoTIFF save error: {e}")

    # EPSG:4326 → UTM 自動変換（メートル単位で解析するため）
    ctx.check()
    centre_lon = (lon_min + lon_max) / 2
    centre_lat = (lat_min + lat_max) / 2
    ctx.report("Reprojecting to UTM...")
    utm_path = reproject(tif_path, centre_lon, centre_lat)
    if utm_path != tif_path:
        ctx.created_files.append(utm_path)

    # 標準 DEMLoader で読み直す
    ctx.check()
    dem_loader = DEMLoader()
    dem_loader.load(utm_path)
    return {"loader": dem_loader, "tif_path": utm_path}


def _cancel_aware_progress(ctx, fmt):
    """resolve_years 等の progress_cb(done, total) 用: 中止を確かめてから文言を出す。"""
    def _cb(done, total):
        ctx.check()
        ctx.report(fmt.format(done=done, total=total))
    return _cb


def _download_progress(ctx, prefix):
    """download_* の progress_cb(downloaded, total) 用（_download が 0.15秒ごとに呼ぶ）。"""
    def _cb(downloaded, total):
        if total > 0:
            ctx.report(f"{prefix}{downloaded / 1e6:.0f}/{total / 1e6:.0f}MB…")
        else:
            ctx.report(f"{prefix}{downloaded / 1e6:.0f}MB…")
    return _cb


def fetch_vs_grid(ctx, codes, out_dir):
    """Virtual Shizuoka LP/Grid タイル（codes）を S3 から取得して DEM にする。

    戻り値: {"loader": DEMLoader, "tif_path": str, "resolved": {code: year}}
    """
    from .terrain.dem_loader import DEMLoader
    from .terrain.remote_zip import Cancelled
    from .vs_lp import download_grid_tif, merge_tifs, resolve_years

    ctx.watch_dir(out_dir)
    ctx.report(f"Resolving {len(codes)} tile(s)...")
    resolved = resolve_years(
        codes, progress_cb=_cancel_aware_progress(ctx, "Checking S3… {done}/{total}"))
    if not resolved:
        raise DemFetchError("⚠ No VS LP Grid tiles available for this area.")

    tif_paths = []
    errors = []
    n = len(resolved)
    for i, (code, year) in enumerate(resolved.items()):
        ctx.check()
        ctx.report(f"Downloading {i + 1}/{n}…")
        try:
            tif_paths.append(download_grid_tif(
                code, year, out_dir, cancel_cb=ctx.cancelled,
                progress_cb=_download_progress(ctx, f"Downloading {i + 1}/{n}: "),
            ))
        except Cancelled:
            raise DemFetchCancelled()
        except Exception as e:
            errors.append(f"{code}: {e}")

    if not tif_paths:
        raise DemFetchError("⚠ All tile downloads failed.\n" + "\n".join(errors[:3]))

    ctx.check()
    ctx.report("Processing…")
    if len(tif_paths) == 1:
        tif_path = tif_paths[0]
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        tif_path = os.path.join(out_dir, f"vs_grid_{ts}.tif")
        merge_tifs(tif_paths, tif_path)

    dem_loader = DEMLoader()
    dem_loader.load(tif_path)
    return {"loader": dem_loader, "tif_path": tif_path, "resolved": resolved}


def fetch_vs_dsm(ctx, codes, out_dir):
    """Virtual Shizuoka LP（LP/Original 優先、無ければ LP/Ground）の LAS を取得し、
    DSM（最大値グリッド）にする。成功したら、今回使わない古い LAS・DSM TIF を消す。

    戻り値: {"loader": DEMLoader, "tif_path": str, "resolved": {code: year},
             "las_paths": [str], "las_urls": {basename: url}}
    """
    from .terrain.dem_loader import DEMLoader
    from .terrain.remote_zip import Cancelled
    from .vs_lp import BUCKET_URL, download_las, las_to_dsm, merge_tifs, resolve_years

    ctx.watch_dir(out_dir)
    ctx.report(f"Resolving {len(codes)} tile(s)...")
    _prog = _cancel_aware_progress(ctx, "Checking S3… {done}/{total}")
    resolved_orig = resolve_years(codes, progress_cb=_prog, lp_type="Original")
    remaining = codes - set(resolved_orig.keys())
    resolved_ground = (resolve_years(remaining, progress_cb=_prog, lp_type="Ground")
                       if remaining else {})
    resolved = {**resolved_orig, **resolved_ground}
    resolved_lp_type = {c: "Original" for c in resolved_orig}
    resolved_lp_type.update({c: "Ground" for c in resolved_ground})
    if not resolved:
        raise DemFetchError("⚠ No VS LP tiles available for this area.")

    dsm_tifs = []
    las_paths = []
    las_urls = {}  # {basename: S3 ZIP URL}
    n = len(resolved)
    for i, (code, year) in enumerate(resolved.items()):
        ctx.check()
        ctx.report(f"DSM {i + 1}/{n} — downloading LAS…")
        try:
            lp_type = resolved_lp_type[code]
            las_path = download_las(
                code, year, out_dir, lp_type=lp_type,
                cancel_cb=ctx.cancelled,
                progress_cb=_download_progress(ctx, f"DSM {i + 1}/{n} — downloading LAS: "),
            )
            las_paths.append(las_path)
            folder = code[2:4]
            xx = code[4:6]
            las_urls[os.path.basename(las_path)] = (
                f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
            )
            ctx.check()
            ctx.report(f"DSM {i + 1}/{n} — converting LAS…")
            dsm_tif = os.path.join(out_dir, f"{code}_dsm.tif")
            las_to_dsm(las_path, dsm_tif)
            dsm_tifs.append(dsm_tif)
        except Cancelled:
            raise DemFetchCancelled()
        except (DemFetchCancelled, DemFetchError):
            raise
        except Exception as e:
            raise DemFetchError(f"Error ({code}): {e}")

    if not dsm_tifs:
        raise DemFetchError("⚠ All DSM downloads failed.")

    ctx.check()
    if len(dsm_tifs) == 1:
        tif_path = dsm_tifs[0]
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        tif_path = os.path.join(out_dir, f"vs_dsm_{ts}.tif")
        merge_tifs(dsm_tifs, tif_path)

    dsm_loader = DEMLoader()
    dsm_loader.load(tif_path)
    ctx.check()

    # 今回使用しない古い LAS・DSM TIF を削除
    current_las = {os.path.normpath(p) for p in las_paths}
    current_dsm = {os.path.normpath(p) for p in dsm_tifs}
    for fname in os.listdir(out_dir):
        fpath = os.path.normpath(os.path.join(out_dir, fname))
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext == ".las" and fpath not in current_las:
            try:
                os.remove(fpath)
            except OSError:
                pass
        elif ext == ".tif" and fpath not in current_dsm and fpath != os.path.normpath(tif_path):
            try:
                os.remove(fpath)
            except OSError:
                pass

    return {"loader": dsm_loader, "tif_path": tif_path, "resolved": resolved,
            "las_paths": las_paths, "las_urls": las_urls}


# ── 長野県（砂防課 DEM・林務部 DEM・DCHM → DSM）────────────────────────────

def _cancel_exceptions():
    """取得モジュールが中止時に投げる例外（remote_zip / sevenz_reader）。"""
    from .terrain.remote_zip import Cancelled as _RzCancelled
    from .terrain.sevenz_reader import Cancelled as _SzCancelled
    return (_RzCancelled, _SzCancelled)


def _tile_progress(ctx, prefix, place_fmt="{}"):
    """長野の download_tile_tif の progress_cb(phase, place, detail) 用。"""
    def _cb(phase, place, detail):
        where = place_fmt.format(place)
        if phase == "checking":
            ctx.report(f"{prefix}: checking {where}…")
        else:
            downloaded, total = detail
            ctx.report(f"{prefix}: downloading {downloaded / 1e6:.0f}/{total / 1e6:.0f}MB from {where}…")
    return _cb


def fetch_nagano_tiles(ctx, codes, out_dir, download, merged_prefix):
    """長野県の DEM タイル（砂防課・林務部 共通）を取得して DEM にする。

    download      : nagano_sabo / nagano_rinmu の download_tile_tif
    merged_prefix : 複数タイルを結合したときのファイル名の先頭
    戻り値: {"loader": DEMLoader, "tif_path": str, "n_tiles": int, "missing": [code]}
    """
    from .terrain.dem_loader import DEMLoader
    from .vs_lp import merge_tifs

    cancel_excs = _cancel_exceptions()
    ctx.watch_dir(out_dir)
    tif_paths = []
    missing = []
    n = len(codes)
    for i, code in enumerate(codes):
        ctx.check()
        prefix = f"Tile {i + 1}/{n} ({code})"
        ctx.report(f"{prefix}: locating…")
        try:
            path = download(code, out_dir, cancel_cb=ctx.cancelled,
                            progress_cb=_tile_progress(ctx, prefix))
        except cancel_excs:
            raise DemFetchCancelled()
        # 中止されると None で戻ることもあるので、見つからなかった扱いにする前に確かめる
        ctx.check()
        if path:
            tif_paths.append(path)
        else:
            missing.append(code)

    if not tif_paths:
        raise DemFetchError("⚠ No tiles could be fetched for this area.")

    ctx.report("Processing…")
    if len(tif_paths) == 1:
        tif_path = tif_paths[0]
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        tif_path = os.path.join(out_dir, f"{merged_prefix}_{ts}.tif")
        merge_tifs(tif_paths, tif_path)

    dem_loader = DEMLoader()
    dem_loader.load(tif_path)
    return {"loader": dem_loader, "tif_path": tif_path,
            "n_tiles": len(tif_paths), "missing": missing}


def _bounds_from_ds(ds):
    gt = ds.GetGeoTransform()
    xmin = gt[0]
    ymax = gt[3]
    xmax = xmin + ds.RasterXSize * gt[1]
    ymin = ymax + ds.RasterYSize * gt[5]
    return xmin, ymin, xmax, ymax


def _covers(outer, inner, tol=0.5):
    return (
        outer[0] <= inner[0] + tol
        and outer[1] <= inner[1] + tol
        and outer[2] >= inner[2] - tol
        and outer[3] >= inner[3] - tol
    )


def _validate_dchm_tile(code, path):
    from osgeo import gdal
    from .nagano_sabo import tile_bbox as nagano_tile_bbox

    ds = gdal.Open(path)
    if ds is None or ds.RasterXSize <= 0 or ds.RasterYSize <= 0:
        return False, "cannot be opened"
    band = ds.GetRasterBand(1)
    if band is None:
        ds = None
        return False, "has no raster band"
    # Read the corners lightly; this catches many truncated/corrupt GeoTIFF caches.
    if band.ReadRaster(0, 0, 1, 1) is None:
        ds = None
        return False, "cannot read raster data"
    if band.ReadRaster(ds.RasterXSize - 1, ds.RasterYSize - 1, 1, 1) is None:
        ds = None
        return False, "cannot read raster data"
    actual = _bounds_from_ds(ds)
    ds = None
    expected = nagano_tile_bbox(code)
    if not _covers(actual, expected):
        return False, "does not cover the expected tile extent"
    return True, ""


def fetch_nagano_dchm_dsm(ctx, codes, dchm_out_dir, dsm_out_dir, dem_tif_path):
    """DCHM（樹冠高）タイルを取得・検査し、全部そろえば DEM に加算して DSM を作る。

    戻り値（status で分かれる。取れなかったときの Retry 等の窓はメインスレッドで出す）:
      {"status": "incomplete", "missing": [code], "invalid": [str], "invalid_paths": [str]}
      {"status": "failed", "error": str}            DSM の計算に失敗（DEM はそのまま使う）
      {"status": "ok", "loader": DEMLoader, "dsm_path": str, "n_dchm": int}
    """
    from osgeo import gdal
    from .nagano_dchm import download_tile_tif as download_dchm_tile
    from .terrain.dem_loader import DEMLoader
    from .vs_lp import merge_tifs

    cancel_excs = _cancel_exceptions()
    ctx.watch_dir(dchm_out_dir)
    ctx.watch_dir(dsm_out_dir)

    codes = list(dict.fromkeys(codes))
    dchm_paths = []
    missing = []
    invalid = []
    invalid_paths = []
    n = len(codes)
    for i, code in enumerate(codes):
        ctx.check()
        prefix = f"DCHM {i + 1}/{n} ({code})"
        ctx.report(f"{prefix}: locating…")
        try:
            path = download_dchm_tile(code, dchm_out_dir, cancel_cb=ctx.cancelled,
                                      progress_cb=_tile_progress(ctx, prefix, "sheet {}"))
        except cancel_excs:
            raise DemFetchCancelled()
        ctx.check()
        if not path:
            missing.append(code)
            continue
        ok, reason = _validate_dchm_tile(code, path)
        if ok:
            dchm_paths.append(path)
        else:
            invalid.append(f"{code}: {reason}")
            invalid_paths.append(path)

    if len(dchm_paths) != len(codes):
        return {"status": "incomplete", "missing": missing, "invalid": invalid,
                "invalid_paths": invalid_paths}

    ctx.report("Merging DCHM…")
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if len(dchm_paths) == 1:
        dchm_merged = dchm_paths[0]
    else:
        dchm_merged = os.path.join(dchm_out_dir, f"dchm_{ts}.tif")
        merge_tifs(dchm_paths, dchm_merged)

    ctx.check()
    ctx.report("Computing DSM = DEM + DCHM…")
    try:
        dem_ds = gdal.Open(dem_tif_path)
        if dem_ds is None:
            raise RuntimeError("DEM cannot be opened")
        dem_gt = dem_ds.GetGeoTransform()
        dem_wkt = dem_ds.GetProjection()
        w, h = dem_ds.RasterXSize, dem_ds.RasterYSize
        dem_bounds = _bounds_from_ds(dem_ds)
        dem_band = dem_ds.GetRasterBand(1)
        dem_nodata = dem_band.GetNoDataValue()
        dem_arr = dem_band.ReadAsArray().astype(np.float64)
        dem_ds = None

        merged_ds = gdal.Open(dchm_merged)
        if merged_ds is None:
            raise RuntimeError("Merged DCHM cannot be opened")
        dchm_bounds = _bounds_from_ds(merged_ds)
        merged_ds = None
        if not _covers(dchm_bounds, dem_bounds):
            raise RuntimeError("Merged DCHM does not cover the DEM extent")

        # DCHM を DEM と同じグリッド(範囲・解像度・CRS)にワープしてから加算
        dchm_ds = gdal.Warp(
            "", dchm_merged, format="MEM",
            outputBounds=(dem_gt[0], dem_gt[3] + h * dem_gt[5], dem_gt[0] + w * dem_gt[1], dem_gt[3]),
            width=w, height=h, dstSRS=dem_wkt,
        )
        if dchm_ds is None:
            raise RuntimeError("DCHM warp failed")
        dchm_band = dchm_ds.GetRasterBand(1)
        dchm_nodata = dchm_band.GetNoDataValue()
        dchm_arr = dchm_band.ReadAsArray().astype(np.float64)
        dchm_ds = None

        if dchm_nodata is not None:
            dchm_arr = np.where(dchm_arr == dchm_nodata, 0.0, dchm_arr)
        dchm_arr = np.where(np.isnan(dchm_arr), 0.0, dchm_arr)
        dchm_arr = np.where(dchm_arr < 0, 0.0, dchm_arr)  # 樹冠高の負値はノイズとして0扱い

        dsm_arr = dem_arr + dchm_arr
        if dem_nodata is not None:
            dsm_arr = np.where(dem_arr == dem_nodata, dem_nodata, dsm_arr)

        os.makedirs(dsm_out_dir, exist_ok=True)
        dsm_path = os.path.join(dsm_out_dir, f"nagano_dsm_{ts}.tif")

        driver = gdal.GetDriverByName("GTiff")
        out_ds = driver.Create(dsm_path, w, h, 1, gdal.GDT_Float32,
                               options=["COMPRESS=LZW", "TILED=YES"])
        out_ds.SetGeoTransform(dem_gt)
        out_ds.SetProjection(dem_wkt)
        out_band = out_ds.GetRasterBand(1)
        if dem_nodata is not None:
            out_band.SetNoDataValue(dem_nodata)
        out_band.WriteArray(dsm_arr.astype(np.float32))
        out_band.FlushCache()
        out_ds = None
    except Exception as e:
        return {"status": "failed", "error": f"⚠ DSM computation failed: {e}"}

    ctx.check()
    dsm_loader = DEMLoader()
    dsm_loader.load(dsm_path)
    return {"status": "ok", "loader": dsm_loader, "dsm_path": dsm_path,
            "n_dchm": len(dchm_paths)}


# ── 書き出し（CS Map / VS → WODMI ZIP）──────────────────────────────────────

def cs_map_compute(ctx, src_path, src_loader, resample, resample_threshold, resample_target):
    """CS Map の前半: DEM 全体を読み込み、検査・リサンプルして CS Map（RGBA）を作る。

    src_path   : 読み込む DEM ファイル（画面側とは別に開く）。無ければ src_loader を使う
    src_loader : 読み込み済み（data のある）DEM。src_path が無いときだけ使う
    resample   : (loader, target) -> loader（dockwidget の _resample_dem）
    戻り値: {"dem": DEM（data/gt/crs_wkt/cell_size）, "rgba": ndarray}
    """
    from .terrain import cs_map as cm
    from .terrain.dem_loader import DEMLoader

    ctx.report("Reading DEM...")
    if src_path:
        try:
            loader = DEMLoader()
            loader.load(src_path)
        except Exception as exc:
            raise DemFetchError(f"CS MAP DEM read failed: {exc}")
    else:
        loader = src_loader
    if getattr(loader, "data", None) is None:
        raise DemFetchError("DEM has no raster data.")
    if np.all(np.isnan(loader.data)):
        raise DemFetchError("DEM has no valid elevation values.")
    ctx.check()
    if loader.cell_size < resample_threshold:
        ctx.report(f"Resampling {loader.cell_size:.3f}m -> {resample_target}m ...")
        loader = resample(loader, resample_target)
    ctx.check()
    ctx.report("Creating CS MAP...")
    rgba = cm.compute_cs_map(loader.data, loader.cell_size)
    return {"dem": loader, "rgba": rgba}


def cs_map_save(ctx, rgba, dem, export_dir, name, tags):
    """CS Map の後半: GeoTIFF に保存し、来歴のメタデータ（tags）を書き込む。戻り値: path"""
    from osgeo import gdal
    from .terrain import result_writer as rw

    ctx.check()
    ctx.report("Saving CS MAP...")
    ctx.created_files.append(os.path.join(export_dir, f"{name}.tif"))
    path = rw.save_rgba_raster(rgba, dem.gt, dem.crs_wkt, export_dir, name, overwrite=True)
    if not os.path.exists(path) or os.path.getsize(path) <= 0:
        raise DemFetchError(f"CS MAP export error: CS MAP file was not written: {path}")
    # 失敗してもラスタ本体は書き出し済みなので、メタデータの失敗は無視する
    try:
        ds = gdal.Open(path, gdal.GA_Update)
        if ds is not None:
            for key, value in tags.items():
                ds.SetMetadataItem(key, value)
            ds.FlushCache()
        ds = None
    except Exception:  # nosec B110
        pass
    return path


def _next_wodmi_zip_path(zip_dir):
    """WODMI 用 ZIP の書き出し先（FOL_YYYYMMDD[_n]-all.zip）を決める。(path, name)"""
    os.makedirs(zip_dir, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y%m%d")
    prefix = f"FOL_{today}"
    existing = [f for f in os.listdir(zip_dir)
                if f.startswith(prefix) and f.endswith("-all.zip")]
    seq = len(existing) + 1
    zip_name = f"{prefix}-all.zip" if seq == 1 else f"{prefix}_{seq}-all.zip"
    return os.path.join(zip_dir, zip_name), zip_name


# 点群を使わないソースの README_LAS_LINKS.txt
_LAS_NOT_USED_README = (
    "このDEMソースは点群(LAS)データを使用していません。",
    "そのためLASファイルの参照情報はありません。",
)


def vs_resolve_ortho(ctx, codes):
    """VS の書き出しの前に、LP/Ortho のある年度を調べる。戻り値: {code: year}"""
    from .vs_lp import resolve_years
    ctx.report("Checking Ortho…")
    if not codes:
        return {}
    return resolve_years(codes, lp_type="Ortho",
                         progress_cb=_cancel_aware_progress(ctx, "Checking Ortho… {done}/{total}"))


def vs_export_zip(ctx, resolved_ortho, tmp_dir, zip_dir, dtm_path, dsm_path,
                  las_paths, las_urls, las_capable):
    """LP/Ortho を取得し、DTM/DSM/Ortho と LAS の参照を WODMI ZIP に書き出す。

    戻り値: {"zip_path": str, "zip_name": str, "has_dsm": bool, "has_ortho": bool}
    ZIP の作成に失敗したら DemFetchError("ZIP creation failed:\n...")。
    """
    import json
    import shutil
    import zipfile
    from .terrain.remote_zip import Cancelled
    from .vs_lp import download_grid_tif, merge_tifs

    # ── Ortho (LP/Ortho) ─────────────────────────────────────
    ortho_path = None
    if resolved_ortho:
        ortho_tifs = []
        n = len(resolved_ortho)
        for i, (code, year) in enumerate(resolved_ortho.items()):
            ctx.check()
            ctx.report(f"Ortho {i + 1}/{n}…")
            try:
                ortho_tifs.append(download_grid_tif(
                    code, year, tmp_dir, "Ortho", cancel_cb=ctx.cancelled,
                    progress_cb=_download_progress(ctx, f"Ortho {i + 1}/{n}: "),
                ))
            except Cancelled:
                raise DemFetchCancelled()
            except Exception:  # nosec B110  取れなかったタイルは飛ばす（今までどおり）
                pass
        if ortho_tifs:
            ctx.check()
            ctx.report("Merging Ortho…")
            ortho_path = os.path.join(tmp_dir, "odm_orthophoto.tif")
            if len(ortho_tifs) == 1:
                shutil.copy2(ortho_tifs[0], ortho_path)
            else:
                merge_tifs(ortho_tifs, ortho_path)

    # ── ZIP 作成 ──────────────────────────────────────────────
    ctx.check()
    zip_path, zip_name = _next_wodmi_zip_path(zip_dir)

    ctx.report("Creating ZIP…")
    ctx.created_files.append(zip_path)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dtm_path, "odm_dem/dtm.tif")
            if dsm_path:
                zf.write(dsm_path, "odm_dem/dsm.tif")
            if ortho_path:
                zf.write(ortho_path, "odm_orthophoto/odm_orthophoto.tif")

            # las_sources.json は「実LASの参照」だけでなく「FOL Export由来である
            # ことのマーカー」も兼ねる（webodm_importer側 asset_detector.is_fol_source
            # がこのファイルの有無だけでFOL由来か判定し、Forest Naturalness Index
            # 等のUI表示可否に使う）。そのため実LASが無いソース（長野等）でも
            # las配列を空にして必ず書き出す。中身（lasが空かどうか）と、
            # ファイルの有無（FOL由来かどうか）は別の意味を持つので混同しない。
            las_sources = {"las": []}
            if not las_capable:
                readme_lines = list(_LAS_NOT_USED_README)
            elif not las_paths:
                readme_lines = [
                    "LASファイルは取得されませんでした（DSM未読込、または",
                    "DSM生成時にLASが見つからなかったため）。",
                    "点群を参照したい場合は先にDSMを読み込んでから再度Exportしてください。",
                ]
            else:
                for las_path in las_paths:
                    fname = os.path.basename(las_path)
                    rel = os.path.relpath(las_path, zip_dir)
                    las_sources["las"].append({
                        "filename": fname,
                        "relative": rel,
                    })
                readme_lines = [
                    "このZIPにLASファイルは含まれていません。",
                    "LASファイルはlas_sources.jsonの相対パスで参照されます。",
                    "",
                    "LASファイルの入手先（ダウンロードURL）:",
                ]
                for p in las_paths:
                    fname = os.path.basename(p)
                    url = las_urls.get(fname, "（URL不明）")
                    readme_lines.append(f"  {fname}")
                    readme_lines.append(f"    {url}")
                readme_lines += [
                    "",
                    "注意: LASファイルを移動した場合はlas_sources.jsonの",
                    "relativeパスを修正するか、上記URLから再取得してください。",
                ]
            zf.writestr("las_sources.json",
                        json.dumps(las_sources, ensure_ascii=False, indent=2))
            zf.writestr("README_LAS_LINKS.txt",
                        "\n".join(readme_lines))
    except Exception as e:
        raise DemFetchError(f"ZIP creation failed:\n{e}")
    ctx.check()
    return {"zip_path": zip_path, "zip_name": zip_name,
            "has_dsm": bool(dsm_path), "has_ortho": bool(ortho_path)}


# ── 海外の公開 DEM / DSM（オランダ AHN・フランス IGN LiDAR HD・ニュージーランド LINZ）──
# どれもログイン不要。キャンバス範囲（各ソースの座標系）で取得し、1枚の GeoTIFF にする。
#   kind="ogc"  : WCS / WMS に範囲を区切って順に問い合わせ、つなぐ（進捗・中止のため）
#   kind="nzcog": AWS 上の COG タイル（Topo50 図郭）の必要な部分だけを読んでつなぐ

_AHN_WCS = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?service=WCS&request=GetCoverage"
            "&version=2.0.1&coverageId={layer}&subset=x({x0},{x1})&subset=y({y0},{y1})"
            "&format=image/tiff")
_IGN_WMSR = ("https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
             "&LAYERS={layer}&STYLES=&CRS=EPSG:2154&BBOX={x0},{y0},{x1},{y1}"
             "&WIDTH={w}&HEIGHT={h}&FORMAT=image/geotiff")
_NZ_BASE = "https://nz-elevation.s3.ap-southeast-2.amazonaws.com/new-zealand/new-zealand/dem_1m/2193/"

# センチネル値（DemBrowserDialog と同じ文字列）→ 取得の仕様
REMOTE_SOURCES = {
    "__AHN_DTM__": dict(kind="ogc", url=_AHN_WCS, layer="dtm_05m", epsg=28992, cell=0.5,
                        chunk_px=1000, label="AHN4 DTM 0.5m", prefix="ahn_dtm"),
    "__AHN_DSM__": dict(kind="ogc", url=_AHN_WCS, layer="dsm_05m", epsg=28992, cell=0.5,
                        chunk_px=1000, label="AHN4 DSM 0.5m", prefix="ahn_dsm"),
    "__IGN_LIDARHD_MNT__": dict(kind="ogc", url=_IGN_WMSR,
                                layer="IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
                                epsg=2154, cell=0.5, chunk_px=2000,
                                label="IGN LiDAR HD MNT 0.5m", prefix="ign_lidarhd_mnt"),
    "__IGN_LIDARHD_MNS__": dict(kind="ogc", url=_IGN_WMSR,
                                layer="IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
                                epsg=2154, cell=0.5, chunk_px=2000,
                                label="IGN LiDAR HD MNS 0.5m", prefix="ign_lidarhd_mns"),
    "__LINZ_DEM_1M__": dict(kind="nzcog", epsg=2193, cell=1.0,
                            label="LINZ NZ DEM 1m", prefix="linz_dem"),
}

# 1回の取得で扱う最大画素数（範囲が広すぎるときは中止を促す）
_REMOTE_MAX_PIXELS = 100_000_000


def _http_download(ctx, url, dest, timeout=180, accept=("tif",)):
    """url を dest に保存する（64KB ごとに中止を確かめる）。

    Content-Type に accept のどれも含まれなければ（サービスのエラー応答など）DemFetchError。
    """
    import urllib.request
    if not url.startswith("https://"):
        raise DemFetchError(f"⚠ Invalid URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Forestry Operations Lite (QGIS plugin)"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:  # nosec B310
        ctype = r.headers.get("Content-Type", "")
        while True:
            ctx.check()
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
    if not any(a in ctype.lower() for a in accept):
        try:
            with open(dest, "rb") as f:
                head = f.read(300).decode("utf-8", "replace")
        except OSError:
            head = ""
        what = "elevation data" if accept == ("tif",) else "the requested data"
        raise DemFetchError(f"⚠ The service returned an error instead of {what}: {head}")


def _snap_bbox(bbox, cell):
    """範囲をセルの格子に合わせる（外側へ広げる）。"""
    import math
    x0, y0, x1, y1 = bbox
    return (math.floor(x0 / cell) * cell, math.floor(y0 / cell) * cell,
            math.ceil(x1 / cell) * cell, math.ceil(y1 / cell) * cell)


def _finish_mosaic(ctx, sources, out_dir, prefix, epsg, bbox=None, cell=None):
    """sources（GeoTIFF / VSI パスの一覧）を1枚の GeoTIFF にする。全部 NoData なら範囲外。

    座標系は epsg で付け直す。IGN の WMS-R が返す GeoTIFF は EPSG コードの無い
    不完全な定義（楕円体も WGS84）で、そのままだと QGIS で座標系不明になるため。
    """
    from osgeo import gdal
    from .terrain.dem_loader import DEMLoader

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    tif_path = os.path.join(out_dir, f"{prefix}_{ts}.tif")
    ctx.created_files.append(tif_path)
    vrt = gdal.BuildVRT("", sources)
    if vrt is None:
        raise DemFetchError("⚠ Could not combine the downloaded tiles.")

    def _cb(complete, _msg, _data):
        ctx.report(f"Reading elevation data… {int(complete * 100)}%")
        return 0 if ctx.cancelled() else 1

    opts = dict(format="GTiff", creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
                outputSRS=f"EPSG:{epsg}", callback=_cb)
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        opts.update(projWin=[x0, y1, x1, y0])
    out = gdal.Translate(tif_path, vrt, **opts)
    vrt = None
    ctx.check()
    if out is None:
        raise DemFetchError("⚠ Could not write the elevation GeoTIFF.")
    band = out.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    arr = band.ReadAsArray()
    out = None
    valid = np.isfinite(arr)
    if nodata is not None:
        valid &= arr != nodata
    # 0.5m の格子で float32 の最大値（AHN の NoData）も無効として扱う
    valid &= np.abs(arr) < 1e30
    if not valid.any():
        raise DemFetchError("⚠ No elevation data for this area (outside the source coverage).")

    dem_loader = DEMLoader()
    dem_loader.load(tif_path)
    return {"loader": dem_loader, "tif_path": tif_path}


def _fetch_ogc(ctx, spec, bbox, out_dir):
    """WCS / WMS に範囲を区切って問い合わせ、つないで1枚にする。"""
    cell = spec["cell"]
    x0, y0, x1, y1 = _snap_bbox(bbox, cell)
    w_px = int(round((x1 - x0) / cell))
    h_px = int(round((y1 - y0) / cell))
    if w_px <= 0 or h_px <= 0:
        raise DemFetchError("⚠ The preview extent is empty.")
    if w_px * h_px > _REMOTE_MAX_PIXELS:
        raise DemFetchError(
            f"⚠ Extent too large ({w_px:,} × {h_px:,} px at {cell} m). Zoom in and try again.")
    step = spec["chunk_px"] * cell
    xs = []
    x = x0
    while x < x1 - 1e-9:
        xs.append((x, min(x + step, x1)))
        x += step
    ys = []
    y = y0
    while y < y1 - 1e-9:
        ys.append((y, min(y + step, y1)))
        y += step
    parts_dir = os.path.join(out_dir, f".parts_{spec['prefix']}")
    os.makedirs(parts_dir, exist_ok=True)
    ctx.watch_dir(parts_dir)
    paths = []
    n = len(xs) * len(ys)
    k = 0
    try:
        for cy0, cy1 in ys:
            for cx0, cx1 in xs:
                ctx.check()
                k += 1
                ctx.report(f"Fetching {spec['label']}… {k}/{n}")
                url = spec["url"].format(
                    layer=spec["layer"], x0=cx0, y0=cy0, x1=cx1, y1=cy1,
                    w=int(round((cx1 - cx0) / cell)), h=int(round((cy1 - cy0) / cell)))
                dest = os.path.join(parts_dir, f"part_{k:04d}.tif")
                _http_download(ctx, url, dest)
                paths.append(dest)
        ctx.report("Combining…")
        return _finish_mosaic(ctx, paths, out_dir, spec["prefix"], spec["epsg"])
    finally:
        # 区切って取った部分は、成否に関わらず残さない
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(parts_dir)
        except OSError:
            pass


_TOPO50_LETTERS = "".join(c for c in string.ascii_uppercase if c not in "IO")  # I と O を除く


def _nz_tile_names(bbox):
    """NZTM2000（EPSG:2193）の範囲に掛かる Topo50 図郭名（例 BP35）。

    図郭は 24km × 36km。列 = (xmin - 988000) / 24000、
    行 = AS を 0 とした2文字の並び順（I・O を除く）で ymax = 6234000 - 行 × 36000。
    """
    import math
    x0, y0, x1, y1 = bbox
    c0 = int(math.floor((x0 - 988000) / 24000))
    c1 = int(math.floor((x1 - 988000 - 1e-6) / 24000))
    r0 = int(math.floor((6234000 - y1) / 36000))
    r1 = int(math.floor((6234000 - y0 - 1e-6) / 36000))
    base = _TOPO50_LETTERS.index("A") * 24 + _TOPO50_LETTERS.index("S")
    names = []
    for r in range(r0, r1 + 1):
        code = base + r
        if code < 0:
            continue
        a, b = divmod(code, 24)
        if a >= len(_TOPO50_LETTERS):
            continue
        for c in range(c0, c1 + 1):
            if 0 <= c <= 99:
                names.append(f"{_TOPO50_LETTERS[a]}{_TOPO50_LETTERS[b]}{c:02d}")
    return names


def gdal_has_lerc():
    """この GDAL が LERC 圧縮の GeoTIFF を読めるか（LINZ の COG は LERC 圧縮）。

    GDAL のビルドによっては LERC が入っていない（例: Flatpak 版 QGIS）。
    """
    from osgeo import gdal
    drv = gdal.GetDriverByName("GTiff")
    opts = drv.GetMetadataItem("DMD_CREATIONOPTIONLIST") if drv is not None else None
    return bool(opts) and "<Value>LERC</Value>" in opts


LERC_MISSING_MESSAGE = (
    "⚠ This QGIS's GDAL cannot read LERC-compressed GeoTIFFs, which the LINZ data uses. "
    "Use a QGIS build whose GDAL supports LERC.")


def _nz_available_tiles(ctx):
    """全国 1m DEM の collection.json から、実在するタイル名の集合を得る。"""
    import json
    import urllib.request
    ctx.report("Checking LINZ tile index…")
    req = urllib.request.Request(_NZ_BASE + "collection.json",
                                 headers={"User-Agent": "Forestry Operations Lite (QGIS plugin)"})
    with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
        col = json.loads(r.read().decode("utf-8"))
    return {link["href"][2:-5] for link in col.get("links", [])
            if link.get("rel") == "item" and link.get("href", "").startswith("./")}


def _fetch_nzcog(ctx, spec, bbox, out_dir):
    """LINZ の COG タイルの必要な部分だけを読み、1枚にする。"""
    from osgeo import gdal
    cell = spec["cell"]
    bbox = _snap_bbox(bbox, cell)
    w_px = int(round((bbox[2] - bbox[0]) / cell))
    h_px = int(round((bbox[3] - bbox[1]) / cell))
    if w_px * h_px > _REMOTE_MAX_PIXELS:
        raise DemFetchError(
            f"⚠ Extent too large ({w_px:,} × {h_px:,} px at {cell} m). Zoom in and try again.")
    if not gdal_has_lerc():
        raise DemFetchError(LERC_MISSING_MESSAGE)
    wanted = _nz_tile_names(bbox)
    available = _nz_available_tiles(ctx)
    tiles = [n for n in wanted if n in available]
    if not tiles:
        raise DemFetchError("⚠ No elevation data for this area (outside the source coverage).")
    ctx.check()
    # S3 上の COG を部分読み出しする（このスレッドだけの設定）
    gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetThreadLocalConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tiff,.tif")
    try:
        sources = [f"/vsicurl/{_NZ_BASE}{n}.tiff" for n in tiles]
        ctx.report(f"Reading {spec['label']} ({len(tiles)} tile(s))…")
        return _finish_mosaic(ctx, sources, out_dir, spec["prefix"], spec["epsg"],
                              bbox=bbox, cell=cell)
    finally:
        gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)
        gdal.SetThreadLocalConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", None)


def fetch_remote_raster(ctx, sentinel, bbox, out_dir):
    """海外の公開 DEM / DSM を bbox（ソースの座標系）で取得する。

    戻り値: {"loader": DEMLoader, "tif_path": str}
    """
    spec = REMOTE_SOURCES[sentinel]
    os.makedirs(out_dir, exist_ok=True)
    if spec["kind"] == "nzcog":
        return _fetch_nzcog(ctx, spec, bbox, out_dir)
    return _fetch_ogc(ctx, spec, bbox, out_dir)


# ── 海外ソースの WebODM Importer 書き出し（オランダ AHN・フランス IGN）──
# DTM・DSM・オルソと、データの一覧・出典（DATA_SOURCES.txt）を ZIP にする。
# 点群は扱わない。las_sources.json は FOL 由来の印なので空のまま必ず書く。
# オルソは DEM の撮影年にできるだけ近い年のものを選び、年が違えば注記する。

_PDOK_ORTHO_WMS = ("https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0?SERVICE=WMS&VERSION=1.3.0"
                   "&REQUEST=GetMap&LAYERS={layer}&STYLES=&CRS=EPSG:28992"
                   "&BBOX={x0},{y0},{x1},{y1}&WIDTH={w}&HEIGHT={h}&FORMAT=image/jpeg")
_IGN_ORTHO_WMS = ("https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
                  "&LAYERS={layer}&STYLES=&CRS=EPSG:2154&BBOX={x0},{y0},{x1},{y1}"
                  "&WIDTH={w}&HEIGHT={h}&FORMAT=image/jpeg")
_IGN_WFS = ("https://data.geopf.fr/wfs/ows?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
            "&TYPENAMES={typename}&OUTPUTFORMAT=application/json&COUNT={count}"
            "&BBOX={s},{w},{n},{e},urn:ogc:def:crs:EPSG::4326")

# AHN4 は地域ごとに 2020〜2022 年に撮影された。データに地点ごとの撮影日は付いて
# いないので、期間で示す。オルソはその中ほどの年（全国を覆う年ごとの版がある）。
# 2021 年は 8cm 版（orthoHR）だけなので、それを 25cm で取る。
_AHN4_YEARS = (2020, 2022)
_NL_ORTHO_YEAR = 2021
_NL_ORTHO_LAYER = "2021_orthoHR"

# IGN BD ORTHO の年ごとの版（ORTHOIMAGERY.ORTHOPHOTOS<年>）。県ごとに撮影年が違う
# ので、年によっては範囲が空（白）になる。HR.ORTHOIMAGERY.ORTHOPHOTOS は最新版。
_IGN_ORTHO_YEARS = range(2016, 2025)
_IGN_ORTHO_LATEST = "HR.ORTHOIMAGERY.ORTHOPHOTOS"
# 撮影日（date_vol）と年（pva）を持つモザイクの継ぎ目のグラフ（年の範囲ごと・最新）
_IGN_ORTHO_GRAPHS = (
    ("ORTHOIMAGERY.ORTHOPHOTOS.GRAPHE.2016-2020:graphe_bdortho", 2016, 2020),
    ("ORTHOIMAGERY.ORTHOPHOTOS.GRAPHE.2021-2023:graphe_bdortho", 2021, 2023),
    ("ORTHOIMAGERY.ORTHOPHOTOS.GRAPHE-MOSAIQUAGE:graphe_bdortho", None, None),
)
_IGN_LICENCE = "Licence Ouverte / Open Licence (Etalab 2.0)"

# オルソの範囲が埋まっているとみなす割合（白くない画素の割合）
_ORTHO_FULL_COVER = 0.98

# DEM のセンチネル → 書き出しの国と、組になる DSM のセンチネル
REMOTE_EXPORT_SOURCES = {
    "__AHN_DTM__": {"country": "nl", "dsm": "__AHN_DSM__"},
    "__IGN_LIDARHD_MNT__": {"country": "fr", "dsm": "__IGN_LIDARHD_MNS__"},
    "__LINZ_DEM_1M__": {"country": "nz", "dsm": None},
}


def _http_json(ctx, url, timeout=60):
    import json
    import urllib.request
    if not url.startswith("https://"):
        raise DemFetchError(f"⚠ Invalid URL: {url}")
    ctx.check()
    req = urllib.request.Request(url, headers={"User-Agent": "Forestry Operations Lite (QGIS plugin)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        return json.loads(r.read().decode("utf-8"))


def _raster_bbox(path):
    """GeoTIFF の範囲 (x0, y0, x1, y1)（ファイルの座標系）。"""
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise DemFetchError(f"⚠ Cannot open {os.path.basename(path)}.")
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    ds = None
    xs = (gt[0], gt[0] + gt[1] * w)
    ys = (gt[3], gt[3] + gt[5] * h)
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_to_wgs84(bbox, epsg):
    """(lon0, lat0, lon1, lat1)"""
    from osgeo import osr
    src = osr.SpatialReference()
    src.ImportFromEPSG(epsg)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src, dst)
    x0, y0, x1, y1 = bbox
    pts = [tr.TransformPoint(x, y)[:2] for x in (x0, x1) for y in (y0, y1)]
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    return (min(lons), min(lats), max(lons), max(lats))


def _wfs_features(ctx, typename, bbox84, count=500):
    lon0, lat0, lon1, lat1 = bbox84
    url = _IGN_WFS.format(typename=typename, count=count, s=lat0, w=lon0, n=lat1, e=lon1)
    return [f.get("properties", {}) for f in _http_json(ctx, url).get("features", [])]


def _day(value):
    """'2022-04-21Z' → '2022-04-21'（無ければ None）"""
    return str(value)[:10] if value else None


def _date_range_text(dates):
    dates = sorted(set(d for d in dates if d))
    if not dates:
        return "unknown"
    if len(dates) <= 3:
        return ", ".join(dates)
    return f"{dates[0]} – {dates[-1]}"


def _ortho_cover(ctx, url_tmpl, layer, bbox):
    """オルソの layer が bbox をどれだけ埋めているか（白くない画素の割合 0..1）。"""
    import urllib.request
    from osgeo import gdal
    x0, y0, x1, y1 = bbox
    w = 256
    h = max(16, int(round(w * (y1 - y0) / max(x1 - x0, 1e-9))))
    url = url_tmpl.format(layer=layer, x0=x0, y0=y0, x1=x1, y1=y1, w=w, h=h)
    ctx.check()
    req = urllib.request.Request(url, headers={"User-Agent": "Forestry Operations Lite (QGIS plugin)"})
    with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
    if "image" not in ctype.lower():
        raise DemFetchError(f"⚠ The Ortho service returned an error: {body[:300].decode('utf-8', 'replace')}")
    mem = f"/vsimem/fol_ortho_probe_{id(body)}.jpg"
    gdal.FileFromMemBuffer(mem, body)
    try:
        ds = gdal.Open(mem)
        arr = ds.ReadAsArray() if ds is not None else None
        ds = None
    finally:
        gdal.Unlink(mem)
    if arr is None or arr.ndim != 3:
        return 0.0
    return float((arr[:3].min(axis=0) < 250).mean())


def _ign_ortho_dates(ctx, bbox84, year):
    """BD ORTHO の year の版の撮影日と県 [(date, dep)]（継ぎ目のグラフから）。"""
    found = []
    for typename, y0, y1 in _IGN_ORTHO_GRAPHS:
        if y0 is not None and not (y0 <= year <= y1):
            continue
        try:
            feats = _wfs_features(ctx, typename, bbox84)
        except DemFetchCancelled:
            raise
        except Exception:  # nosec B112  撮影日が分からなくても書き出しは続ける
            continue
        for p in feats:
            if p.get("pva") == year and p.get("date_vol"):
                found.append((_day(p["date_vol"]), str(p.get("dep", ""))))
    return sorted(set(found))


def _plan_item(entry, label, acquired, source, licence, attribution, service):
    return {"entry": entry, "label": label, "acquired": acquired, "source": source,
            "licence": licence, "attribution": attribution, "service": service}


def _user_dsm_item(dsm_path):
    return _plan_item("odm_dem/dsm.tif", f"{os.path.basename(dsm_path)} (user-provided file)",
                      "unknown", "user-provided", "see the original data", "", "")


# ── ニュージーランド（LINZ）: DEM の元になった測量と、航空写真（nz-imagery の COG）──
# 全国 1m DEM の図郭（Topo50）は、1:10,000 図郭ごとに元の測量（地域・年）を
# derived_from に持つ。航空写真は地域ごと・撮影ごとのコレクションで、
# 240m×360m（1:500 図郭）などのタイルが WebP（可逆）の COG で置かれている。
_NZ_ELEV_BASE = "https://nz-elevation.s3.ap-southeast-2.amazonaws.com/"
_NZ_IMAGERY_BASE = "https://nz-imagery.s3.ap-southeast-2.amazonaws.com/"
# 調べる航空写真のコレクションの数の上限（年の近い順）
_NZ_MAX_COLLECTIONS = 8


def _topo50_sheet_bbox(sheet):
    """Topo50 図郭名（例 BQ31）の範囲 (x0, y0, x1, y1)（EPSG:2193）。"""
    a = _TOPO50_LETTERS.index(sheet[0])
    b = _TOPO50_LETTERS.index(sheet[1])
    r = a * 24 + b - (_TOPO50_LETTERS.index("A") * 24 + _TOPO50_LETTERS.index("S"))
    x0 = 988000 + int(sheet[2:]) * 24000
    y1 = 6234000 - r * 36000
    return (x0, y1 - 36000, x0 + 24000, y1)


def _nz_tile_bbox(name):
    """LINZ のタイル名（例 BQ31_10000_0405、BP31_500_097091）の範囲（EPSG:2193）。

    Topo50 図郭を 50000/縮尺 で縦横に割った、上から row 番目・左から col 番目（1 始まり）。
    """
    sheet, scale, rc = name.split("_")[:3]
    n = 50000 // int(scale)
    k = len(rc) // 2
    row, col = int(rc[:k]), int(rc[k:])
    sx0, _sy0, _sx1, sy1 = _topo50_sheet_bbox(sheet)
    w, h = 24000 / n, 36000 / n
    x0 = sx0 + (col - 1) * w
    y1 = sy1 - (row - 1) * h
    return (x0, y1 - h, x0 + w, y1)


def _overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _slug_years(slug):
    """'wellington_2013-2014' / 'wellington_2021_0.075m' → (2013, 2014) / (2021, 2021)"""
    import re
    m = re.search(r"_(\d{4})(?:-(\d{4}))?(?:_[\d.]+m)?$", slug)
    if not m:
        return None
    y0 = int(m.group(1))
    return (y0, int(m.group(2) or y0))


def _years_text(y):
    return str(y[0]) if y[0] == y[1] else f"{y[0]}–{y[1]}"


def gdal_has_webp():
    """この GDAL が WebP 圧縮の GeoTIFF を読めるか（LINZ の航空写真は WebP）。"""
    from osgeo import gdal
    drv = gdal.GetDriverByName("GTiff")
    opts = drv.GetMetadataItem("DMD_CREATIONOPTIONLIST") if drv is not None else None
    return bool(opts) and "<Value>WEBP</Value>" in opts


def _nz_dem_surveys(ctx, bbox):
    """範囲に掛かる DEM の元の測量 {(地域, 測量名): 重なる面積}。"""
    found = {}
    for sheet in _nz_tile_names(bbox):
        ctx.check()
        try:
            item = _http_json(ctx, f"{_NZ_BASE}{sheet}.json")
        except DemFetchCancelled:
            raise
        except Exception:  # nosec B112  図郭の情報が無ければ飛ばす
            continue
        for link in item.get("links", []):
            if link.get("rel") != "derived_from":
                continue
            parts = link.get("href", "").split("/")
            if len(parts) < 6:
                continue
            region, survey, tile = parts[3], parts[4], parts[-1][:-5]
            try:
                area = _overlap_area(_nz_tile_bbox(tile), bbox)
            except (ValueError, IndexError):
                continue
            if area > 0:
                found[(region, survey)] = found.get((region, survey), 0.0) + area
    return found


def _nz_collection_cover(ctx, href, bbox):
    """航空写真のコレクションのうち範囲に掛かるタイルと、範囲を埋める割合。"""
    col = _http_json(ctx, _NZ_IMAGERY_BASE + href[2:])
    base = _NZ_IMAGERY_BASE + href[2:].rsplit("/", 1)[0] + "/"
    tiles, covered = [], 0.0
    for link in col.get("links", []):
        if link.get("rel") != "item":
            continue
        name = link.get("href", "")[2:-5]
        try:
            area = _overlap_area(_nz_tile_bbox(name), bbox)
        except (ValueError, IndexError):
            continue
        if area > 0:
            tiles.append(f"/vsicurl/{base}{name}.tiff")
            covered += area
    total = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    return col, tiles, (covered / total if total > 0 else 0.0)


def _plan_nz(ctx, bbox, dem_spec, dsm_path, items, notes):
    """ニュージーランドの書き出しの中身。items / notes に足し、オルソの取得の仕様を返す。"""
    ctx.report("Checking acquisition dates…")
    surveys = _nz_dem_surveys(ctx, bbox)
    if surveys:
        ordered = sorted(surveys.items(), key=lambda kv: -kv[1])
        acquired = ", ".join(
            f"{survey} ({_years_text(_slug_years(survey))})" if _slug_years(survey) else survey
            for (_region, survey), _a in ordered)
        main_years = _slug_years(ordered[0][0][1])
        regions = sorted(set(region for (region, _s) in surveys))
    else:
        acquired, main_years, regions = "unknown", None, []
    items.append(_plan_item(
        "odm_dem/dtm.tif", dem_spec["label"], acquired,
        "Toitū Te Whenua Land Information New Zealand (LINZ), New Zealand 1m DEM",
        "CC BY 4.0", "Toitū Te Whenua Land Information New Zealand and the survey licensors",
        "https://nz-elevation.s3.ap-southeast-2.amazonaws.com/"))
    if dsm_path:
        items.append(_user_dsm_item(dsm_path))
    if len(surveys) > 1:
        notes.append("The DTM in this area is built from more than one survey; the orthophoto "
                     f"is matched to the largest one ({ordered[0][0][1]}).")

    if not gdal_has_webp():
        notes.append("⚠ This QGIS's GDAL cannot read WebP-compressed GeoTIFFs, which the LINZ "
                     "aerial imagery uses, so no orthophoto is included.")
        return None
    if not regions:
        notes.append("No orthophoto is available for this area.")
        return None

    # 同じ地域の航空写真（RGB）を、DEM の主な測量の年に近い順に調べる
    ctx.report("Checking Ortho…")
    catalog = _http_json(ctx, _NZ_IMAGERY_BASE + "catalog.json")
    cands = []
    for link in catalog.get("links", []):
        href = link.get("href", "")
        parts = href.split("/")
        if link.get("rel") != "child" or len(parts) < 4 or parts[1] not in regions or parts[3] != "rgb":
            continue
        years = _slug_years(parts[2])
        if years is None:
            continue
        if main_years:
            gap = max(0, years[0] - main_years[1], main_years[0] - years[1])
        else:
            gap = -years[1]  # 年が分からなければ新しい順
        cands.append((gap, -years[1], href, years, link.get("title", parts[2])))
    cands.sort()
    best = None
    for i, (_gap, _ny, href, years, title) in enumerate(cands[:_NZ_MAX_COLLECTIONS]):
        ctx.report(f"Checking Ortho {_years_text(years)}… ({i + 1}/{min(len(cands), _NZ_MAX_COLLECTIONS)})")
        try:
            col, tiles, cover = _nz_collection_cover(ctx, href, bbox)
        except DemFetchCancelled:
            raise
        except Exception:  # nosec B112  読めないコレクションは飛ばす
            continue
        if tiles and (best is None or cover > best[0] + 1e-6):
            best = (cover, col, tiles, years, title)
        if cover >= _ORTHO_FULL_COVER:
            break
    if best is None or best[0] < 0.5:
        notes.append("No orthophoto is available for this area.")
        return None
    cover, col, tiles, years, title = best
    interval = (col.get("extent", {}).get("temporal", {}).get("interval") or [[None, None]])[0]
    days = sorted(set(d for d in (_day(interval[0]), _day(interval[1])) if d))
    o_acq = _date_range_text(days) if days else _years_text(years)
    providers = ", ".join(p.get("name", "") for p in col.get("providers", []) if p.get("name"))
    items.append(_plan_item(
        "odm_orthophoto/odm_orthophoto.tif", f"LINZ {title}, 0.25m", o_acq,
        f"LINZ aerial imagery ({col.get('linz:slug', '')}), via nz-imagery",
        col.get("license", "CC-BY-4.0"), providers or "Toitū Te Whenua Land Information New Zealand",
        _NZ_IMAGERY_BASE))
    if main_years and (years[0] > main_years[1] or years[1] < main_years[0]):
        notes.append(f"⚠ The orthophoto ({_years_text(years)}) and the elevation data "
                     f"({_years_text(main_years)}) were acquired in different years. "
                     "Tree canopy and land cover may differ.")
    if cover < _ORTHO_FULL_COVER:
        notes.append(f"⚠ The orthophoto covers only about {cover:.0%} of the area.")
    return {"kind": "cog", "tiles": tiles, "cell": 0.25, "epsg": 2193}


def remote_export_plan(ctx, dem_sentinel, dsm_sentinel, dtm_path, dsm_path):
    """海外ソースの書き出しの中身（データ・撮影時期・出典、使うオルソ）を決める。

    戻り値の plan: items（ZIP に入る各データの情報）、ortho（取得の仕様、無ければ None）、
    notes（利用者への注記）、bbox / epsg（DTM の範囲と座標系）。
    """
    country = REMOTE_EXPORT_SOURCES[dem_sentinel]["country"]
    dem_spec = REMOTE_SOURCES[dem_sentinel]
    epsg = dem_spec["epsg"]
    bbox = _raster_bbox(dtm_path)
    pair_dsm = dsm_path and dsm_sentinel == REMOTE_EXPORT_SOURCES[dem_sentinel]["dsm"]
    items, notes, ortho = [], [], None

    if country == "nl":
        period = f"{_AHN4_YEARS[0]}–{_AHN4_YEARS[1]} (AHN4 survey period, by region)"
        for entry, spec in (("odm_dem/dtm.tif", dem_spec),
                            ("odm_dem/dsm.tif", REMOTE_SOURCES["__AHN_DSM__"])):
            if entry.endswith("dsm.tif"):
                if not dsm_path:
                    continue
                if not pair_dsm:
                    items.append(_user_dsm_item(dsm_path))
                    continue
            items.append(_plan_item(entry, spec["label"], period,
                                    "AHN (Actueel Hoogtebestand Nederland), via PDOK",
                                    "CC0 1.0", "AHN", "https://service.pdok.nl/rws/ahn/wcs/v1_0"))
        ctx.report("Checking Ortho…")
        layer = _NL_ORTHO_LAYER
        cover = _ortho_cover(ctx, _PDOK_ORTHO_WMS, layer, bbox)
        if cover > 0.5:
            ortho = {"url": _PDOK_ORTHO_WMS, "layer": layer, "cell": 0.25, "chunk_px": 2500,
                     "epsg": epsg}
            items.append(_plan_item(
                "odm_orthophoto/odm_orthophoto.tif",
                f"PDOK Luchtfoto {_NL_ORTHO_YEAR} Ortho 0.25m ({layer})", str(_NL_ORTHO_YEAR),
                "Beeldmateriaal Nederland, via PDOK", "CC BY 4.0", "Beeldmateriaal.nl",
                "https://service.pdok.nl/hwh/luchtfotorgb/wms/v1_0"))
            notes.append(f"AHN4 was surveyed region by region in {_AHN4_YEARS[0]}–{_AHN4_YEARS[1]}; "
                         f"the orthophoto is from {_NL_ORTHO_YEAR}, so the two may differ by up to "
                         "a year or two.")
            if cover < _ORTHO_FULL_COVER:
                notes.append(f"⚠ The orthophoto covers only about {cover:.0%} of the area.")
        else:
            notes.append("No orthophoto is available for this area.")

    elif country == "nz":
        ortho = _plan_nz(ctx, bbox, dem_spec, dsm_path, items, notes)

    else:  # fr
        ctx.report("Checking acquisition dates…")
        bbox84 = _bbox_to_wgs84(bbox, epsg)
        try:
            meta = _wfs_features(ctx, "IGNF_LIDAR-HD_METADONNEE:metadata", bbox84)
        except DemFetchCancelled:
            raise
        except Exception:  # 撮影日が分からなくても書き出しは続ける
            meta = []
        lidar_dates = sorted(set(
            d for p in meta
            for d in (_day(p.get("date_debut_acquisition")), _day(p.get("date_fin_acquisition"))) if d))
        acquired = _date_range_text(lidar_dates)
        dem_year = int(lidar_dates[-1][:4]) if lidar_dates else None
        for entry, spec in (("odm_dem/dtm.tif", dem_spec),
                            ("odm_dem/dsm.tif", REMOTE_SOURCES["__IGN_LIDARHD_MNS__"])):
            if entry.endswith("dsm.tif"):
                if not dsm_path:
                    continue
                if not pair_dsm:
                    items.append(_user_dsm_item(dsm_path))
                    continue
            items.append(_plan_item(entry, spec["label"], acquired, "IGN – LiDAR HD, via Géoplateforme",
                                    _IGN_LICENCE, "IGN – LiDAR HD", "https://data.geopf.fr/wms-r/wms"))

        # DEM の撮影年に近い年から順に、範囲が埋まっている版を探す
        years = sorted(_IGN_ORTHO_YEARS,
                       key=(lambda y: (abs(y - dem_year), -y)) if dem_year else (lambda y: -y))
        best = (0.0, None)
        for i, year in enumerate(years):
            ctx.report(f"Checking Ortho {year}… ({i + 1}/{len(years)})")
            cover = _ortho_cover(ctx, _IGN_ORTHO_WMS, f"ORTHOIMAGERY.ORTHOPHOTOS{year}", bbox)
            if cover > best[0]:
                best = (cover, year)
            if cover >= _ORTHO_FULL_COVER:
                break
        cover, year = best
        layer = f"ORTHOIMAGERY.ORTHOPHOTOS{year}" if year else None
        if cover < 0.5:
            # 年ごとの版で埋まらなければ最新版を使う
            ctx.report("Checking the latest Ortho…")
            latest_cover = _ortho_cover(ctx, _IGN_ORTHO_WMS, _IGN_ORTHO_LATEST, bbox)
            if latest_cover > cover:
                cover, year, layer = latest_cover, None, _IGN_ORTHO_LATEST
        if cover > 0.5:
            ctx.report("Checking Ortho dates…")
            if year is None:  # 最新版: グラフから年を知る
                latest = [p for p in _wfs_features(ctx, _IGN_ORTHO_GRAPHS[-1][0], bbox84) if p.get("pva")]
                year = max((p["pva"] for p in latest), default=None)
            flights = _ign_ortho_dates(ctx, bbox84, year) if year else []
            deps = sorted(set(dep for _d, dep in flights if dep))
            o_acq = _date_range_text([d for d, _dep in flights]) if flights else (str(year) if year else "unknown")
            if deps:
                o_acq += f" (département {', '.join(deps)})"
            # 元は 20cm。DEM の 0.5m 格子と端が揃うよう 0.25m で取る
            ortho = {"url": _IGN_ORTHO_WMS, "layer": layer, "cell": 0.25, "chunk_px": 4000, "epsg": epsg}
            items.append(_plan_item(
                "odm_orthophoto/odm_orthophoto.tif",
                f"IGN BD ORTHO {year or 'latest'} 0.25m ({layer})", o_acq,
                "IGN – BD ORTHO, via Géoplateforme", _IGN_LICENCE, "IGN – BD ORTHO",
                "https://data.geopf.fr/wms-r/wms"))
            if dem_year and year and year != dem_year:
                notes.append(f"⚠ The orthophoto ({year}) and the elevation data ({dem_year}) were "
                             "acquired in different years. Tree canopy and land cover may differ.")
            if cover < _ORTHO_FULL_COVER:
                notes.append(f"⚠ The orthophoto covers only about {cover:.0%} of the area.")
        else:
            notes.append("No orthophoto is available for this area.")

    return {"country": country, "bbox": bbox, "epsg": epsg, "items": items,
            "ortho": ortho, "notes": notes}


def remote_export_summary(plan):
    """確認・完了の窓に出す行。"""
    names = {"odm_dem/dtm.tif": "DTM", "odm_dem/dsm.tif": "DSM",
             "odm_orthophoto/odm_orthophoto.tif": "Ortho"}
    by_entry = {it["entry"]: it for it in plan["items"]}
    lines = []
    for entry, name in names.items():
        it = by_entry.get(entry)
        if it:
            lines.append(f"• {name}: {it['label']} — acquired {it['acquired']}")
        elif name == "DSM":
            lines.append("• DSM: not included (DSM Data is not set)")
        else:
            lines.append(f"• {name}: not available for this area")
    return lines + ([""] + plan["notes"] if plan["notes"] else [])


def _data_sources_text(plan, entries):
    """ZIP に入れる DATA_SOURCES.txt（データの一覧と出典）。"""
    x0, y0, x1, y1 = plan["bbox"]
    out = ["Forestry Operations Lite — data list and sources for this ZIP",
           f"Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
           f"Area (EPSG:{plan['epsg']}): {x0:.1f}, {y0:.1f} – {x1:.1f}, {y1:.1f}", ""]
    by_entry = {it["entry"]: it for it in plan["items"]}
    for entry in ("odm_dem/dtm.tif", "odm_dem/dsm.tif", "odm_orthophoto/odm_orthophoto.tif"):
        it = by_entry.get(entry)
        if entry not in entries or it is None:
            if entry == "odm_dem/dsm.tif":
                out += [entry, "  Not included (DSM Data was not set when exporting).", ""]
            continue
        out += [it["entry"],
                f"  Data:     {it['label']}",
                f"  Acquired: {it['acquired']}",
                f"  Source:   {it['source']}",
                f"  Licence:  {it['licence']}"]
        if it["attribution"]:
            out.append(f"  Attribution: {it['attribution']}")
        if it["service"]:
            out.append(f"  Service:  {it['service']}")
        out.append("")
    if plan["notes"]:
        out += ["Notes:"] + [f"  {n}" for n in plan["notes"]] + [""]
    out += ["las_sources.json / README_LAS_LINKS.txt:",
            "  This source does not use point clouds, so no LAS files are referenced.", ""]
    return "\n".join(out)


def _fetch_ortho_cog(ctx, ortho, bbox, out_path):
    """COG のオルソ（LINZ）から範囲を 0.25m で切り出して1枚の GeoTIFF にする。"""
    from osgeo import gdal
    cell = ortho["cell"]
    x0, y0, x1, y1 = _snap_bbox(bbox, cell)
    w_px = int(round((x1 - x0) / cell))
    h_px = int(round((y1 - y0) / cell))
    if w_px * h_px > _REMOTE_MAX_PIXELS:
        raise DemFetchError(
            f"⚠ Ortho extent too large ({w_px:,} × {h_px:,} px at {cell} m). Zoom in and try again.")

    def _cb(complete, _msg, _data):
        ctx.report(f"Fetching Ortho… {int(complete * 100)}%")
        return 0 if ctx.cancelled() else 1

    gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetThreadLocalConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tiff,.tif")
    try:
        ctx.report(f"Fetching Ortho ({len(ortho['tiles'])} tile(s))…")
        vrt = gdal.BuildVRT("", ortho["tiles"])
        if vrt is None:
            raise DemFetchError("⚠ Could not read the orthophoto tiles.")
        out = gdal.Translate(out_path, vrt, format="GTiff", callback=_cb, bandList=[1, 2, 3],
                             projWin=[x0, y1, x1, y0], width=w_px, height=h_px,
                             resampleAlg="average", outputSRS=f"EPSG:{ortho['epsg']}",
                             creationOptions=["COMPRESS=JPEG", "JPEG_QUALITY=90", "PHOTOMETRIC=YCBCR",
                                              "TILED=YES", "BIGTIFF=IF_SAFER"])
        vrt = None
    finally:
        gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)
        gdal.SetThreadLocalConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", None)
    ctx.check()
    if out is None:
        raise DemFetchError("⚠ Could not write the orthophoto GeoTIFF.")
    out = None
    return out_path


def _fetch_ortho(ctx, ortho, bbox, out_path):
    """WMS のオルソ（JPEG）を区切って取り、座標を付けて1枚の GeoTIFF にする。"""
    from osgeo import gdal
    if ortho.get("kind") == "cog":
        return _fetch_ortho_cog(ctx, ortho, bbox, out_path)
    cell = ortho["cell"]
    x0, y0, x1, y1 = _snap_bbox(bbox, cell)
    w_px = int(round((x1 - x0) / cell))
    h_px = int(round((y1 - y0) / cell))
    if w_px * h_px > _REMOTE_MAX_PIXELS:
        raise DemFetchError(
            f"⚠ Ortho extent too large ({w_px:,} × {h_px:,} px at {cell} m). Zoom in and try again.")
    step = ortho["chunk_px"] * cell
    cells = []
    y = y0
    while y < y1 - 1e-9:
        x = x0
        while x < x1 - 1e-9:
            cells.append((x, y, min(x + step, x1), min(y + step, y1)))
            x += step
        y += step
    parts_dir = os.path.dirname(out_path)
    srs = f"EPSG:{ortho['epsg']}"
    parts = []
    for k, (cx0, cy0, cx1, cy1) in enumerate(cells, 1):
        ctx.check()
        ctx.report(f"Fetching Ortho… {k}/{len(cells)}")
        w = int(round((cx1 - cx0) / cell))
        h = int(round((cy1 - cy0) / cell))
        url = ortho["url"].format(layer=ortho["layer"], x0=cx0, y0=cy0, x1=cx1, y1=cy1, w=w, h=h)
        jpg = os.path.join(parts_dir, f"ortho_part_{k:04d}.jpg")
        _http_download(ctx, url, jpg, accept=("jpeg", "jpg"))
        tif = os.path.join(parts_dir, f"ortho_part_{k:04d}.tif")
        if gdal.Translate(tif, jpg, outputBounds=[cx0, cy1, cx1, cy0], outputSRS=srs) is None:
            raise DemFetchError("⚠ Could not georeference the orthophoto.")
        os.remove(jpg)
        parts.append(tif)
    ctx.report("Merging Ortho…")
    vrt = gdal.BuildVRT("", parts)

    def _cb(complete, _msg, _data):
        ctx.report(f"Merging Ortho… {int(complete * 100)}%")
        return 0 if ctx.cancelled() else 1

    out = gdal.Translate(out_path, vrt, format="GTiff", callback=_cb,
                         creationOptions=["COMPRESS=JPEG", "JPEG_QUALITY=90", "PHOTOMETRIC=YCBCR",
                                          "TILED=YES", "BIGTIFF=IF_SAFER"])
    vrt = None
    ctx.check()
    if out is None:
        raise DemFetchError("⚠ Could not write the orthophoto GeoTIFF.")
    out = None
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    return out_path


def remote_export_zip(ctx, plan, tmp_dir, zip_dir, dtm_path, dsm_path):
    """海外ソースの DTM・DSM・オルソと DATA_SOURCES.txt を WODMI ZIP に書き出す。

    戻り値: {"zip_path", "zip_name", "has_dsm", "has_ortho", "summary"}
    """
    import json
    import zipfile

    ortho_path = None
    if plan["ortho"]:
        ortho_path = _fetch_ortho(ctx, plan["ortho"], plan["bbox"],
                                  os.path.join(tmp_dir, "odm_orthophoto.tif"))
    ctx.check()
    zip_path, zip_name = _next_wodmi_zip_path(zip_dir)
    ctx.report("Creating ZIP…")
    ctx.created_files.append(zip_path)
    entries = {"odm_dem/dtm.tif"}
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(dtm_path, "odm_dem/dtm.tif")
            if dsm_path:
                zf.write(dsm_path, "odm_dem/dsm.tif")
                entries.add("odm_dem/dsm.tif")
            if ortho_path:
                zf.write(ortho_path, "odm_orthophoto/odm_orthophoto.tif")
                entries.add("odm_orthophoto/odm_orthophoto.tif")
            # FOL 由来の印（点群は使わないので空）— vs_export_zip の説明を参照
            zf.writestr("las_sources.json", json.dumps({"las": []}, indent=2))
            zf.writestr("README_LAS_LINKS.txt", "\n".join(_LAS_NOT_USED_README))
            zf.writestr("DATA_SOURCES.txt", _data_sources_text(plan, entries))
    except Exception as e:
        raise DemFetchError(f"ZIP creation failed:\n{e}")
    ctx.check()
    return {"zip_path": zip_path, "zip_name": zip_name, "has_dsm": bool(dsm_path),
            "has_ortho": bool(ortho_path), "summary": remote_export_summary(plan)}
