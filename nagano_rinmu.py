"""長野県 林務部 0.5mメッシュDEM（2013〜2014年度計測）ユーティリティ。

データ配布元:
    https://www.geospatial.jp/ckan/dataset/nagano-dem
    （長野県林務部、航空レーザ測量成果、0.5mメッシュGeoTIFF、EPSG:6676）

配布は12個の地域単位zip（県全域、市町村単位ではない）で、1zipに
数百〜二千弱のタイルを内包する（例: 佐久地域=H24-30(saku).zip、1850タイル）。
砂防課DEM（nagano_sabo.py）とは測量年度・タイル体系とも別物。

タイルコード体系（EPSG:6676、平面直角第8系）:
    "{zone2桁}{シート2文字}{行1桁}{列1桁}{外側Z1桁}{内側Z1桁}"
    例: "08ID5721" → シート"08ID57"（行5列7）+ シート内位置"21"

    2026-09-23、実データ突合で確認・6点完全一致（すべて佐久地域データ、
    シート"08ID57"/"08ID58"内）:
      08ID5721 / 08ID5722 / 08ID5723 / 08ID5724 / 08ID5741 / 08ID5833

    シート(4000m×3000m、行・列は1桁の数字)は**全県共通の単純な式では求まらない**
    （2文字部分が"ID"だけでなく"MB"等も存在し、地域ごとに異なる文字コードを
    持つため）。そのため座標→シートコードの対応づけは、公式の索引shp
    （全体索引図_図郭.shp×全体索引図_図郭名.shp、1073シート・県全域）を
    そのまま突合して作った `data/nagano_rinmu_sheet_index.json`
    （{シートコード: [xmin,ymin,xmax,ymax]}）を参照する。

    シート内の位置（末尾2桁）はシートのbboxさえ分かれば式で確定できる
    （2階層Z曲線、nagano_sabo.py/VSの2022年データと同一規則）:
      外側2×2ブロック(10の位、2000m×1500m) → 内側2×2ブロック(1の位、1000m×750m)
      1=左上/2=右上/3=左下/4=右下

タイル→zip URL の対応（市町村のような細かい索引は無く、12個の地域zipの
どれに入っているかは事前に分からない）。central directory 実在確認で
順に試す（12個なので市町村版のような近傍ヒューリスティックは不要）。

【既知の制約】
- DSMは提供されていない（DCHMもR3/R4測量のみで2013-2014年データに対応するものは無い）。
- シート索引はXLSXでなくshpからの静的生成であり、データセット更新には追従しない。
"""

import json
import os

from .terrain.remote_zip import fetch_entry_bytes

_SHEET_INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "nagano_rinmu_sheet_index.json")
_sheet_index_cache = None

SHEET_W = 4000
SHEET_H = 3000
_QUAD = {1: (0, 0), 2: (1, 0), 3: (0, 1), 4: (1, 1)}  # (col,row) 1=TL,2=TR,3=BL,4=BR

# 12地域zip（県全域、市町村より粗い単位）。2026-09-23、CKAN nagano-dem データセットの
# リソース一覧から取得（プレサインではない固定URL、Range対応確認済み）。
_BASE_URL = ("https://gsic-opendata.s3.ap-northeast-1.amazonaws.com/local-gov/nagano/"
             "forestry-research-center/dem/nagano-dem/download/nagano/dem/")
ZIP_URLS = [
    _BASE_URL + "H24-27(kamiina).zip",
    _BASE_URL + "H24-28(shimoina).zip",
    _BASE_URL + "H24-29(kiso).zip",
    _BASE_URL + "H24-30(saku).zip",
    _BASE_URL + "H24-31(jyousyou).zip",
    _BASE_URL + "H24-32(suwa).zip",
    _BASE_URL + "H24-33(matsumoto).zip",
    _BASE_URL + "H24-34(kitaazumi1).zip",
    _BASE_URL + "H24-34(kitaazumi2).zip",
    _BASE_URL + "H24-35(nagano).zip",
    _BASE_URL + "H24-36(hokusin1).zip",
    _BASE_URL + "H26-30(hokusin2).zip",
]


def _load_sheet_index():
    global _sheet_index_cache
    if _sheet_index_cache is None:
        with open(_SHEET_INDEX_PATH, encoding="utf-8") as f:
            _sheet_index_cache = json.load(f)
    return _sheet_index_cache


def _sheet_for_point(x: float, y: float):
    """座標を含むシートの (code, xmin, ymin, xmax, ymax) を返す。無ければ None。"""
    for code, (xmin, ymin, xmax, ymax) in _load_sheet_index().items():
        if xmin <= x <= xmax and ymin <= y <= ymax:
            return code, xmin, ymin, xmax, ymax
    return None


