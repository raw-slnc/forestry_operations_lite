import math
import os
import numpy as np

try:
    from osgeo import gdal
    gdal.UseExceptions()
    HAS_GDAL = True
except ImportError:
    HAS_GDAL = False


class DEMLoader:
    def __init__(self):
        self.path = None
        self.data = None
        self.gt = None
        self.crs_wkt = None
        self.cell_size = None
        self.nodata = None
        self._ds = None
        self.preparation = {}

    def _clean_elevations(self, data, band):
        """Apply the declared elevation scale/unit, never invent a height datum."""
        data = data.astype(np.float64)
        nodata = band.GetNoDataValue()
        if nodata is not None:
            data[data == nodata] = np.nan
        unit = (band.GetUnitType() or "").strip().lower()
        factors = {"": 1.0, "m": 1.0, "metre": 1.0, "meter": 1.0,
                   "metres": 1.0, "meters": 1.0, "ft": 0.3048,
                   "foot": 0.3048, "feet": 0.3048,
                   "us survey foot": 1200.0 / 3937.0}
        if unit not in factors:
            raise ValueError(f"Unsupported elevation unit: {unit}. Supply elevations in metres.")
        scale = band.GetScale()
        offset = band.GetOffset()
        data = (data * (1.0 if scale is None else scale)
                + (0.0 if offset is None else offset)) * factors[unit]
        data[~np.isfinite(data)] = np.nan
        return data

    @staticmethod
    def _horizontal_srs(wkt):
        from osgeo import osr
        if not wkt:
            raise ValueError("DEM/DSM CRS is unknown. Assign the correct source CRS before analysis.")
        srs = osr.SpatialReference()
        if srs.ImportFromWkt(wkt) != 0 or srs.Validate() != 0:
            raise ValueError("DEM/DSM CRS is invalid. Check the source CRS before analysis.")
        srs.StripVertical()
        srs.DemoteTo2D()
        if not (srs.IsProjected() or srs.IsGeographic()):
            raise ValueError("DEM/DSM requires a projected or geographic horizontal CRS.")
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return srs

    def prepare_for_analysis(self, target_cell_size=None):
        """Return a north-up square metric grid, preserving the source file.

        Suitable projected CRSs are retained. Geographic, Web Mercator and
        non-metric CRSs use the local UTM zone. Heights are already in metres
        after read_data/clip_to_extent; only horizontal coordinates are warped.
        """
        from osgeo import osr
        if self.data is None:
            self.read_data()
        source_srs = self._horizontal_srs(self.crs_wkt)
        mercator = osr.SpatialReference()
        mercator.ImportFromEPSG(3857)
        target_srs = source_srs.Clone()
        metric = (source_srs.IsProjected()
                  and abs(source_srs.GetLinearUnits() - 1.0) < 1e-9
                  and not source_srs.IsSame(mercator))
        rows, cols = self.data.shape
        if not rows or not cols or not np.isfinite(self.data).any():
            raise ValueError("DEM has no valid elevation values in the analysis range.")
        if not metric:
            wgs84 = osr.SpatialReference()
            wgs84.ImportFromEPSG(4326)
            wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            x, y = gdal.ApplyGeoTransform(self.gt, cols / 2, rows / 2)
            lon, lat, _ = osr.CoordinateTransformation(source_srs, wgs84).TransformPoint(x, y)
            if not (math.isfinite(lon) and math.isfinite(lat) and -80 <= lat <= 84):
                raise ValueError("Automatic UTM conversion supports 80 S to 84 N. Supply a local metric projected DEM.")
            zone = min(60, max(1, int((lon + 180) / 6) + 1))
            target_srs = osr.SpatialReference()
            target_srs.ImportFromEPSG((32600 if lat >= 0 else 32700) + zone)
            target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        gt = self.gt
        regular = (gt[1] > 0 and gt[5] < 0 and gt[2] == 0 and gt[4] == 0
                   and math.isclose(gt[1], -gt[5], rel_tol=1e-9))
        if target_cell_size is not None and (
                not math.isfinite(target_cell_size) or target_cell_size <= 0):
            raise ValueError("Analysis cell size must be positive and finite.")
        record = dict(self.preparation) or {
            "source_crs_wkt": self.crs_wkt,
            "source_geotransform": list(gt),
            "elevation_unit": "metres (assumed when source unit is unspecified)",
        }
        if metric and regular and (target_cell_size is None or
                math.isclose(gt[1], target_cell_size, rel_tol=1e-9)):
            self.cell_size = gt[1]
            record.update({"analysis_crs_wkt": self.crs_wkt,
                           "analysis_geotransform": list(gt),
                           "cell_size_m": self.cell_size,
                           "resampling": record.get("resampling", "none")})
            self.preparation = record
            return self

        source = gdal.GetDriverByName("MEM").Create("", cols, rows, 1, gdal.GDT_Float64)
        source.SetGeoTransform(gt)
        source.SetProjection(source_srs.ExportToWkt())
        band = source.GetRasterBand(1)
        band.SetNoDataValue(float("nan"))
        band.WriteArray(np.where(np.isfinite(self.data), self.data, np.nan))
        # A VRT determines the grid without allocating the output pixel array.
        suggested = gdal.Warp("", source, format="VRT", dstSRS=target_srs.ExportToWkt())
        if suggested is None:
            raise RuntimeError("Could not determine the analysis DEM grid.")
        suggested_gt = suggested.GetGeoTransform()
        cell = target_cell_size or max(abs(suggested_gt[1]), abs(suggested_gt[5]))
        suggested = None
        method = "average" if metric and regular and cell > gt[1] else "bilinear"
        options = dict(dstSRS=target_srs.ExportToWkt(), xRes=cell, yRes=cell,
                       resampleAlg=method, outputType=gdal.GDT_Float64,
                       srcNodata=float("nan"), dstNodata=float("nan"), errorThreshold=0)
        preview = gdal.Warp("", source, format="VRT", **options)
        if preview.RasterXSize * preview.RasterYSize > 100_000_000:
            raise ValueError("Analysis DEM exceeds 100 million cells. Reduce the analysis extent.")
        preview = None
        warped = gdal.Warp("", source, format="MEM", **options)
        if warped is None:
            raise RuntimeError("DEM conversion failed.")
        result = DEMLoader()
        result.path = self.path
        result._ds = warped
        result.gt = warped.GetGeoTransform()
        result.crs_wkt = warped.GetProjection()
        result.cell_size = result.gt[1]
        result.nodata = float("nan")
        result.data = warped.GetRasterBand(1).ReadAsArray().astype(np.float64)
        result.data[~np.isfinite(result.data)] = np.nan
        if not np.isfinite(result.data).any():
            raise ValueError("DEM conversion produced no valid elevation values.")
        record.update({"analysis_crs_wkt": result.crs_wkt,
                       "analysis_geotransform": list(result.gt),
                       "cell_size_m": result.cell_size, "resampling": method})
        result.preparation = record
        return result

    def open_metadata(self, path):
        """ファイルを開いてメタデータのみ読み込む（ピクセルデータは読まない）。
        info_text() および clip_to_extent() はこの状態で動作する。"""
        if not HAS_GDAL:
            raise RuntimeError("GDAL is not available")
        if not os.path.exists(path):
            raise FileNotFoundError(f"File not found: {path}")

        self._ds = gdal.Open(path, gdal.GA_ReadOnly)
        if self._ds is None:
            raise RuntimeError(f"Cannot open DEM: {path}")

        self.path = path
        band = self._ds.GetRasterBand(1)
        self.nodata = band.GetNoDataValue()
        self.gt = self._ds.GetGeoTransform()
        self.crs_wkt = self._ds.GetProjection()
        self.cell_size = abs(self.gt[1])

        # 地理座標系（度単位）の場合は中心緯度でメートル換算
        try:
            from osgeo import osr
            srs = osr.SpatialReference(wkt=self.crs_wkt)
            if srs.IsGeographic():
                center_lat = self.gt[3] + self.gt[5] * self._ds.RasterYSize / 2
                meters_per_deg = 111320.0 * math.cos(math.radians(abs(center_lat)))
                self.cell_size = abs(self.gt[1]) * meters_per_deg
        except Exception:  # nosec B110
            pass

        self.data = None
        self.preparation = {}
        return self

    def read_data(self):
        """open_metadata 後にピクセルデータを読み込む。
        すでに data がある場合は何もしない。"""
        if self.data is not None:
            return self
        if self._ds is None:
            raise RuntimeError("File not open. Call open_metadata first.")
        band = self._ds.GetRasterBand(1)
        self.data = self._clean_elevations(band.ReadAsArray(), band)
        if band.GetMaskFlags() != gdal.GMF_ALL_VALID:
            self.data[band.GetMaskBand().ReadAsArray() == 0] = np.nan
        return self

    def load(self, path):
        """メタデータとピクセルデータを一括読み込みする（後方互換）。"""
        self.open_metadata(path)
        self.read_data()
        return self

    def read_aligned_to(self, reference):
        """Read onto the exact analysis grid; return (array, resampling method)."""
        from osgeo import osr

        if self._ds is None:
            raise RuntimeError("DSM is not loaded")
        if not self.crs_wkt or not reference.crs_wkt:
            raise ValueError("DEM and DSM must both have a coordinate reference system")
        # Reject explicitly different height datums; horizontal alignment cannot fix them.
        source_full = osr.SpatialReference(wkt=self.crs_wkt)
        dem_wkt = reference.preparation.get("source_crs_wkt", reference.crs_wkt)
        dem_full = osr.SpatialReference(wkt=dem_wkt)
        source_vertical = source_full.GetAttrValue("VERT_CS|VERT_DATUM")
        dem_vertical = dem_full.GetAttrValue("VERT_CS|VERT_DATUM")
        source_ellipsoidal = source_full.IsGeographic() and source_full.GetAxesCount() == 3
        dem_ellipsoidal = dem_full.IsGeographic() and dem_full.GetAxesCount() == 3
        if ((source_vertical and dem_vertical and source_vertical != dem_vertical)
                or (source_vertical and dem_ellipsoidal)
                or (dem_vertical and source_ellipsoidal)):
            raise ValueError("DEM and DSM have different vertical datums. Convert heights to the same datum first.")
        source_srs = self._horizontal_srs(self.crs_wkt)
        target_srs = self._horizontal_srs(reference.crs_wkt)
        rows, cols = reference.data.shape
        source_gt = self._ds.GetGeoTransform()
        target_gt = reference.gt
        band = self._ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()

        def clean(data):
            return self._clean_elevations(data, band)

        # An integer-offset window on the same grid needs no interpolation.
        basis = (1, 2, 4, 5)
        if source_srs.IsSame(target_srs) and np.allclose(
                [source_gt[i] for i in basis], [target_gt[i] for i in basis],
                rtol=0, atol=1e-10):
            inv_gt = gdal.InvGeoTransform(source_gt)
            col, row = gdal.ApplyGeoTransform(inv_gt, target_gt[0], target_gt[3])
            c0, r0 = round(col), round(row)
            if (abs(col - c0) < 1e-7 and abs(row - r0) < 1e-7
                    and 0 <= c0 and 0 <= r0
                    and c0 + cols <= self._ds.RasterXSize
                    and r0 + rows <= self._ds.RasterYSize):
                data = clean(band.ReadAsArray(c0, r0, cols, rows))
                if band.GetMaskFlags() != gdal.GMF_ALL_VALID:
                    mask = band.GetMaskBand().ReadAsArray(c0, r0, cols, rows)
                    data[mask == 0] = np.nan
                data[~np.isfinite(reference.data)] = np.nan
                return data, "none"

        # Pre-create the destination so even rotated grids retain the DEM affine.
        target = gdal.GetDriverByName("MEM").Create(
            "", cols, rows, 1, gdal.GDT_Float64)
        target.SetGeoTransform(target_gt)
        target.SetProjection(reference.crs_wkt)
        target_band = target.GetRasterBand(1)
        target_band.SetNoDataValue(float("nan"))
        target_band.Fill(float("nan"))
        try:
            result = gdal.Warp(
                target, self._ds, srcSRS=source_srs.ExportToWkt(),
                dstSRS=target_srs.ExportToWkt(),
                resampleAlg="bilinear", errorThreshold=0,
                srcNodata=nodata if nodata is not None else float("nan"),
                dstNodata=float("nan"), warpOptions=["INIT_DEST=NO_DATA"])
            if not result:
                raise RuntimeError("DSM alignment failed")
            data = target_band.ReadAsArray().astype(np.float64)
            data[~np.isfinite(data)] = np.nan
            data = self._clean_elevations(data, band)
            data[~np.isfinite(reference.data)] = np.nan
            return data, "bilinear"
        finally:
            target_band = None
            target = None

    def sample_at_point(self, x, y, src_crs_wkt=None):
        """地理座標 (x, y) でラスタ値をサンプリングして返す。
        src_crs_wkt を指定した場合、DEM CRS へ変換してからサンプリングする。
        data が未読みの場合は自動的に read_data() を呼び出す。
        範囲外・nodata は None を返す。"""
        if self.data is None:
            if self._ds is None:
                return None
            self.read_data()
        if src_crs_wkt and src_crs_wkt != self.crs_wkt:
            try:
                from qgis.core import (
                    QgsCoordinateReferenceSystem,
                    QgsCoordinateTransform,
                    QgsProject,
                    QgsPointXY,
                )
                src_crs = QgsCoordinateReferenceSystem()
                src_crs.createFromWkt(src_crs_wkt)
                dst_crs = QgsCoordinateReferenceSystem()
                dst_crs.createFromWkt(self.crs_wkt)
                xform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                pt = xform.transform(QgsPointXY(x, y))
                x, y = pt.x(), pt.y()
            except Exception:
                return None
        gt = self.gt
        col_f, row_f = gdal.ApplyGeoTransform(gdal.InvGeoTransform(gt), x, y)
        col_i = math.floor(col_f)
        row_i = math.floor(row_f)
        if col_i < 0 or row_i < 0 or col_i >= self.data.shape[1] or row_i >= self.data.shape[0]:
            return None
        val = self.data[row_i, col_i]
        return None if np.isnan(val) else float(val)

    def info_text(self):
        if self._ds is None:
            return "Not set"
        rows = self._ds.RasterYSize
        cols = self._ds.RasterXSize
        try:
            from osgeo import osr
            srs = osr.SpatialReference(wkt=self.crs_wkt)
            auth = srs.GetAuthorityCode(None)
            crs_str = f"EPSG:{auth}" if auth else "Unknown CRS"
        except Exception:
            crs_str = "Unknown CRS"
        return f"{cols}×{rows} px  |  {self.cell_size:.2f} m/px  |  {crs_str}"

    def clip_to_extent(self, xmin, ymin, xmax, ymax):
        """プレビュー可視範囲（DEM CRS 座標）でクリップ。新DEMLoaderを返す。
        open_metadata 後（data=None の状態）でも動作する。"""
        if self._ds is None:
            raise RuntimeError("DEM is not loaded")

        inv_gt = gdal.InvGeoTransform(self.gt)

        pixels = [gdal.ApplyGeoTransform(inv_gt, x, y)
                  for x in (xmin, xmax) for y in (ymin, ymax)]
        c0 = math.floor(min(p[0] for p in pixels))
        c1 = math.ceil(max(p[0] for p in pixels))
        r0 = math.floor(min(p[1] for p in pixels))
        r1 = math.ceil(max(p[1] for p in pixels))
        c0 = max(0, c0)
        r0 = max(0, r0)
        c1 = min(self._ds.RasterXSize, c1)
        r1 = min(self._ds.RasterYSize, r1)

        if c1 <= c0 or r1 <= r0:
            raise ValueError("Clip extent is outside DEM bounds")

        band = self._ds.GetRasterBand(1)
        data = self._clean_elevations(band.ReadAsArray(c0, r0, c1 - c0, r1 - r0), band)
        if band.GetMaskFlags() != gdal.GMF_ALL_VALID:
            data[band.GetMaskBand().ReadAsArray(c0, r0, c1 - c0, r1 - r0) == 0] = np.nan

        x0, y0 = gdal.ApplyGeoTransform(self.gt, c0, r0)
        clipped_gt = (x0, self.gt[1], self.gt[2], y0, self.gt[4], self.gt[5])

        result = DEMLoader()
        result.path = self.path
        result.data = data
        result.gt = clipped_gt
        result.crs_wkt = self.crs_wkt
        result.cell_size = self.cell_size
        result.nodata = self.nodata
        result._ds = self._ds
        return result


