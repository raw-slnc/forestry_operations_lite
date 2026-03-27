"""Virtual Shizuoka LP point cloud S3 utilities.

タイルコード体系（EPSG:6676、平面直角第8系）:
  08{folder}{XX:02d}{AA:02d}
    folder : 2文字 (例 OE, NE, MC ...)
    XX     : 4km×3km ブロック内の列(個位)・行(十位)
    AA     : タイルの列(個位)・行(十位)

座標 (EPSG:6676, メートル単位) ── 標準フォーマット (2019〜2021, 2025 確認済み):
    タイルサイズ: 400m × 300m、AA は単純行列（tens=行, units=列）
    x_center = folder_x0 + (XX%10)*4000 + (AA%10)*400 + 200
    y_center = folder_y0 - (XX//10)*3000 - (AA//10)*300 - 150
    folder_x0 = (ord(folder[1]) - ord('E')) * 40000
    folder_y0 = -(ord(folder[0]) - ord('M') + 2) * 30000

【注意】2022年データ（LD/MD フォルダで確認）はタイル仕様が異なる:
    タイルサイズ: 1000m × 750m、AA は 2 階層 Z 曲線エンコード
      tens桁 = 2×2 サブブロック (1=左上, 2=右上, 3=左下, 4=右下)
      units桁 = サブブロック内位置 (1=左上, 2=右上, 3=左下, 4=右下)
    そのため tile_bbox() / tiles_for_extent() の座標計算は 2022 年データと
    一致しない。新年度のデータを追加する際は実タイルをダウンロードして
    サイズと AA エンコードを必ず実測確認すること。
"""

import os
import urllib.request
import urllib.error
import zipfile
import xml.etree.ElementTree as ET
import sys

# laspy: ユーザー環境を優先し、なければプラグイン同梱の vendor から使用
try:
    import laspy  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "vendor"))
    import laspy  # noqa: F401

BUCKET_URL = "https://virtual-shizuoka.s3.ap-northeast-1.amazonaws.com"
TILE_W = 400   # タイル幅 X(northing) [m]
TILE_H = 300   # タイル高 Y(easting) [m]

# LP データのリターン種別（実測確認済み、2025年時点）:
#   2021, 2025: Original（全リターン）と Ground（地面フィルタ済み）が別パスで提供
#               Ground は ~53〜100 bytes/m² → 地面点のみ（DSM計算不可）
#               Original は ~1000〜4000 bytes/m² → 全リターン（DSM計算可）
#   2019, 2020, 2022: Original パスなし。Ground が全リターン結合データを提供
#               Ground は ~860〜2200 bytes/m² → 全リターン（DSM計算可）
# → DSM 生成には Original（2021/2025）または Ground（2019/2020/2022）のどちらを使っても良い
YEARS_WITH_ORIGINAL = (2021, 2025)  # LP/Original パスが存在する年度

