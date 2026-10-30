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
    os.makedirs(zip_dir, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y%m%d")
    prefix = f"FOL_{today}"
    existing = [f for f in os.listdir(zip_dir)
                if f.startswith(prefix) and f.endswith("-all.zip")]
    seq = len(existing) + 1
    zip_name = f"{prefix}-all.zip" if seq == 1 else f"{prefix}_{seq}-all.zip"
    zip_path = os.path.join(zip_dir, zip_name)

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
                readme_lines = [
                    "このDEMソースは点群(LAS)データを使用していません。",
                    "そのためLASファイルの参照情報はありません。",
                ]
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
