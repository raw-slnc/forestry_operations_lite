"""長野県 DCHM（樹冠高モデル、R3/R4計測）ユーティリティ。DSM算出用。

データ配布元:
    https://www.geospatial.jp/ckan/dataset/r4_dchm-r4 （R4計測）
    https://www.geospatial.jp/ckan/dataset/r3_dchm    （R3計測）
    （長野県建設部砂防課、QGISプラグイン「FUSION for Processing」で作成、
     林木以外の建物等の高さも含む＝地表面からの高さ全般。GeoTIFF、EPSG:6676）

2026-09-23、実データ突合で確認: DCHMの個々のタイル（例 "G8-a9.tif"）は
nagano_sabo.py で確立した DEM と全く同じ座標式・同じタイルサイズ
（3000m×2500m）を使っている。よって座標⇔タイルコード変換は
nagano_sabo.tile_bbox() / tiles_for_extent() をそのまま流用する。

配布zip（"長野県図郭割50,000単位"）は、細かいタイルコードの英字部分だけを
取り出したもの（例 "G8-a9" → シート "G-a"）に一致する。1シート=10行×10列=
100タイル。`Clp50000.shp`（図郭50000索引）の座標と全36シートで整合確認済み。
大きいシートは "_1", "_2", ... に分割配布されている（CKANのファイル名から
そのまま分かるため、DEM側のような市町村対応表は不要）。

R3計測・R4計測で範囲が重複するシートもあるため、シートごとに両方のURLを
候補として保持し、central directory 実在確認で正しいものを選ぶ
（DEM側と同じ方式、terrain/remote_zip.fetch_entry_bytes）。

【既知の制約】
- 35/36シートのみデータあり（1シートは未提供の可能性、未調査）。
- R3/R4以外の年度のDCHMは無い（DEM側R5-R7に対応するDCHMは未確認）。
"""

import json
import os

from .nagano_sabo import tile_bbox, tiles_for_extent  # noqa: F401  (再エクスポート)
from .terrain.remote_zip import fetch_entry_bytes

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "data", "nagano_dchm_index.json")
_index_cache = None


def _load_index():
    global _index_cache
    if _index_cache is None:
        with open(_INDEX_PATH, encoding="utf-8") as f:
            _index_cache = json.load(f)
    return _index_cache


def sheet_code_for_tile(tile_code: str) -> str:
    """細かいタイルコード（例 "G8-a9"）→ 50000スケールシート名（例 "G-a"）。"""
    row_part, col_part = tile_code.split("-")
    return f"{row_part[0]}-{col_part[0]}"


def download_tile_tif(code: str, out_dir: str, cancel_cb=None, progress_cb=None):
    """DCHMタイルを取得しローカルパスを返す。見つからない場合は None。
    該当タイルの50000シートに紐づく全zip候補（R3/R4計測、分割ファイル含む）を
    central directory実在確認しながら順に試す。

    progress_cb(phase, sheet, detail) で段階を通知する（nagano_sabo.download_tile_tif
    と同じ考え方。詳細はそちらの docstring 参照）。"""
    os.makedirs(out_dir, exist_ok=True)
    tif_cache = os.path.join(out_dir, f"{code}.tif")
    if os.path.isfile(tif_cache):
        return tif_cache

    sheet = sheet_code_for_tile(code)
    urls = _load_index()["sheet_to_urls"].get(sheet, [])
    for url in urls:
        if cancel_cb and cancel_cb():
            return None

        def _relay(phase, info, _sheet=sheet):
            if progress_cb:
                detail = info[1] if phase == "downloading" else info
                progress_cb(phase, _sheet, detail)

        data = fetch_entry_bytes(url, f"{code}.tif", progress_cb=_relay, cancel_cb=cancel_cb)
        if data is not None:
            with open(tif_cache, "wb") as fh:
                fh.write(data)
            return tif_cache
    return None