def _tile_code_in_sheet(sheet_xmin, sheet_ymax, x, y):
    """シート左上(sheet_xmin, sheet_ymax)基準で、座標(x,y)が属す2桁Z曲線位置を返す。"""
    dx = x - sheet_xmin
    dy = sheet_ymax - y
    outer_col = min(int(dx // 2000), 1)
    outer_row = min(int(dy // 1500), 1)
    inner_col = min(int((dx - outer_col * 2000) // 1000), 1)
    inner_row = min(int((dy - outer_row * 1500) // 750), 1)
    tens = next(k for k, v in _QUAD.items() if v == (outer_col, outer_row))
    units = next(k for k, v in _QUAD.items() if v == (inner_col, inner_row))
    return f"{tens}{units}"


def tile_code_for_point(x: float, y: float):
    """EPSG:6676座標からタイルコード（例 "08ID5721"）を返す。範囲外ならNone。"""
    hit = _sheet_for_point(x, y)
    if hit is None:
        return None
    sheet_code, xmin, ymin, xmax, ymax = hit
    suffix = _tile_code_in_sheet(xmin, ymax, x, y)
    return f"{sheet_code}{suffix}"


def tile_bbox(code: str):
    """タイルコード（例 "08ID5721"）→ (xmin, ymin, xmax, ymax) EPSG:6676。
    シート索引に無いコードは ValueError。"""
    sheet_code, suffix = code[:6], code[6:8]
    index = _load_sheet_index()
    if sheet_code not in index:
        raise ValueError(f"Unknown sheet: {sheet_code}")
    xmin, ymin, xmax, ymax = index[sheet_code]
    tens, units = int(suffix[0]), int(suffix[1])
    oc, orow = _QUAD[tens]
    ic, irow = _QUAD[units]
    x = xmin + oc * 2000 + ic * 1000
    y = ymax - orow * 1500 - irow * 750
    return x, y - 750, x + 1000, y


def tiles_for_extent(xmin: float, ymin: float, xmax: float, ymax: float):
    """EPSG:6676 bbox に重なるタイルコードのリストを返す。"""
    codes = []
    index = _load_sheet_index()
    for sheet_code, (sxmin, symin, sxmax, symax) in index.items():
        if sxmax < xmin or sxmin > xmax or symax < ymin or symin > ymax:
            continue
        # このシートと重なる細タイル(1000m x 750m、4x4=16枚)を列挙
        col_lo = max(int((xmin - sxmin) // 1000), 0)
        col_hi = min(int((xmax - sxmin) // 1000), 3)
        row_lo = max(int((symax - ymax) // 750), 0)
        row_hi = min(int((symax - ymin) // 750), 3)
        for row in range(row_lo, row_hi + 1):
            for col in range(col_lo, col_hi + 1):
                outer_col, inner_col = divmod(col, 2)
                outer_row, inner_row = divmod(row, 2)
                tens = next(k for k, v in _QUAD.items() if v == (outer_col, outer_row))
                units = next(k for k, v in _QUAD.items() if v == (inner_col, inner_row))
                codes.append(f"{sheet_code}{tens}{units}")
    return codes


def download_tile_tif(code: str, out_dir: str, cancel_cb=None, progress_cb=None):
    """タイルコードに対応するGeoTIFFを取得しローカルパスを返す。
    市町村のような索引が無いため、12地域zipを順に試す（central directory確認）。
    見つからない場合は None を返す。

    progress_cb(phase, zip_label, detail) — nagano_sabo.download_tile_tif と同じ形。
    """
    os.makedirs(out_dir, exist_ok=True)
    tif_cache = os.path.join(out_dir, f"{code}.tif")
    if os.path.isfile(tif_cache):
        return tif_cache

    for url in ZIP_URLS:
        if cancel_cb and cancel_cb():
            return None
        label = os.path.basename(url)

        def _relay(phase, info, _label=label):
            if progress_cb:
                detail = (info[1], info[2]) if phase == "downloading" else info
                progress_cb(phase, _label, detail)

        data = fetch_entry_bytes(url, f"{code}_2013.tif", progress_cb=_relay, cancel_cb=cancel_cb)
        if data is None:
            data = fetch_entry_bytes(url, f"{code}_2014.tif", progress_cb=_relay, cancel_cb=cancel_cb)
        if data is not None:
            with open(tif_cache, "wb") as fh:
                fh.write(data)
            return tif_cache
    return None
