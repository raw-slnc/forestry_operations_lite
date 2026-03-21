"""Virtual Shizuoka LP point cloud S3 utilities.

タイルコード体系（EPSG:6676、平面直角第8系）:
  08{folder}{XX:02d}{AA:02d}
    folder : 2文字 (例 OE, NE, MC ...)
    XX     : 4km×3km ブロック内の列(個位)・行(十位)
    AA     : 400m×300m タイルの列(個位)・行(十位)

座標 (EPSG:6676, メートル単位):
    x_center = folder_x0 + (XX%10)*4000 + (AA%10)*400 + 200
    y_center = folder_y0 - (XX//10)*3000 - (AA//10)*300 - 150
    folder_x0 = (ord(folder[1]) - ord('E')) * 40000
    folder_y0 = -(ord(folder[0]) - ord('M') + 2) * 30000
"""

import os
import urllib.request
import urllib.error
import zipfile
import xml.etree.ElementTree as ET

BUCKET_URL = "https://virtual-shizuoka.s3.ap-northeast-1.amazonaws.com"
TILE_W = 400   # タイル幅 X(northing) [m]
TILE_H = 300   # タイル高 Y(easting) [m]

# 全リターンLAS(LP/Original)を提供する年度（LP/Groundは全年度で提供）
YEARS_WITH_ORIGINAL = (2021, 2025)

# 年度試行順（新しい順）
_YEAR_PRIORITY = (2025, 2022, 2021, 2020, 2019)


# ── 座標計算 ──────────────────────────────────────────────────────────────────

def _folder_origins(folder: str):
    """フォルダコード2文字 → (x_origin, y_origin) EPSG:6676"""
    x0 = (ord(folder[1]) - ord('E')) * 40000
    y0 = -(ord(folder[0]) - ord('M') + 2) * 30000
    return x0, y0


def tile_bbox(code: str):
    """タイルコード → (xmin, ymin, xmax, ymax) EPSG:6676"""
    folder = code[2:4]
    xx = int(code[4:6])
    aa = int(code[6:8])
    x0, y0 = _folder_origins(folder)
    xc = x0 + (xx % 10) * 4000 + (aa % 10) * TILE_W + TILE_W // 2
    yc = y0 - (xx // 10) * 3000 - (aa // 10) * TILE_H - TILE_H // 2
    return xc - TILE_W // 2, yc - TILE_H // 2, xc + TILE_W // 2, yc + TILE_H // 2


def tiles_for_extent(xmin: float, ymin: float, xmax: float, ymax: float) -> set:
    """EPSG:6676 bbox に重複するタイルコードセットを返す（未確認含む）。"""
    codes = set()
    for c1 in "LMNOP":
        for c2 in "BCDEF":
            folder = c1 + c2
            x0, y0 = _folder_origins(folder)
            # フォルダ全体の bbox: X=[x0, x0+40000], Y=[y0-30000, y0]
            if xmax < x0 or xmin > x0 + 40000 or ymax < y0 - 30000 or ymin > y0:
                continue
            xc_min = max(0, int((xmin - x0) / 4000))
            xc_max = min(9, int((xmax - x0) / 4000))
            yr_min = max(0, int((y0 - ymax) / 3000))
            yr_max = min(9, int((y0 - ymin) / 3000))
            for yr in range(yr_min, yr_max + 1):
                for xc in range(xc_min, xc_max + 1):
                    xx_base_x = x0 + xc * 4000
                    xx_base_y = y0 - yr * 3000
                    ac_min = max(0, int((xmin - xx_base_x) / TILE_W))
                    ac_max = min(9, int((xmax - xx_base_x) / TILE_W))
                    ar_min = max(0, int((xx_base_y - ymax) / TILE_H))
                    ar_max = min(9, int((xx_base_y - ymin) / TILE_H))
                    xx = yr * 10 + xc
                    for ar in range(ar_min, ar_max + 1):
                        for ac in range(ac_min, ac_max + 1):
                            aa = ar * 10 + ac
                            codes.add(f"08{folder}{xx:02d}{aa:02d}")
    return codes


# ── S3 ヘルパー ───────────────────────────────────────────────────────────────

def _s3_list_xx(year: int, folder: str, xx: str, lp_type: str = "Grid") -> set:
    """指定 XX ディレクトリ内のタイルコードセットを S3 LIST で取得。"""
    prefix = f"{year}/LP/{lp_type}/08/{folder}/{xx}/"
    url = f"{BUCKET_URL}/?list-type=2&prefix={prefix}&delimiter=/"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            root = ET.fromstring(r.read())
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        codes = set()
        for item in root.findall("s:Contents", ns):
            key = item.find("s:Key", ns).text
            fname = key.rstrip("/").split("/")[-1]
            if fname.endswith(".zip"):
                codes.add(fname[:-4])
        return codes
    except Exception:
        return set()


# ── 年度解決 ──────────────────────────────────────────────────────────────────

def resolve_years(codes: set, progress_cb=None, lp_type: str = "Grid") -> dict:
    """コードセット → {code: year} （S3 LISTをXX単位でバッチ処理、新年度優先）。

    Args:
        codes: タイルコードセット
        progress_cb: (done, total) を受け取るコールバック（任意）
        lp_type: "Grid" | "Original" | "Ortho"

    Returns:
        存在が確認できたコードの {code: year} 辞書
    """
    result: dict[str, int] = {}
    remaining = set(codes)

    # (folder, xx) でグループ化してバッチ S3 LIST
    groups: dict[tuple, set] = {}
    for code in remaining:
        key = (code[2:4], code[4:6])
        groups.setdefault(key, set()).add(code)

    done = 0
    total = len(groups)
    for (folder, xx), batch in groups.items():
        left = set(batch)
        for year in _YEAR_PRIORITY:
            if not left:
                break
            found = _s3_list_xx(year, folder, xx, lp_type)
            hits = left & found
            for code in hits:
                result[code] = year
            left -= hits
        done += 1
        if progress_cb:
            progress_cb(done, total)

    return result


# ── CRS 付与ヘルパー ──────────────────────────────────────────────────────────

def _set_tif_epsg(tif_path: str, epsg: int):
    """GeoTIFF に CRS が未設定の場合に EPSG コードを書き込む。"""
    from osgeo import gdal, osr
    ds = gdal.Open(tif_path, gdal.GA_Update)
    if ds is None:
        return
    if not ds.GetProjection():
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        ds.SetProjection(srs.ExportToWkt())
    ds = None


def _set_las_epsg(las_path: str, epsg: int):
    """LAS ファイルに VLR が未設定の場合に EPSG コードの WKT CRS VLR を追加して上書き保存。"""
    import laspy
    from osgeo import osr
    las = laspy.read(las_path)
    if las.header.vlrs:
        return
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    vlr = laspy.VLR(
        user_id="LASF_Projection",
        record_id=2112,
        description="OGC Transformation Record",
        record_data=srs.ExportToWkt().encode("utf-8"),
    )
    las.vlrs = [vlr]
    las.write(las_path)


# ── ダウンロード ──────────────────────────────────────────────────────────────

def _download(url: str, dest: str):
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)


