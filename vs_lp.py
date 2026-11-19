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
    同じコードでも方式が違えば別の場所を指す。方式は年度で決まる（YEAR_SCHEME）。
    tile_bbox(code, scheme) は方式ごとの位置を返し、年度の決まっている候補は
    その方式の位置で範囲と重なるかを確かめる。YEAR_SCHEME に無い新しい年度は
    両方式の候補を扱う。新年度のデータを追加する際は実タイルをダウンロードして
    サイズと AA エンコードを必ず実測確認し、YEAR_SCHEME に足すこと。

年度は配信サーバの一覧から見つける（available_years）。通信の失敗は取り直し、
それでも失敗したら NetworkError（タイルが「無い」こととは区別する）。
"""

import os
import sys
import time
import urllib.request
import urllib.error
import zipfile

_vendor = os.path.join(os.path.dirname(__file__), "vendor")
if _vendor not in sys.path:
    sys.path.append(_vendor)  # vendor は末尾追加でシステム版を優先

from defusedxml.ElementTree import fromstring as _defused_fromstring  # noqa: E402

# laspy: ユーザー環境を優先し、なければプラグイン同梱の vendor から使用
try:
    import laspy  # noqa: F401
except ImportError:
    import laspy  # noqa: F401  (vendor 経由)

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

# 年度の一覧が取れないとき（一覧の機能が閉じられたときなど）に使う年度（新しい順）
FALLBACK_YEARS = (2025, 2022, 2021, 2020, 2019)
# 年度ごとのタイルコードの方式（実タイルで確かめたもの）。ここに無い年度は両方式を扱う
YEAR_SCHEME = {2019: "A", 2020: "A", 2021: "A", 2022: "B", 2025: "A"}
# 通信の取り直し（回ごとの待ち秒）
RETRY_WAITS = (1, 2, 4)


class NetworkError(RuntimeError):
    """配信サーバとの通信が、取り直しても失敗した（タイルが無いのとは別）。"""


# ── 座標計算 ──────────────────────────────────────────────────────────────────

def _folder_origins(folder: str):
    """フォルダコード2文字 → (x_origin, y_origin) EPSG:6676"""
    x0 = (ord(folder[1]) - ord('E')) * 40000
    y0 = -(ord(folder[0]) - ord('M') + 2) * 30000
    return x0, y0


def tile_bbox(code: str, scheme: str = "A"):
    """タイルコード → (xmin, ymin, xmax, ymax) EPSG:6676。
    scheme="B"（2022年形式）で符号として成り立たないコードは None。"""
    folder = code[2:4]
    xx = int(code[4:6])
    aa = int(code[6:8])
    x0, y0 = _folder_origins(folder)
    bx, by = x0 + (xx % 10) * 4000, y0 - (xx // 10) * 3000
    if scheme == "A":
        left, top = bx + (aa % 10) * TILE_W, by - (aa // 10) * TILE_H
        return left, top - TILE_H, left + TILE_W, top
    t, u = aa // 10, aa % 10
    if not (1 <= t <= 4 and 1 <= u <= 4):
        return None
    gr = ((t - 1) // 2) * 2 + (u - 1) // 2
    gc = ((t - 1) % 2) * 2 + (u - 1) % 2
    left, top = bx + gc * 1000, by - gr * 750
    return left, top - 750, left + 1000, top


def _overlaps(box, bbox) -> bool:
    return box is not None and box[2] > bbox[0] and box[0] < bbox[2] and box[3] > bbox[1] and box[1] < bbox[3]


def code_overlaps(code: str, year: int, bbox) -> bool:
    """その年度の方式で、タイルが bbox に重なるか。方式の分からない年度はどちらかの方式で重なれば True"""
    schemes = (YEAR_SCHEME[year],) if year in YEAR_SCHEME else ("A", "B")
    return any(_overlaps(tile_bbox(code, scheme), bbox) for scheme in schemes)


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
    # 方式ごとの位置で範囲と重なるかを確かめる（以前は方式Bの候補も方式Aの位置で判定しており、
    # 2022年のタイルが範囲の端で候補から落ちていた）。年度ごとの絞り込みは resolve_years で行う
    bbox = (xmin, ymin, xmax, ymax)
    return {c for c in codes if _overlaps(tile_bbox(c, "A"), bbox) or _overlaps(tile_bbox(c, "B"), bbox)}


# ── S3 ヘルパー ───────────────────────────────────────────────────────────────

def _open(url: str, method: str = "GET", timeout: int = 15, cancel_cb=None):
    """取り直し付きの通信。(状態コード, 本文) を返す（404 なども返す）。
    通信できない・サーバの不調（5xx）が続いたら NetworkError。"""
    from .terrain.remote_zip import Cancelled
    last = ""
    for wait in (0,) + RETRY_WAITS:
        if wait:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if cancel_cb and cancel_cb():
                    raise Cancelled("cancelled")
                time.sleep(0.1)
        if cancel_cb and cancel_cb():
            raise Cancelled("cancelled")
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
                return r.status, (r.read() if method == "GET" else b"")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return e.code, b""
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            last = str(e)
    raise NetworkError(
        "Could not connect to the VIRTUAL SHIZUOKA server (retried several times). "
        f"Please try again later.\n({last})"
    )


_years_cache = {}


def available_years(cancel_cb=None) -> list:
    """配信サーバにある年度（新しい順）。一覧の機能が使えなければ FALLBACK_YEARS。"""
    if "years" in _years_cache:
        return list(_years_cache["years"])
    status, body = _open(f"{BUCKET_URL}/?list-type=2&delimiter=/", cancel_cb=cancel_cb)
    years = []
    if status == 200:
        root = _defused_fromstring(body)
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        for item in root.findall("s:CommonPrefixes", ns):
            prefix = item.find("s:Prefix", ns).text.rstrip("/")
            if prefix.isdigit() and len(prefix) == 4:
                years.append(int(prefix))
    years = sorted(set(years), reverse=True) or list(FALLBACK_YEARS)
    _years_cache["years"] = years
    return list(years)


def _s3_list_xx(year: int, folder: str, xx: str, lp_type: str = "Grid", cancel_cb=None):
    """指定 XX ディレクトリ内のタイルコードセットを S3 LIST で取得。一覧の機能が使えなければ None。
    1ブロックのタイルは最大100枚で、一覧の上限（1000件）に収まる。"""
    prefix = f"{year}/LP/{lp_type}/08/{folder}/{xx}/"
    status, body = _open(f"{BUCKET_URL}/?list-type=2&prefix={prefix}&delimiter=/", cancel_cb=cancel_cb)
    if status != 200:
        return None
    root = _defused_fromstring(body)
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    codes = set()
    for item in root.findall("s:Contents", ns):
        fname = item.find("s:Key", ns).text.rstrip("/").split("/")[-1]
        if fname.endswith(".zip"):
            codes.add(fname[:-4])
    return codes


def _s3_head_check(year: int, code: str, lp_type: str = "Grid", cancel_cb=None) -> bool:
    """指定タイルが S3 に存在するか HEAD リクエストで確認（一覧が使えない場合）。404・403 は「無い」。"""
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{code[2:4]}/{code[4:6]}/{code}.zip"
    status, _ = _open(url, method="HEAD", cancel_cb=cancel_cb)
    return status == 200


def existing_codes(year: int, codes: set, lp_type: str = "Grid", cancel_cb=None) -> set:
    """その年度・種別に実在するコード。"""
    groups = {}
    for code in codes:
        groups.setdefault((code[2:4], code[4:6]), set()).add(code)
    found = set()
    for (folder, xx), batch in groups.items():
        listed = _s3_list_xx(year, folder, xx, lp_type, cancel_cb)
        if listed is None:
            found |= {c for c in batch if _s3_head_check(year, c, lp_type, cancel_cb)}
        else:
            found |= batch & listed
    return found


def resolve_years(codes: set, progress_cb=None, lp_type: str = "Grid", bbox=None, cancel_cb=None) -> dict:
    """コードセット → {code: year}（新しい年度を優先）。

    bbox（EPSG:6676）を渡すと、年度ごとにその年度の方式の位置で bbox に重なるコードだけを数える
    （同じコードでも方式A・Bで別の場所を指すため）。通信が取り直しても失敗したら NetworkError。

    Args:
        codes: タイルコードセット
        progress_cb: (done, total) を受け取るコールバック（任意）
        lp_type: "Grid" | "Original" | "Ground" | "Ortho"
    """
    years = available_years(cancel_cb)
    result: dict[str, int] = {}
    left = set(codes)
    for done, year in enumerate(years, start=1):
        if not left:
            break
        batch = {c for c in left if bbox is None or code_overlaps(c, year, bbox)}
        if batch:
            found = existing_codes(year, batch, lp_type, cancel_cb)
            for code in found:
                result[code] = year
            left -= found
        if progress_cb:
            progress_cb(done, len(years))
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

def _download(url: str, dest: str, cancel_cb=None, progress_cb=None):
    """cancel_cb を渡すと、チャンク（65536バイト）単位でキャンセルを確認できる。
    これが無いと大きなZIP取得中はQtのイベントループが回らず、Cancelボタンの
    クリック自体がダウンロード完了まで処理されない
    （terrain/remote_zip.py と同じ理由・同じ粒度）。

    progress_cb(downloaded, total) を渡すと、チャンク単位（0.15秒間隔にスロットル）で
    ダウンロード済み/合計バイト数を通知する。totalはContent-Lengthから取得できない
    場合-1（進捗率は出せないが呼び出し側でdownloadedだけ表示することは可能）。

    途中で切れたときは最初から取り直し、取り直しても失敗したら NetworkError。"""
    from .terrain.remote_zip import Cancelled
    last = ""
    for wait in (0,) + RETRY_WAITS:
        if wait:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(url, timeout=600) as r, open(dest, "wb") as f:  # nosec B310
                total = r.headers.get("Content-Length")
                total = int(total) if total is not None else -1
                downloaded = 0
                last_report = 0.0
                while True:
                    if cancel_cb and cancel_cb():
                        raise Cancelled("download cancelled")
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        now = time.monotonic()
                        if now - last_report >= 0.15 or downloaded == total:
                            last_report = now
                            progress_cb(downloaded, total)
            return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            last = str(e)
    raise NetworkError(
        f"Could not download a VIRTUAL SHIZUOKA tile ({os.path.basename(url)}) (retried several times). "
        f"Please try again later.\n({last})"
    )


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


def download_grid_tif(code: str, year: int, out_dir: str, lp_type: str = "Grid", cancel_cb=None, progress_cb=None) -> str:
    """LP/{lp_type} ZIP をダウンロード→展開→GeoTIFF パスを返す。
    同タイルコードの TIF が既に out_dir に存在する場合は再利用する。
    progress_cb(downloaded, total) は _download 参照。"""
    # 年度ごとのフォルダに置く（同じコードでも年度が違えば別のタイル。方式A・Bでは別の場所を指す）
    out_dir = os.path.join(out_dir, f"{lp_type.lower()}_{year}")
    os.makedirs(out_dir, exist_ok=True)
    # 既存 TIF キャッシュチェック
    tif_cache = os.path.join(out_dir, f"{code}.tif")
    if os.path.isfile(tif_cache):
        return tif_cache

    folder = code[2:4]
    xx = code[4:6]
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
    zip_path = os.path.join(out_dir, f"{code}_{lp_type}.zip")
    _download(url, zip_path, cancel_cb=cancel_cb, progress_cb=progress_cb)
    tif_path = _extract_tif(zip_path, out_dir)
    _set_tif_epsg(tif_path, 6676)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return tif_path


# ── GeoTIFF マージ ────────────────────────────────────────────────────────────

def merge_tifs(tif_paths: list, out_path: str, src_nodata=None):
    """複数 GeoTIFF を gdal.Warp でマージして out_path に保存。後に並べたものが上になる。
    src_nodata を渡すと、その値の画素は下のタイルを隠さない（DEM の年度の重ね合わせ用）。"""
    from osgeo import gdal
    options = {}
    if src_nodata is not None:
        options = {"srcNodata": src_nodata, "dstNodata": src_nodata}
    result = gdal.Warp(
        out_path, tif_paths,
        format="GTiff",
        creationOptions=["COMPRESS=LZW", "TILED=YES"],
        **options,
    )
    if result is None:
        raise RuntimeError(f"gdal.Warp failed for {tif_paths}")
    result = None


def download_las(code: str, year: int, out_dir: str, lp_type: str = "Ground", cancel_cb=None, progress_cb=None) -> str:
    """LP/{lp_type} ZIP をダウンロード→LAS ファイルパスを返す。

    lp_type:
        "Original" — 全リターン LAS（2021/2025 のみ提供）
        "Ground"   — 2021/2025 は地面フィルタ済み（DSM計算不可）
                     2019/2020/2022 は全リターン結合データ（DSM計算可）

    同タイルコードの LAS が既に out_dir に存在する場合は再利用する。
    progress_cb(downloaded, total) は _download 参照。
    """
    # 既存 LAS キャッシュチェック（{code} で始まる .las ファイル）
    for fname in os.listdir(out_dir):
        if fname.lower().endswith(".las") and os.path.splitext(fname)[0] == code:
            return os.path.join(out_dir, fname)

    folder = code[2:4]
    xx = code[4:6]
    url = f"{BUCKET_URL}/{year}/LP/{lp_type}/08/{folder}/{xx}/{code}.zip"
    zip_path = os.path.join(out_dir, f"{code}_{lp_type.lower()}.zip")
    _download(url, zip_path, cancel_cb=cancel_cb, progress_cb=progress_cb)
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