# ── 国土地理院 DEM5A タイルローダー ─────────────────────────────────────────

class GSITileDEMLoader:
    """国土地理院 DEM5A PNG タイルをキャンバス範囲で取得し DEMLoader 互換インタフェースを提供する。

    使い方:
        loader = GSITileDEMLoader()
        loader.fetch_for_extent(lon_min, lat_min, lon_max, lat_max)
        # 以降は DEMLoader と同様に data / gt / crs_wkt / cell_size が使える
    """

    SENTINEL  = "__GSI_DEM5A__"
    TILE_SIZE = 256

    # 解像度優先順: 1m → 5m → 10m
    # (url_template, zoom, label, encoding)
    TILE_SOURCES = [
        ("https://cyberjapandata.gsi.go.jp/xyz/dem1a_png/{z}/{x}/{y}.png", 17, "DEM1A 1m",   "gsi"),
        ("https://cyberjapandata.gsi.go.jp/xyz/dem5a_png/{z}/{x}/{y}.png", 15, "DEM5A 5m",   "gsi"),
        ("https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png",   14, "DEM10B 10m", "gsi"),
    ]

    # AWS Terrain Tiles (Mapzen/Terrarium) — 全球、登録不要
    # (url_template, zoom, label, encoding)
    TERRARIUM_SOURCES = [
        ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png", 14, "Terrarium ~2m",  "terrarium"),
        ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png", 13, "Terrarium ~5m",  "terrarium"),
        ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png", 12, "Terrarium ~10m", "terrarium"),
    ]

    def __init__(self):
        self.path     = self.SENTINEL
        self.data     = None
        self.gt       = None
        self.crs_wkt  = None
        self.cell_size = None
        self.nodata   = None
        self._ds      = None   # DEMLoader 互換ダミー

    # ── タイル座標変換 ────────────────────────────────────────────────

    @staticmethod
    def _lonlat_to_tile(lon, lat, z):
        n = 2 ** z
        tx = int((lon + 180.0) / 360.0 * n)
        lat_r = math.radians(lat)
        ty = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
        return tx, ty

    @staticmethod
    def _tile_to_lonlat(tx, ty, z):
        n = 2 ** z
        lon = tx / n * 360.0 - 180.0
        lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * ty / n))))
        return lon, lat

    # ── タイル取得 ────────────────────────────────────────────────────

    @staticmethod
    def _fetch_tile_array(url, encoding="gsi"):
        """URL の PNG タイルを取得し (256, 256) の標高 numpy 配列を返す。
        encoding: "gsi" = 国土地理院形式, "terrarium" = Mapzen/AWS Terrarium 形式
        失敗時は (None, エラー文字列) を返す。"""
        import urllib.request
        from qgis.PyQt.QtGui import QImage

        if not url.startswith(("https://", "http://")):
            return None, f"Invalid URL scheme: {url}"
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; QGIS plugin)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
                raw = resp.read()
        except Exception as e:
            return None, f"Connection error: {e}"

        try:
            img = QImage()
            if not img.loadFromData(raw):
                return None, "PNG decode failed"
            img = img.convertToFormat(QImage.Format.Format_ARGB32)

            ptr = img.bits()
            nbytes = img.sizeInBytes() if hasattr(img, 'sizeInBytes') else img.byteCount()
            if hasattr(ptr, 'setsize'):
                ptr.setsize(nbytes)
            buf = np.frombuffer(bytes(ptr), dtype=np.uint8).reshape((256, 256, 4))
            # ARGB32 リトルエンディアン: byte0=B, byte1=G, byte2=R, byte3=A
            r = buf[:, :, 2].astype(np.float64)
            g = buf[:, :, 1].astype(np.float64)
            b = buf[:, :, 0].astype(np.float64)

            if encoding == "terrarium":
                # Mapzen Terrarium: elevation = R * 256 + G + B / 256 - 32768
                arr = r * 256.0 + g + b / 256.0 - 32768.0
            else:
                # 国土地理院形式: RGB 24bit → 0.01m 精度
                u = (r.astype(np.uint32) * 65536
                     + g.astype(np.uint32) * 256
                     + b.astype(np.uint32))
                arr = np.where(u == 0x800000, np.nan,
                               np.where(u <  0x800000, u.astype(np.float64) * 0.01,
                                        (u.astype(np.int32) - 0x1000000).astype(np.float64) * 0.01))

            return arr.astype(np.float64), None
        except Exception as e:
            return None, f"Image processing error: {e}"

    # ── 範囲取得 ──────────────────────────────────────────────────────

    def fetch_for_extent(self, lon_min, lat_min, lon_max, lat_max, sources=None, cancel_cb=None,
                         progress_cb=None):
        """WGS84 経緯度範囲のタイルをダウンロードして numpy 配列に組み立てる。
        sources を省略すると TILE_SOURCES（1m→5m→10m）の順で自動フォールバックする。

        Parameters
        ----------
        lon_min, lat_min, lon_max, lat_max : float  WGS84 範囲
        sources : list of (url_template, zoom, label) | None
            省略時は TILE_SOURCES を使用。ルート計算など特定解像度を指定する場合に渡す。
        cancel_cb : callable | None
            呼び出すと True を返す場合、タイル取得をキャンセルする。
        progress_cb : callable(done, total) | None
            タイルを1枚取得するごとに呼ぶ（解像度を切り替えると数え直す）。
        """
        self.last_errors = []
        self._used_source_label = None
        self._cancelled = False
        result = None
        for item in (sources or self.TILE_SOURCES):
            # 3-tuple (後方互換) または 4-tuple (encoding付き)
            if len(item) == 4:
                tile_url, tile_zoom, label, encoding = item
            else:
                tile_url, tile_zoom, label = item
                encoding = "gsi"
            result = self._fetch_tiles(lon_min, lat_min, lon_max, lat_max,
                                       tile_url, tile_zoom, encoding, cancel_cb=cancel_cb,
                                       progress_cb=progress_cb)
            if result is None:  # キャンセルされた
                self._cancelled = True
                return self
            if not np.all(np.isnan(result[0])):
                self._used_source_label = label
                break
            self.last_errors.append(f"{label} fetch failed → trying next resolution")
        if result is None or np.all(np.isnan(result[0])):
            return self
        total_arr, zoom_used, x0, y0 = result
        TS = self.TILE_SIZE

        # ── EPSG:3857 (Web Mercator) でジオトランスフォームを設定 ──────────
        WORLD_M = 20037508.3428
        tile_m  = 2.0 * WORLD_M / (2 ** zoom_used)
        px_m    = tile_m / TS

        x_origin = -WORLD_M + x0 * tile_m
        y_origin =  WORLD_M - y0 * tile_m

        # EPSG:3857 CRS
        try:
            from osgeo import osr
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(3857)
            crs_wkt = srs.ExportToWkt()
        except Exception:
            crs_wkt = 'PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",' \
                      'DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],' \
                      'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]],' \
                      'PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],' \
                      'PARAMETER["scale_factor",1],PARAMETER["false_easting",0],' \
                      'PARAMETER["false_northing",0],UNIT["metre",1],' \
                      'AUTHORITY["EPSG","3857"]]'

        # Web Mercator の px_m は赤道基準。高緯度ほど実地上距離は短くなるため cos(lat) で補正
        center_lat = (lat_min + lat_max) / 2
        corrected_cell_size = px_m * math.cos(math.radians(abs(center_lat)))

        self.data      = total_arr
        self.gt        = (x_origin, px_m, 0.0, y_origin, 0.0, -px_m)
        self.crs_wkt   = crs_wkt
        self.cell_size = corrected_cell_size
        self.nodata    = None
        return self

    def _fetch_tiles(self, lon_min, lat_min, lon_max, lat_max, tile_url, zoom, encoding="gsi", cancel_cb=None,
                     progress_cb=None):
        """指定URLとzoomでタイルを取得し (total_arr, zoom, x0, y0) を返す。
        キャンセル時は None を返す。progress_cb(done, total) はタイル1枚ごとに呼ぶ。"""
        max_tile = 2 ** zoom - 1
        TS = self.TILE_SIZE

        x0, y0 = self._lonlat_to_tile(lon_min, lat_max, zoom)
        x1, y1 = self._lonlat_to_tile(lon_max, lat_min, zoom)
        x0 = max(0, min(x0, max_tile))
        y0 = max(0, min(y0, max_tile))
        x1 = max(0, min(x1, max_tile))
        y1 = max(0, min(y1, max_tile))

        nx = x1 - x0 + 1
        ny = y1 - y0 + 1

        # メモリ上限チェック（float64 で約 800 MB 相当）
        MAX_PIXELS = 100_000_000
        if nx * ny * TS * TS > MAX_PIXELS:
            raise MemoryError(
                f"Extent too large (tiles: {nx * ny}, "
                f"pixels: {nx * TS:,} × {ny * TS:,}).\n"
                f"Zoom in or reduce the canvas extent and try again."
            )

        total_arr = np.full((ny * TS, nx * TS), np.nan, dtype=np.float64)
        n_total = nx * ny
        if progress_cb is not None:
            progress_cb(0, n_total)

        for iy, ty in enumerate(range(y0, y1 + 1)):
            for ix, tx in enumerate(range(x0, x1 + 1)):
                if cancel_cb is not None and cancel_cb():
                    return None  # キャンセル
                url = tile_url.format(z=zoom, x=tx, y=ty)
                tile, err = self._fetch_tile_array(url, encoding)
                if tile is not None:
                    total_arr[iy * TS:(iy + 1) * TS,
                              ix * TS:(ix + 1) * TS] = tile
                elif err:
                    if not hasattr(self, 'last_errors'):
                        self.last_errors = []
                    self.last_errors.append(f"z={zoom} x={tx} y={ty}: {err}")
                if progress_cb is not None:
                    progress_cb(iy * nx + ix + 1, n_total)

        return (total_arr, zoom, x0, y0)

    # ── DEMLoader 互換メソッド ────────────────────────────────────────

    def open_metadata(self, path):
        return self

    def read_data(self):
        return self

    def sample_at_point(self, x, y, src_crs_wkt=None):
        """x, y は src_crs_wkt 座標系での点。EPSG:3857 へ変換してから参照する。"""
        if self.data is None or self.gt is None:
            return None
        # CRS 変換（src_crs_wkt が指定されており EPSG:3857 でない場合）
        if src_crs_wkt:
            try:
                from qgis.core import (QgsCoordinateReferenceSystem,
                                       QgsCoordinateTransform,
                                       QgsPointXY, QgsProject)
                src_crs = QgsCoordinateReferenceSystem()
                src_crs.createFromWkt(src_crs_wkt)
                dst_crs = QgsCoordinateReferenceSystem("EPSG:3857")
                if src_crs.authid() != "EPSG:3857":
                    xform = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
                    pt = xform.transform(QgsPointXY(x, y))
                    x, y = pt.x(), pt.y()
            except Exception:  # nosec B110
                pass
        gt = self.gt
        col_i = int((x - gt[0]) / gt[1])
        row_i = int((y - gt[3]) / gt[5])
        if col_i < 0 or row_i < 0 or col_i >= self.data.shape[1] or row_i >= self.data.shape[0]:
            return None
        val = self.data[row_i, col_i]
        return None if np.isnan(val) else float(val)

    def info_text(self):
        if self.data is None:
            return "Not fetched"
        rows, cols = self.data.shape
        px_m = abs(self.gt[1]) if self.gt else 0
        src = getattr(self, "_used_source_label", "Elevation Tile")
        return f"{cols}×{rows} px  |  {src}  |  EPSG:3857  |  {px_m:.1f} m/px"