def _xyz_to_tif(txt_path: str, out_dir: str, cell_size: float = 0.5) -> str:
    """VS LP/Grid の X Y Z テキストファイルを GeoTIFF（EPSG:6676）に変換。"""
    import numpy as np
    from osgeo import gdal, osr

    xs, ys, zs = [], [], []
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) == 3:
                xs.append(float(parts[0]))
                ys.append(float(parts[1]))
                zs.append(float(parts[2]))

    x = np.asarray(xs, dtype=np.float64)
    y = np.asarray(ys, dtype=np.float64)
    z = np.asarray(zs, dtype=np.float32)

    xmin = float(x.min()) - cell_size / 2
    xmax = float(x.max()) + cell_size / 2
    ymin = float(y.min()) - cell_size / 2
    ymax = float(y.max()) + cell_size / 2
    cols = int(round((xmax - xmin) / cell_size))
    rows = int(round((ymax - ymin) / cell_size))

    grid = np.full((rows, cols), np.float32(-9999.0))
    ci = np.round((x - xmin - cell_size / 2) / cell_size).astype(np.int32)
    ri = (rows - 1) - np.round((y - ymin - cell_size / 2) / cell_size).astype(np.int32)
    valid = (ci >= 0) & (ci < cols) & (ri >= 0) & (ri < rows)
    grid[ri[valid], ci[valid]] = z[valid]

    out_path = os.path.splitext(txt_path)[0] + ".tif"
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, cols, rows, 1, gdal.GDT_Float32,
                    ["COMPRESS=LZW", "TILED=YES"])
    ds.SetGeoTransform([xmin, cell_size, 0, ymax, 0, -cell_size])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(6676)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(grid)
    ds = None

    try:
        os.remove(txt_path)
    except OSError:
        pass
    return out_path