# 年度試行順（新しい順）
_YEAR_PRIORITY = (2025, 2022, 2021, 2020, 2019)           # Ground/Original 用
_YEAR_PRIORITY_GRID = (2025, 2022, 2021, 2020, 2019)       # Grid 用


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
    """EPSG:6676 bbox に重複するタイルコードセットを返す（未確認含む）。

    年度によりタイル仕様が異なるため、両方式の候補コードを生成して返す。
    実在確認は resolve_years() に委ねる。

    方式 A — 標準 400m×300m 単純行列 (2019〜2021, 2025 確認済み):
        AA tens=行, units=列。AA は 00〜99 の範囲。
    方式 B — 1000m×750m Z曲線 (2022/LD・MD で確認):
        4×4 グリッドを 2×2 サブブロック 2 階層で符号化。AA は 11〜44。
        今後の年度でこの方式が再登場する可能性があるため両方生成する。
    """
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
                    xx = yr * 10 + xc

                    # ── 方式 A: 400m×300m 単純行列 ──────────────────────────
                    ac_min = max(0, int((xmin - xx_base_x) / TILE_W))
                    ac_max = min(9, int((xmax - xx_base_x) / TILE_W))
                    ar_min = max(0, int((xx_base_y - ymax) / TILE_H))
                    ar_max = min(9, int((xx_base_y - ymin) / TILE_H))
                    for ar in range(ar_min, ar_max + 1):
                        for ac in range(ac_min, ac_max + 1):
                            aa = ar * 10 + ac
                            codes.add(f"08{folder}{xx:02d}{aa:02d}")

                    # ── 方式 B: 1000m×750m Z曲線 (2022年形式) ───────────────
                    # 4×4 グリッド (gc=列0-3, gr=行0-3) を Z曲線で AA に変換
                    gc_min = max(0, int((xmin - xx_base_x) / 1000))
                    gc_max = min(3, int((xmax - xx_base_x) / 1000))
                    gr_min = max(0, int((xx_base_y - ymax) / 750))
                    gr_max = min(3, int((xx_base_y - ymin) / 750))
                    for gr in range(gr_min, gr_max + 1):
                        for gc in range(gc_min, gc_max + 1):
                            # サブブロック (2×2) → t (1-4)
                            t = (gr // 2) * 2 + (gc // 2) + 1
                            # サブブロック内位置 → u (1-4)
                            u = (gr % 2) * 2 + (gc % 2) + 1
                            aa = t * 10 + u
                            codes.add(f"08{folder}{xx:02d}{aa:02d}")
    # 生成した全候補（方式A・B混在）を方式A座標で実際の重複確認し絞り込む。
    # 方式Bが生成した候補が方式Aタイルとして範囲外に存在した場合に
    # 誤ってダウンロードされる問題を防ぐ。
    return {
        c for c in codes
        if (lambda b: b[2] > xmin and b[0] < xmax and b[3] > ymin and b[1] < ymax)(tile_bbox(c))
    }


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


def _s3_head_check(year: int, code: str, lp_type: str = "Grid") -> bool:
    """指定タイルが S3 に存在するか HEAD リクエストで確認。
    ListBucket 権限が無効な場合の _s3_list_xx フォールバック用。"""
    folder = code[2:4]
    xx = code[4:6]
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


# ── 年度解決 ──────────────────────────────────────────────────────────────────

def resolve_years(codes: set, progress_cb=None, lp_type: str = "Grid") -> dict:
    """コードセット → {code: year} （S3 LISTをXX単位でバッチ処理、新年度優先）。

    S3 LIST（ListBucket）が無効化されている場合、未解決タイルに対して
    HEAD リクエストで個別確認するフォールバックを行う。

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

    priority = _YEAR_PRIORITY_GRID if lp_type == "Grid" else _YEAR_PRIORITY
    done = 0
    total = len(groups)
    for (folder, xx), batch in groups.items():
        left = set(batch)
        for year in priority:
            if not left:
                break
            found = _s3_list_xx(year, folder, xx, lp_type)
            hits = left & found
            for code in hits:
                result[code] = year
            left -= hits

        # LIST で解決できなかったタイルは HEAD で個別確認
        # （ListBucket が無効化されている環境でのフォールバック）
        if left:
            for code in list(left):
                for year in priority:
                    if _s3_head_check(year, code, lp_type):
                        result[code] = year
                        left.discard(code)
                        break

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
    with urllib.request.urlopen(url, timeout=600) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)


def _xyz_to_tif(txt_path: str, out_dir: str, cell_size: float = 0.5) -> str:
    """VS LP/Grid の X Y Z テキストファイルを GeoTIFF（EPSG:6676）に変換。

    対応フォーマット（2025年時点）:
      旧形式 (2019〜2020): スペース区切り 3列「X Y Z」
          例: -65199.75 -130200.25 33.40
      新形式 (2021〜):    カンマ区切り 4〜5列「ID,X,Y,Z[,flag]」
          例: 1,-65199.75,-130200.25,33.40,1
    フォーマットは先頭の有効行から自動判定する。
    今後カラム構成・区切り文字が変わった場合はこの関数を修正すること。
    """
    import numpy as np
    from osgeo import gdal, osr

    xs, ys, zs = [], [], []
    fmt = None  # "old": スペース区切り X Y Z  /  "new": カンマ区切り ID,X,Y,Z[,flag]
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # 先頭の有効行でフォーマットを決定
            if fmt is None:
                fmt = "new" if "," in line else "old"
            try:
                if fmt == "new":
                    parts = line.split(",")
                    if len(parts) >= 4:          # ID, X, Y, Z [, flag]
                        xs.append(float(parts[1]))
                        ys.append(float(parts[2]))
                        zs.append(float(parts[3]))
                else:
                    parts = line.split()
                    if len(parts) == 3:          # X Y Z
                        xs.append(float(parts[0]))
                        ys.append(float(parts[1]))
                        zs.append(float(parts[2]))
            except ValueError:
                continue

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
    """LP/{lp_type} ZIP をダウンロード→展開→GeoTIFF パスを返す。
    同タイルコードの TIF が既に out_dir に存在する場合は再利用する。"""
    # 既存 TIF キャッシュチェック
    tif_cache = os.path.join(out_dir, f"{code}.tif")
    if os.path.isfile(tif_cache):
        return tif_cache

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

    lp_type:
        "Original" — 全リターン LAS（2021/2025 のみ提供）
        "Ground"   — 2021/2025 は地面フィルタ済み（DSM計算不可）
                     2019/2020/2022 は全リターン結合データ（DSM計算可）

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
