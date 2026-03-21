"""
解析結果の保存モジュール
  save_raster          : numpy配列 → GeoTIFF
  mask_to_polygons     : バイナリマスク → ポリゴン GPKG
  values_to_points     : 閾値超セル → ポイント GPKG
  mask_to_centroids    : 連結成分ごとの重心 → ポイント GPKG

  overwrite=True のとき固定ファイル名で上書き保存。
  overwrite=False（デフォルト）のときタイムスタンプ付きで追記保存。
"""
import os
import numpy as np
from datetime import datetime

try:
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    HAS_GDAL = True
except ImportError:
    HAS_GDAL = False

NODATA = -9999.0


def _ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _resolve_path(out_dir, name, ext, overwrite):
    """overwrite=True → 固定名、False → タイムスタンプ付き"""
    if overwrite:
        return os.path.join(out_dir, f"{name}{ext}")
    return os.path.join(out_dir, f"{name}_{_ts()}{ext}")


def save_raster(data, gt, crs_wkt, out_dir, name_prefix, overwrite=False):
    """numpy配列をGeoTIFF(LZW圧縮)として保存。保存パスを返す"""
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    os.makedirs(out_dir, exist_ok=True)
    path = _resolve_path(out_dir, name_prefix, ".tif", overwrite)
    if overwrite and os.path.exists(path):
        os.remove(path)
    rows, cols = data.shape
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, cols, rows, 1, gdal.GDT_Float32,
                    options=["COMPRESS=LZW", "TILED=YES"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(crs_wkt)
    arr = data.astype(np.float32)
    arr[np.isnan(arr)] = NODATA
    band = ds.GetRasterBand(1)
    band.WriteArray(arr)
    band.SetNoDataValue(NODATA)
    ds.FlushCache()
    ds = None
    return path


def mask_to_polygons(binary_mask, gt, crs_wkt, out_dir, layer_name, overwrite=False):
    """
    binary_mask(True=対象)をポリゴン化してGPKGに保存。
    保存パスを返す。
    """
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    os.makedirs(out_dir, exist_ok=True)
    rows, cols = binary_mask.shape

    # マスクをメモリラスタへ
    mem_drv = gdal.GetDriverByName("MEM")
    mem_ds = mem_drv.Create("", cols, rows, 1, gdal.GDT_Byte)
    mem_ds.SetGeoTransform(gt)
    mem_ds.SetProjection(crs_wkt)
    arr = binary_mask.astype(np.uint8)
    mem_ds.GetRasterBand(1).WriteArray(arr)

    path = _resolve_path(out_dir, layer_name, ".gpkg", overwrite)
    if overwrite and os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("GPKG")
    vec_ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromWkt(crs_wkt)
    lyr = vec_ds.CreateLayer(layer_name, srs=srs, geom_type=ogr.wkbMultiPolygon)
    fd = ogr.FieldDefn("value", ogr.OFTInteger)
    lyr.CreateField(fd)

    gdal.Polygonize(mem_ds.GetRasterBand(1), None, lyr, 0, [], callback=None)

    # value=0（対象外）の地物を削除
    lyr.SetAttributeFilter("value = 0")
    fids = [f.GetFID() for f in lyr]
    lyr.SetAttributeFilter(None)
    for fid in fids:
        lyr.DeleteFeature(fid)

    vec_ds.FlushCache()
    mem_ds = None
    vec_ds = None
    return path


def values_to_points(data, threshold_gt, gt, crs_wkt, out_dir, layer_name, overwrite=False):
    """
    data > threshold_gt のセルをポイントとしてGPKGに保存。
    保存パスを返す。
    """
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    os.makedirs(out_dir, exist_ok=True)
    path = _resolve_path(out_dir, layer_name, ".gpkg", overwrite)
    if overwrite and os.path.exists(path):
        os.remove(path)
    drv = ogr.GetDriverByName("GPKG")
    vec_ds = drv.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromWkt(crs_wkt)
    lyr = vec_ds.CreateLayer(layer_name, srs=srs, geom_type=ogr.wkbPoint)
    fd = ogr.FieldDefn("value", ogr.OFTReal)
    lyr.CreateField(fd)

    r_arr, c_arr = np.where((data > threshold_gt) & ~np.isnan(data))
    for r, c in zip(r_arr.tolist(), c_arr.tolist()):
        x = gt[0] + (c + 0.5) * gt[1]
        y = gt[3] + (r + 0.5) * gt[5]
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({x} {y})"))
        feat.SetField("value", float(data[r, c]))
        lyr.CreateFeature(feat)

    vec_ds.FlushCache()
    vec_ds = None
    return path
