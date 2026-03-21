"""
解析結果の統合指標作成

目的:
  terrain_analysis 配下の出力ラスターを1つの指標に統合し、
  現場で重ね合わせ判断しやすいデータを作る。

出力:
  - integrated_risk_index.tif  : 0-6 の統合リスク指標
  - integrated_high_risk.gpkg  : 高リスク域(デフォルト: index>=3)
"""
import glob
import math
import os
from dataclasses import dataclass

import numpy as np

from . import result_writer as rw

try:
    from osgeo import gdal
    gdal.UseExceptions()
    HAS_GDAL = True
except ImportError:
    HAS_GDAL = False


@dataclass
class RasterData:
    data: np.ndarray
    gt: tuple
    crs_wkt: str
    nodata: float | None


def _latest_path(out_dir, patterns):
    files = []
    for pat in patterns:
        files.extend(glob.glob(os.path.join(out_dir, pat)))
    if not files:
        return None
    files = sorted(files)
    return files[-1]


def _prefixed_path(out_dir, prefix, name, ext):
    """analysis_prefix 指定時のファイルパスを返す（存在しなければ None）"""
    path = os.path.join(out_dir, f"{prefix}{name}{ext}")
    return path if os.path.exists(path) else None


def _read_raster(path):
    ds = gdal.Open(path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Cannot open raster: {path}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        arr[arr == nodata] = np.nan
    return RasterData(arr, ds.GetGeoTransform(), ds.GetProjection(), nodata)


def build_integrated_index(
    out_dir,
    analysis_prefix="",
    fs_caution=1.5,
    twi_caution=8.0,
    flow_caution=0.5,
    high_risk_threshold=3,
    overwrite=True,
):
    """
    指標を統合して index ラスタを出力する。

    analysis_prefix: 解析番号プレフィクス (例: "0010_")。指定時はそのファイルを使用。
                     空文字の場合は最新ファイルを自動検索（レガシー動作）。

    index の内訳:
      FS:   <1.0 => +3, <fs_caution => +2
      TWI:  >=twi_caution => +1
      Flow: >=flow_caution => +1, >=2*flow_caution => +2
    """
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    if not os.path.isdir(out_dir):
        raise FileNotFoundError(f"Output directory not found: {out_dir}")

    if analysis_prefix:
        fs_path   = _prefixed_path(out_dir, analysis_prefix, "stability_fs",  ".tif")
        twi_path  = _prefixed_path(out_dir, analysis_prefix, "twi",           ".tif")
        flow_path = _prefixed_path(out_dir, analysis_prefix, "flow_peak",   ".tif")
    else:
        fs_path   = _latest_path(out_dir, ["stability_fs.tif", "stability_fs_*.tif"])
        twi_path  = _latest_path(out_dir, ["twi.tif", "twi_*.tif"])
        flow_path = _latest_path(out_dir, ["flow_peak.tif", "flow_peak_*.tif"])

    if fs_path is None and twi_path is None and flow_path is None:
        raise FileNotFoundError("No rasters found for integration (FS/TWI/flow)")

    base = None
    for p in (fs_path, twi_path, flow_path):
        if p:
            base = _read_raster(p)
            break

    rows, cols = base.data.shape
    index = np.zeros((rows, cols), dtype=np.float32)
    valid = np.zeros((rows, cols), dtype=bool)

    if fs_path:
        fs = _read_raster(fs_path).data
        fs = np.where(np.isinf(fs), np.nanmax(np.where(np.isfinite(fs), fs, np.nan)), fs)
        fs_valid = ~np.isnan(fs)
        valid |= fs_valid
        index += np.where(fs < 1.0, 3.0, np.where(fs < fs_caution, 2.0, 0.0)).astype(np.float32)

    if twi_path:
        twi = _read_raster(twi_path).data
        twi_valid = ~np.isnan(twi)
        valid |= twi_valid
        index += np.where(twi >= twi_caution, 1.0, 0.0).astype(np.float32)

    if flow_path:
        flow = _read_raster(flow_path).data
        flow_valid = ~np.isnan(flow)
        valid |= flow_valid
        flow_score = np.where(
            flow >= (flow_caution * 2.0),
            2.0,
            np.where(flow >= flow_caution, 1.0, 0.0),
        )
        index += flow_score.astype(np.float32)

    index[~valid] = np.nan

    index_path = rw.save_raster(
        index, base.gt, base.crs_wkt, out_dir,
        f"{analysis_prefix}integrated_risk_index", overwrite=True
    )

    high_mask = (index >= float(high_risk_threshold)) & ~np.isnan(index)
    zone_path = None
    if high_mask.any():
        zone_path = rw.mask_to_polygons(
            high_mask, base.gt, base.crs_wkt, out_dir,
            f"{analysis_prefix}integrated_high_risk", overwrite=True
        )

    return {
        "integrated_risk_index": index_path,
        "integrated_high_risk": zone_path,
        "sources": {"fs": fs_path, "twi": twi_path, "flow": flow_path},
        "thresholds": {
            "fs_caution": fs_caution,
            "twi_caution": twi_caution,
            "flow_caution": flow_caution,
            "high_risk_threshold": high_risk_threshold,
        },
    }


def build_multiplicative_index(
    out_dir,
    analysis_prefix="",
    fs_caution=1.5,
    twi_caution=8.0,
    flow_caution=0.5,
    overwrite=True,
):
    """
    乗算型リスク指標を出力する（リスク分析用）。

    加算型と異なり、複数指標が重なる地点のリスクが急激に高くなる。
    平坦・安定地は低値に収まり、複合危険地は高値に突出する。

    各指標の係数:
      FS:   <1.0 => ×4, <fs_caution => ×2, else ×1
      TWI:  >=twi_caution => ×2, else ×1
      Flow: >=2*flow_caution => ×4, >=flow_caution => ×2, else ×1

    出力範囲: 1（全安全）〜32（全危険）
    出力ファイル: {prefix}integrated_risk_multiplicative.tif
    """
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    if not os.path.isdir(out_dir):
        raise FileNotFoundError(f"Output directory not found: {out_dir}")

    if analysis_prefix:
        fs_path   = _prefixed_path(out_dir, analysis_prefix, "stability_fs", ".tif")
        twi_path  = _prefixed_path(out_dir, analysis_prefix, "twi",          ".tif")
        flow_path = _prefixed_path(out_dir, analysis_prefix, "flow_peak",    ".tif")
    else:
        fs_path   = _latest_path(out_dir, ["stability_fs.tif", "stability_fs_*.tif"])
        twi_path  = _latest_path(out_dir, ["twi.tif", "twi_*.tif"])
        flow_path = _latest_path(out_dir, ["flow_peak.tif", "flow_peak_*.tif"])

    if fs_path is None and twi_path is None and flow_path is None:
        raise FileNotFoundError("No rasters found for integration (FS/TWI/flow)")

    base = None
    for p in (fs_path, twi_path, flow_path):
        if p:
            base = _read_raster(p)
            break

    rows, cols = base.data.shape
    score = np.ones((rows, cols), dtype=np.float32)
    valid = np.zeros((rows, cols), dtype=bool)

    if fs_path:
        fs = _read_raster(fs_path).data
        fs = np.where(np.isinf(fs), np.nanmax(np.where(np.isfinite(fs), fs, np.nan)), fs)
        fs_valid = ~np.isnan(fs)
        valid |= fs_valid
        fs_factor = np.where(fs < 1.0, 4.0, np.where(fs < fs_caution, 2.0, 1.0))
        score *= np.where(fs_valid, fs_factor, 1.0).astype(np.float32)

    if twi_path:
        twi = _read_raster(twi_path).data
        twi_valid = ~np.isnan(twi)
        valid |= twi_valid
        twi_factor = np.where(twi >= twi_caution, 2.0, 1.0)
        score *= np.where(twi_valid, twi_factor, 1.0).astype(np.float32)

    if flow_path:
        flow = _read_raster(flow_path).data
        flow_valid = ~np.isnan(flow)
        valid |= flow_valid
        flow_factor = np.where(
            flow >= (flow_caution * 2.0), 4.0,
            np.where(flow >= flow_caution, 2.0, 1.0),
        )
        score *= np.where(flow_valid, flow_factor, 1.0).astype(np.float32)

    score[~valid] = np.nan

    out_path = rw.save_raster(
        score, base.gt, base.crs_wkt, out_dir,
        f"{analysis_prefix}integrated_risk_multiplicative", overwrite=overwrite
    )

    return {
        "integrated_risk_multiplicative": out_path,
        "sources": {"fs": fs_path, "twi": twi_path, "flow": flow_path},
    }


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        if math.isfinite(v):
            return f"{v:.3f}"
        return str(v)
    return str(v)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="terrain_analysis の統合リスク指標を作成")
    parser.add_argument("out_dir", help="terrain_analysis ディレクトリ")
    parser.add_argument("--fs-caution", type=float, default=1.5)
    parser.add_argument("--twi-caution", type=float, default=8.0)
    parser.add_argument("--flow-caution", type=float, default=0.5)
    parser.add_argument("--high-risk-threshold", type=float, default=3.0)
    parser.add_argument("--no-overwrite", action="store_true")
    args = parser.parse_args()

    result = build_integrated_index(
        args.out_dir,
        fs_caution=args.fs_caution,
        twi_caution=args.twi_caution,
        flow_caution=args.flow_caution,
        high_risk_threshold=args.high_risk_threshold,
        overwrite=not args.no_overwrite,
    )

    print("integrated_risk_index:", _fmt(result["integrated_risk_index"]))
    print("integrated_high_risk:", _fmt(result["integrated_high_risk"]))
    print("source_fs:", _fmt(result["sources"]["fs"]))
    print("source_twi:", _fmt(result["sources"]["twi"]))
    print("source_flow:", _fmt(result["sources"]["flow"]))