# ── GeoTIFF 変換ユーティリティ ──────────────────────────────────────────────

def save_as_geotiff(loader, output_path):
    """GSITileDEMLoader または DEMLoader の data/gt/crs_wkt を GeoTIFF に保存する。

    Parameters
    ----------
    loader     : GSITileDEMLoader または DEMLoader（data, gt, crs_wkt が設定済み）
    output_path: 保存先 .tif パス（ディレクトリは事前に作成しておくこと）

    Returns
    -------
    output_path : 成功時は保存先パス文字列
    """
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    if loader.data is None or loader.gt is None:
        raise ValueError("loader has no data or gt set")

    from osgeo import osr

    data = loader.data
    rows, cols = data.shape

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        output_path, cols, rows, 1, gdal.GDT_Float32,
        ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_NEEDED"]
    )
    ds.SetGeoTransform(loader.gt)

    # CRS 設定
    if loader.crs_wkt:
        ds.SetProjection(loader.crs_wkt)
    else:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3857)
        ds.SetProjection(srs.ExportToWkt())

    # nodata は -9999 に統一
    NODATA = -9999.0
    out_data = data.astype(np.float32)
    out_data[np.isnan(out_data)] = NODATA

    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(out_data)
    band.FlushCache()
    ds.FlushCache()
    ds = None  # ファイルをクローズ

    return output_path
