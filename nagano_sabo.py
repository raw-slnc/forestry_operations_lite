"""長野県 砂防課 R3-4 0.5mメッシュDEM ユーティリティ（テスト実装、未検証部分あり）。

データ配布元:
    https://www.geospatial.jp/ckan/dataset/r3-4-50cmdem
    （長野県建設部砂防課、令和3〜7年度航空レーザ測量成果、0.5mメッシュGeoTIFF、EPSG:6676）

配布は市町村単位のzip（1タイル=3000m(東西)×2500m(南北)のGeoTIFFを内包、
1zipあたり十〜数十タイル）。Virtual Shizuoka（vs_lp.py）と違い1タイル=1zipでは
ないため、zip全体をダウンロードせず central directory のみを HTTP Range で読み、
必要な1タイルの圧縮バイト列だけを取得する（terrain/remote_zip.py、地域非依存の共通部品）。

タイルコード体系（EPSG:6676、平面直角第8系）:
    "{行英字}{行数字}-{列英字}{列数字}"  例: "D8-d3"
    行英字は大文字A-Z(=0-25)、列英字は小文字a-z(=0-25)。英字10刻み+数字1刻みの
    入れ子構造（行英字番号×10+行数字 = 行インデックス、列側も同様）。

    行(北方向, Northing)  : Northing_top = 115000 - 行インデックス × 2500
    列(東方向, Easting)   : Easting_left = -108000 + 列インデックス × 3000
    タイルサイズ: 3000m(東西) × 2500m(南北)

    2026-09-23、以下3点の実データ突合で検証済み（いずれも完全一致）:
      D8-d3 / D8-d4 / D9-d3  (佐久穂町, R4撮影)
      E8-b1                  (木曽町,   R3/R7撮影)
      D1-c2                  (松本市,   R3撮影)
    異なる市町村・撮影年度にまたがって一致したため、県全域で絶対座標ベースの
    共通格子である可能性が高いが、全県・全撮影年度での網羅検証はしていない。

タイル→市町村→zip URL の対応は `data/nagano_sabo_dem_index.json` に同梱
（`_r3r7-.xlsx`「内包図郭」列 × CKAN r3-4-50cmdem リソース一覧を突合して作成、
1287タイル・77市町村）。行政境界をまたぐタイルは複数市町村の候補を持ち、
取得時に候補zipを順に試して実際に該当タイルを含むものを使う。

【重要な制約】xlsx「内包図郭」列は各市町村の実タイルを網羅していない。
2026-09-23、佐久穂町で実測したところzip内には63タイルあるのに索引には28タイル
しか無いことを確認（実地テストで索引に無いタイルの取得要求が発生し判明）。
そのため索引ヒットが無い場合、既知タイルの分布から近そうな市町村を推定して
順に試すフォールバックを入れている（_candidate_cities_by_proximity）。
索引はあくまで「速い一次候補」であり、正しさの担保はcentral directory実在確認
（terrain/remote_zip.fetch_entry_bytes）側にある。

【未実装・既知の制約】
- 森林簿側と違い DSM は提供されていない。
- タイル→URL索引はXLSXからの静的生成であり、データセット側の更新に追従しない
  （元データが更新された場合は data/nagano_sabo_dem_index.json の再生成が必要）。
- ネットワーク経由の部分取得（Range非対応サーバへのフォールバック等）は
  最小限の実装。長時間の大量タイル取得のキャンセル動線は未実装。
- フォールバック時は最悪77市町村ぶんcentral directoryを順に確認する可能性があり、
  索引に無い僻地・境界タイルの取得は索引ヒット時より遅くなる。
"""

import json
import os

from .terrain.remote_zip import fetch_entry_bytes

ROW_STEP = 2500     # 北方向, m/インデックス
COL_STEP = 3000      # 東方向, m/インデックス
BASE_NORTHING = 115000   # 行インデックス0の北端 (EPSG:6676)
BASE_EASTING = -108000   # 列インデックス0の西端 (EPSG:6676)

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "nagano_sabo_dem_index.json")
_index_cache = None


def _load_index():
    global _index_cache
    if _index_cache is None:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            _index_cache = json.load(f)
    return _index_cache


# ── タイルコード ⇔ 座標 ──────────────────────────────────────────────

def _row_index(code_row: str) -> int:
    letter, digit = code_row[0], int(code_row[1:])
    return (ord(letter) - ord("A")) * 10 + digit


def _col_index(code_col: str) -> int:
    letter, digit = code_col[0], int(code_col[1:])
    return (ord(letter) - ord("a")) * 10 + digit