def _extract_tif(zip_path: str, out_dir: str) -> str:
    """ZIP 内の TIF または VS LP/Grid の TXT を展開し、GeoTIFF パスを返す。
    TFW ワールドファイルが同梱されている場合は TIF と並べて展開する。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        tif_name = None
        txt_name = None
        for name in names:
            lower = name.lower()
            if lower.endswith((".tif", ".tiff")):
                tif_name = name
            elif lower.endswith(".txt"):
                txt_name = name

        if tif_name:
            # TIF と同名の TFW があれば先に展開
            base = os.path.splitext(tif_name)[0]
            for name in names:
                if name.lower() == base.lower() + ".tfw":
                    dest_tfw = os.path.join(out_dir, os.path.basename(name))
                    with zf.open(name) as src, open(dest_tfw, "wb") as dst:
                        dst.write(src.read())
                    break
            dest = os.path.join(out_dir, os.path.basename(tif_name))
            with zf.open(tif_name) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            return dest

        if txt_name:
            dest = os.path.join(out_dir, os.path.basename(txt_name))
            with zf.open(txt_name) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            return _xyz_to_tif(dest, out_dir)

    raise ValueError(f"No TIF or TXT found in {zip_path}")


def download_grid_tif(code: str, year: int, out_dir: str, lp_type: str = "Grid") -> str:
    """LP/{lp_type} ZIP をダウンロード→展開→GeoTIFF パスを返す。"""
    folder = code[2:4]
    xx = code[4:6]
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
    zip_path = os.path.join(out_dir, f"{code}_{lp_type}.zip")
    _download(url, zip_path)
    tif_path = _extract_tif(zip_path, out_dir)
    _set_tif_epsg(tif_path, 6676)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return tif_path


# ── GeoTIFF マージ ────────────────────────────────────────────────────────────

def merge_tifs(tif_paths: list, out_path: str):
    """複数 GeoTIFF を gdal.Warp でマージして out_path に保存。"""
    from osgeo import gdal
    result = gdal.Warp(
        out_path, tif_paths,
        format="GTiff",
        creationOptions=["COMPRESS=LZW", "TILED=YES"],
    )
    if result is None:
        raise RuntimeError(f"gdal.Warp failed for {tif_paths}")
    result = None


def download_las(code: str, year: int, out_dir: str, lp_type: str = "Ground") -> str:
    """LP/{lp_type} ZIP をダウンロード→LAS ファイルパスを返す。
    lp_type: "Ground"（地面フィルタ済み、全年度）または "Original"（全リターン、2021/2025のみ）
    同タイルコードの LAS が既に out_dir に存在する場合は再利用する。
    """
    # 既存 LAS キャッシュチェック（{code} で始まる .las ファイル）
    for fname in os.listdir(out_dir):
        if fname.lower().endswith(".las") and os.path.splitext(fname)[0] == code:
            return os.path.join(out_dir, fname)

    folder = code[2:4]
    xx = code[4:6]
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
    zip_path = os.path.join(out_dir, f"{code}_{lp_type.lower()}.zip")
    _download(url, zip_path)
    # ZIP 内の LAS ファイルを展開
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".las"):
                dest = os.path.join(out_dir, os.path.basename(name))
                with zf.open(name) as src, open(dest, "wb") as dst:
                    dst.write(src.read())
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
                _set_las_epsg(dest, 6676)
                return dest
    raise ValueError(f"No LAS found in {zip_path}")


def las_to_dsm(las_path: str, out_path: str, cell_size: float = 0.5):
    """LAS ファイルから DSM（最大値グリッド）を GeoTIFF として保存。
    CRS は EPSG:6676（Virtual Shizuoka 標準）固定。
    """
    import laspy
    import numpy as np
    from osgeo import gdal, osr

    las = laspy.read(las_path)
    x = np.asarray(las.x, dtype=np.float64)
    y = np.asarray(las.y, dtype=np.float64)
    z = np.asarray(las.z, dtype=np.float32)

    xmin, xmax = float(x.min()), float(x.max())
    ymin, ymax = float(y.min()), float(y.max())
    cols = int(np.ceil((xmax - xmin) / cell_size)) + 1
    rows = int(np.ceil((ymax - ymin) / cell_size)) + 1

    grid = np.full((rows, cols), -np.inf, dtype=np.float32)
    ci = ((x - xmin) / cell_size).astype(np.int32)
    ri = (rows - 1 - ((y - ymin) / cell_size).astype(np.int32))
    mask = (ci >= 0) & (ci < cols) & (ri >= 0) & (ri < rows)
    np.maximum.at(grid, (ri[mask], ci[mask]), z[mask])

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_path, cols, rows, 1, gdal.GDT_Float32,
                    ["COMPRESS=LZW", "TILED=YES"])
    ds.SetGeoTransform([xmin, cell_size, 0, ymax, 0, -cell_size])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(6676)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(np.where(np.isfinite(grid), grid, np.float32(-9999.0)))
    ds = None
