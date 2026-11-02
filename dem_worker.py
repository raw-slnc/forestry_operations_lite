"""
DEM の取得をワーカースレッドで行う。

- DemJobWorker: 取得処理（job）を受け取って別スレッドで動かす共通の入れ物。
  job(ctx) は画面（ウィジェット・QgsProject）に触らず、状況は ctx.report(text)、
  中止の確認は ctx.check() / ctx.cancelled() で行う。作ったファイルは
  ctx.created_files に入れておくと、中止・エラー時に dockwidget 側が消す。
- fetch_tile_dem: 国土地理院 / Terrarium の標高タイルを取得して GeoTIFF にし、
  UTM へ投影し直して DEMLoader で読み込む（_load_gsi_dem の取得部分）。
- fetch_vs_grid / fetch_vs_dsm: Virtual Shizuoka の LP/Grid（DEM）と LP（DSM）。
- vs_resolve_ortho / vs_export_zip: Export for WebODM Importer（ZIP の書き出し）。

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
        except Exception as exc:  # ワーカーの外へ例外を出さない
            QgsMessageLog.logMessage(
                "DEM fetch failed:\n" + traceback.format_exc(),
                "Forestry Operations Lite", Qgis.Warning)
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
    from .vs_lp import Cancelled
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
    from .vs_lp import Cancelled
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
        ctx.report(f"DSM {i + 1}/{n} — downloading LAS (~237 MB)…")
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


# ── Named Export の DEM（解析のグリッドの DEM を書き出しフォルダに持たせる）──

def copy_analysis_dem(ctx, folder, reference_path, dem_path):
    """解析の DEM を reference_path（解析のグリッド）に合わせて読み、
    folder/elev_relief.tif に書く。4.x と同じ名前・同じ形（4.x の Elevation
    Background）。3.x は表示に使わない。戻り値: 書いたパス"""
    from .terrain import result_writer as rw
    from .terrain.dem_loader import DEMLoader

    ctx.report("Copying the analysis DEM to the Named Export...")
    try:
        reference = DEMLoader().load(reference_path)
        source = DEMLoader().open_metadata(dem_path)
        ctx.check()
        elevation, _method = source.read_aligned_to(reference)
    except DemFetchCancelled:
        raise
    except Exception as exc:
        raise DemFetchError("The analysis DEM could not be copied: {}".format(exc))
    elevation[~np.isfinite(reference.data)] = np.nan
    ctx.check()
    ctx.created_files.append(os.path.join(folder, "elev_relief.tif"))
    return rw.save_raster(elevation.astype(np.float32), reference.gt,
                          reference.crs_wkt, folder, "elev_relief", overwrite=True)


# ── 書き出し（VS → WODMI ZIP）──────────────────────────────────────────────

def vs_resolve_ortho(ctx, codes):
    """VS の書き出しの前に、LP/Ortho のある年度を調べる。戻り値: {code: year}"""
    from .vs_lp import resolve_years
    ctx.report("Checking Ortho…")
    if not codes:
        return {}
    return resolve_years(codes, lp_type="Ortho",
                         progress_cb=_cancel_aware_progress(ctx, "Checking Ortho… {done}/{total}"))


def vs_export_zip(ctx, resolved_ortho, tmp_dir, zip_dir, dtm_path, dsm_path,
                  las_paths, las_urls):
    """LP/Ortho を取得し、DTM/DSM/Ortho と LAS の参照を WODMI ZIP に書き出す。

    戻り値: {"zip_path": str, "zip_name": str, "has_dsm": bool, "has_ortho": bool}
    ZIP の作成に失敗したら DemFetchError("ZIP creation failed:\n...")。
    """
    import json
    import shutil
    import zipfile
    from .vs_lp import Cancelled
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

            las_sources = {"las": []}
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


# Public DEM/DSM sources outside Japan. These are analysis inputs only; the
# existing Virtual Shizuoka WebODM export remains a separate workflow.
_AHN_WCS = ("https://service.pdok.nl/rws/ahn/wcs/v1_0?service=WCS&request=GetCoverage"
            "&version=2.0.1&coverageId={layer}&subset=x({x0},{x1})&subset=y({y0},{y1})"
            "&format=image/tiff")
_IGN_WMSR = ("https://data.geopf.fr/wms-r/wms?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
             "&LAYERS={layer}&STYLES=&CRS=EPSG:2154&BBOX={x0},{y0},{x1},{y1}"
             "&WIDTH={w}&HEIGHT={h}&FORMAT=image/geotiff")
_NZ_BASE = ("https://nz-elevation.s3.ap-southeast-2.amazonaws.com/"
            "new-zealand/new-zealand/dem_1m/2193/")

REMOTE_SOURCES = {
    "__AHN_DTM__": dict(kind="ogc", url=_AHN_WCS, layer="dtm_05m", epsg=28992,
                        cell=0.5, chunk_px=1000, label="AHN4 DTM 0.5m",
                        prefix="ahn_dtm"),
    "__AHN_DSM__": dict(kind="ogc", url=_AHN_WCS, layer="dsm_05m", epsg=28992,
                        cell=0.5, chunk_px=1000, label="AHN4 DSM 0.5m",
                        prefix="ahn_dsm"),
    "__IGN_LIDARHD_MNT__": dict(
        kind="ogc", url=_IGN_WMSR,
        layer="IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
        epsg=2154, cell=0.5, chunk_px=2000,
        label="IGN LiDAR HD MNT 0.5m", prefix="ign_lidarhd_mnt"),
    "__IGN_LIDARHD_MNS__": dict(
        kind="ogc", url=_IGN_WMSR,
        layer="IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
        epsg=2154, cell=0.5, chunk_px=2000,
        label="IGN LiDAR HD MNS 0.5m", prefix="ign_lidarhd_mns"),
    "__LINZ_DEM_1M__": dict(kind="nzcog", epsg=2193, cell=1.0,
                            label="LINZ NZ DEM 1m", prefix="linz_dem"),
}

_REMOTE_MAX_PIXELS = 100_000_000


def _http_download(ctx, url, dest, timeout=180, accept=("tif",)):
    """Download in chunks so cancellation remains responsive."""
    import urllib.request
    if not url.startswith("https://"):
        raise DemFetchError(f"⚠ Invalid URL: {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "Forestry Operations Lite (QGIS plugin)"})
    with urllib.request.urlopen(req, timeout=timeout) as response:  # nosec B310
        with open(dest, "wb") as stream:
            content_type = response.headers.get("Content-Type", "")
            while True:
                ctx.check()
                chunk = response.read(65536)
                if not chunk:
                    break
                stream.write(chunk)
    if not any(token in content_type.lower() for token in accept):
        try:
            with open(dest, "rb") as stream:
                head = stream.read(300).decode("utf-8", "replace")
        except OSError:
            head = ""
        what = "elevation data" if accept == ("tif",) else "the requested data"
        raise DemFetchError(
            f"⚠ The service returned an error instead of {what}: {head}")


def _snap_bbox(bbox, cell):
    """Expand a bounding box outward to the source grid."""
    import math
    x0, y0, x1, y1 = bbox
    return (math.floor(x0 / cell) * cell, math.floor(y0 / cell) * cell,
            math.ceil(x1 / cell) * cell, math.ceil(y1 / cell) * cell)


def _finish_mosaic(ctx, sources, out_dir, prefix, epsg, bbox=None, cell=None):
    """Combine source rasters and reject an all-NoData result."""
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

    opts = dict(
        format="GTiff",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
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
    valid &= np.abs(arr) < 1e30
    if not valid.any():
        raise DemFetchError(
            "⚠ No elevation data for this area (outside the source coverage).")

    loader = DEMLoader()
    loader.load(tif_path)
    return {"loader": loader, "tif_path": tif_path}


def _fetch_ogc(ctx, spec, bbox, out_dir):
    """Fetch WCS/WMS data in chunks and combine the pieces."""
    cell = spec["cell"]
    x0, y0, x1, y1 = _snap_bbox(bbox, cell)
    w_px = int(round((x1 - x0) / cell))
    h_px = int(round((y1 - y0) / cell))
    if w_px <= 0 or h_px <= 0:
        raise DemFetchError("⚠ The preview extent is empty.")
    if w_px * h_px > _REMOTE_MAX_PIXELS:
        raise DemFetchError(
            f"⚠ Extent too large ({w_px:,} × {h_px:,} px at {cell} m). "
            "Zoom in and try again.")
    step = spec["chunk_px"] * cell
    xs, ys = [], []
    x = x0
    while x < x1 - 1e-9:
        xs.append((x, min(x + step, x1)))
        x += step
    y = y0
    while y < y1 - 1e-9:
        ys.append((y, min(y + step, y1)))
        y += step
    parts_dir = os.path.join(out_dir, f".parts_{spec['prefix']}")
    os.makedirs(parts_dir, exist_ok=True)
    ctx.watch_dir(parts_dir)
    paths = []
    total = len(xs) * len(ys)
    index = 0
    try:
        for cy0, cy1 in ys:
            for cx0, cx1 in xs:
                ctx.check()
                index += 1
                ctx.report(f"Fetching {spec['label']}… {index}/{total}")
                url = spec["url"].format(
                    layer=spec["layer"], x0=cx0, y0=cy0, x1=cx1, y1=cy1,
                    w=int(round((cx1 - cx0) / cell)),
                    h=int(round((cy1 - cy0) / cell)))
                dest = os.path.join(parts_dir, f"part_{index:04d}.tif")
                _http_download(ctx, url, dest)
                paths.append(dest)
        ctx.report("Combining…")
        return _finish_mosaic(
            ctx, paths, out_dir, spec["prefix"], spec["epsg"])
    finally:
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            os.rmdir(parts_dir)
        except OSError:
            pass


_TOPO50_LETTERS = "".join(
    letter for letter in string.ascii_uppercase if letter not in "IO")


def _nz_tile_names(bbox):
    """Return NZ Topo50 sheet names intersecting an NZTM2000 bbox."""
    import math
    x0, y0, x1, y1 = bbox
    c0 = int(math.floor((x0 - 988000) / 24000))
    c1 = int(math.floor((x1 - 988000 - 1e-6) / 24000))
    r0 = int(math.floor((6234000 - y1) / 36000))
    r1 = int(math.floor((6234000 - y0 - 1e-6) / 36000))
    base = _TOPO50_LETTERS.index("A") * 24 + _TOPO50_LETTERS.index("S")
    names = []
    for row in range(r0, r1 + 1):
        code = base + row
        if code < 0:
            continue
        first, second = divmod(code, 24)
        if first >= len(_TOPO50_LETTERS):
            continue
        for col in range(c0, c1 + 1):
            if 0 <= col <= 99:
                names.append(
                    f"{_TOPO50_LETTERS[first]}{_TOPO50_LETTERS[second]}{col:02d}")
    return names


def gdal_has_lerc():
    """Whether this GDAL build supports the LERC compression used by LINZ."""
    from osgeo import gdal
    driver = gdal.GetDriverByName("GTiff")
    options = (driver.GetMetadataItem("DMD_CREATIONOPTIONLIST")
               if driver is not None else None)
    return bool(options) and "<Value>LERC</Value>" in options


LERC_MISSING_MESSAGE = (
    "⚠ This QGIS's GDAL cannot read LERC-compressed GeoTIFFs, which the LINZ data uses. "
    "Use a QGIS build whose GDAL supports LERC.")


def _nz_available_tiles(ctx):
    """Read the LINZ collection index and return available sheet names."""
    import json
    import urllib.request
    ctx.report("Checking LINZ tile index…")
    req = urllib.request.Request(
        _NZ_BASE + "collection.json",
        headers={"User-Agent": "Forestry Operations Lite (QGIS plugin)"})
    with urllib.request.urlopen(req, timeout=60) as response:  # nosec B310
        collection = json.loads(response.read().decode("utf-8"))
    return {link["href"][2:-5] for link in collection.get("links", [])
            if link.get("rel") == "item"
            and link.get("href", "").startswith("./")}


def _fetch_nzcog(ctx, spec, bbox, out_dir):
    """Read only the required parts of LINZ Cloud Optimised GeoTIFFs."""
    from osgeo import gdal
    cell = spec["cell"]
    bbox = _snap_bbox(bbox, cell)
    w_px = int(round((bbox[2] - bbox[0]) / cell))
    h_px = int(round((bbox[3] - bbox[1]) / cell))
    if w_px * h_px > _REMOTE_MAX_PIXELS:
        raise DemFetchError(
            f"⚠ Extent too large ({w_px:,} × {h_px:,} px at {cell} m). "
            "Zoom in and try again.")
    if not gdal_has_lerc():
        raise DemFetchError(LERC_MISSING_MESSAGE)
    wanted = _nz_tile_names(bbox)
    available = _nz_available_tiles(ctx)
    tiles = [name for name in wanted if name in available]
    if not tiles:
        raise DemFetchError(
            "⚠ No elevation data for this area (outside the source coverage).")
    ctx.check()
    gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetThreadLocalConfigOption(
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tiff,.tif")
    try:
        sources = [f"/vsicurl/{_NZ_BASE}{name}.tiff" for name in tiles]
        ctx.report(f"Reading {spec['label']} ({len(tiles)} tile(s))…")
        return _finish_mosaic(
            ctx, sources, out_dir, spec["prefix"], spec["epsg"], bbox=bbox,
            cell=cell)
    finally:
        gdal.SetThreadLocalConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", None)
        gdal.SetThreadLocalConfigOption(
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS", None)


def _raster_bbox(path):
    """Return a raster bounding box in its own CRS."""
    from osgeo import gdal
    dataset = gdal.Open(path)
    if dataset is None:
        raise DemFetchError(f"⚠ Cannot open {os.path.basename(path)}.")
    gt = dataset.GetGeoTransform()
    width, height = dataset.RasterXSize, dataset.RasterYSize
    dataset = None
    xs = (gt[0], gt[0] + gt[1] * width)
    ys = (gt[3], gt[3] + gt[5] * height)
    return min(xs), min(ys), max(xs), max(ys)


def _find_fetched_raster(out_dir, prefix, epsg, bbox, cell):
    """Return the smallest previously fetched raster covering bbox."""
    from osgeo import gdal, osr
    tolerance = cell * 0.01
    best = None
    try:
        names = os.listdir(out_dir)
    except OSError:
        return None
    for name in names:
        if not (name.startswith(prefix + "_") and name.endswith(".tif")):
            continue
        path = os.path.join(out_dir, name)
        try:
            dataset = gdal.Open(path)
            if dataset is None:
                continue
            srs = osr.SpatialReference(wkt=dataset.GetProjection())
            code = srs.GetAuthorityCode(None)
            dataset = None
            if code != str(epsg):
                continue
            x0, y0, x1, y1 = _raster_bbox(path)
        except Exception:  # nosec B112
            continue
        if (x0 <= bbox[0] + tolerance and y0 <= bbox[1] + tolerance
                and x1 >= bbox[2] - tolerance and y1 >= bbox[3] - tolerance):
            area = (x1 - x0) * (y1 - y0)
            if best is None or area < best[0]:
                best = (area, path)
    return best[1] if best else None


def fetch_remote_raster(ctx, sentinel, bbox, out_dir):
    """Fetch an overseas raster, or reuse one that covers the requested bbox."""
    from .terrain.dem_loader import DEMLoader
    spec = REMOTE_SOURCES[sentinel]
    os.makedirs(out_dir, exist_ok=True)
    cached = _find_fetched_raster(
        out_dir, spec["prefix"], spec["epsg"],
        _snap_bbox(bbox, spec["cell"]), spec["cell"])
    if cached:
        ctx.report(f"Using {spec['label']} fetched earlier…")
        loader = DEMLoader()
        loader.load(cached)
        return {"loader": loader, "tif_path": cached}
    if spec["kind"] == "nzcog":
        return _fetch_nzcog(ctx, spec, bbox, out_dir)
    return _fetch_ogc(ctx, spec, bbox, out_dir)