def _row_code(row_idx: int) -> str:
    letter = chr(ord("A") + row_idx // 10)
    return f"{letter}{row_idx % 10}"


def _col_code(col_idx: int) -> str:
    letter = chr(ord("a") + col_idx // 10)
    return f"{letter}{col_idx % 10}"


def tile_bbox(code: str):
    """タイルコード（例 "D8-d3"）→ (xmin, ymin, xmax, ymax) EPSG:6676"""
    row_part, col_part = code.split("-")
    row_idx = _row_index(row_part)
    col_idx = _col_index(col_part)
    north = BASE_NORTHING - row_idx * ROW_STEP
    west = BASE_EASTING + col_idx * COL_STEP
    return west, north - ROW_STEP, west + COL_STEP, north


def tiles_for_extent(xmin: float, ymin: float, xmax: float, ymax: float):
    """EPSG:6676 bbox に重なるタイルコードのリストを返す。"""
    row_lo = int((BASE_NORTHING - ymax) // ROW_STEP)
    row_hi = int((BASE_NORTHING - ymin) // ROW_STEP)
    col_lo = int((xmin - BASE_EASTING) // COL_STEP)
    col_hi = int((xmax - BASE_EASTING) // COL_STEP)
    codes = []
    for row_idx in range(max(row_lo, 0), min(row_hi, 259) + 1):
        for col_idx in range(max(col_lo, 0), min(col_hi, 259) + 1):
            codes.append(f"{_row_code(row_idx)}-{_col_code(col_idx)}")
    return codes


# ── 市町村の近傍候補（索引に無いタイル用のフォールバック） ────────────

_city_bbox_cache = None


def _city_bboxes():
    """各市町村の索引済みタイルから (row_min,row_max,col_min,col_max) を推定してキャッシュする。
    索引(内包図郭列)はその市町村の全タイルを網羅していない（実データの一部サンプルの
    可能性が高く、2026-09-23に佐久穂町で実測63タイル中28タイルしか索引に無いことを確認済み）
    ため、正確な範囲ではなく「だいたいこのあたり」の目安として使う。"""
    global _city_bbox_cache
    if _city_bbox_cache is None:
        index = _load_index()
        boxes = {}
        for code, cities in index["tile_to_cities"].items():
            row_part, col_part = code.split("-")
            r, c = _row_index(row_part), _col_index(col_part)
            for city in cities:
                b = boxes.setdefault(city, [r, r, c, c])
                b[0] = min(b[0], r)
                b[1] = max(b[1], r)
                b[2] = min(b[2], c)
                b[3] = max(b[3], c)
        _city_bbox_cache = boxes
    return _city_bbox_cache


def _candidate_cities_by_proximity(code: str):
    """索引に無いタイルについて、既知タイルの分布から近そうな市町村を近い順に返す。"""
    row_part, col_part = code.split("-")
    r, c = _row_index(row_part), _col_index(col_part)

    def _dist(box):
        dr = max(box[0] - r, 0, r - box[1])
        dc = max(box[2] - c, 0, c - box[3])
        return dr + dc

    boxes = _city_bboxes()
    return [city for city, _ in sorted(boxes.items(), key=lambda kv: _dist(kv[1]))]


# ── タイル取得（キャッシュ付き） ──────────────────────────────────────

def download_tile_tif(code: str, out_dir: str, cancel_cb=None, progress_cb=None):
    """タイルコードに対応するGeoTIFFを取得しローカルパスを返す。

    索引(tile_to_cities)に直接該当があればそこを優先し、無ければ近傍の市町村を
    順に試す（索引は市町村ごとの実タイルを網羅していないため）。各候補について
    zipのcentral directoryに実在するか確認してから取得する。
    見つからない場合は None を返す。

    progress_cb(phase, city, detail) で段階を通知する:
        phase="checking"    detail=zip_url        候補市町村のzipを確認中
        phase="downloading" detail=size(バイト)   実データ取得中（見つかった候補）
    「確認中」（数KB程度）と「取得中」（タイル1枚分、数十〜百MB超）は
    体感速度への影響が大きく異なるため、呼び出し側で表示を出し分けられる。"""
    os.makedirs(out_dir, exist_ok=True)
    tif_cache = os.path.join(out_dir, f"{code}.tif")
    if os.path.isfile(tif_cache):
        return tif_cache

    index = _load_index()
    cities = list(index["tile_to_cities"].get(code, []))
    for city in _candidate_cities_by_proximity(code):
        if city not in cities:
            cities.append(city)

    tried_urls = set()
    for city in cities:
        if cancel_cb and cancel_cb():
            return None
        for url in index["city_to_urls"].get(city, []):
            if url in tried_urls:
                continue
            tried_urls.add(url)
            if cancel_cb and cancel_cb():
                return None

            def _relay(phase, info, _city=city):
                if progress_cb:
                    detail = info[1] if phase == "downloading" else info
                    progress_cb(phase, _city, detail)

            data = fetch_entry_bytes(url, f"{code}.tif", progress_cb=_relay, cancel_cb=cancel_cb)
            if data is not None:
                with open(tif_cache, "wb") as fh:
                    fh.write(data)
                return tif_cache
    return None
