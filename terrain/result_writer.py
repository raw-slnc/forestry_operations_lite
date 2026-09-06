"""
解析結果の保存モジュール
  save_raster          : numpy配列 → GeoTIFF
  save_rgba_raster     : RGBA配列 → GeoTIFF
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


def save_rgba_raster(data, gt, crs_wkt, out_dir, name_prefix, overwrite=False):
    """RGBA/RGB配列をByte GeoTIFFとして保存。保存パスを返す"""
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    if data.ndim != 3 or data.shape[2] not in (3, 4):
        raise ValueError("RGBA raster must have shape (rows, cols, 3|4)")
    os.makedirs(out_dir, exist_ok=True)
    path = _resolve_path(out_dir, name_prefix, ".tif", overwrite)
    if overwrite and os.path.exists(path):
        os.remove(path)
    rows, cols, bands = data.shape
    drv = gdal.GetDriverByName("GTiff")
    create_opts = [
        "COMPRESS=LZW",
        "TILED=YES",
        "PHOTOMETRIC=RGB",
        "BIGTIFF=IF_NEEDED",
    ]
    if bands == 4:
        # 4帯目を非乗算アルファとしてタグ付け（EXTRASAMPLES=2）。
        # これがないと GDAL/QGIS がただの余分な帯として扱い透過しない。
        create_opts.append("ALPHA=YES")
    ds = drv.Create(path, cols, rows, bands, gdal.GDT_Byte, options=create_opts)
    ds.SetGeoTransform(gt)
    ds.SetProjection(crs_wkt)
    arr = data.astype(np.uint8, copy=False)
    color_interp = [gdal.GCI_RedBand, gdal.GCI_GreenBand, gdal.GCI_BlueBand]
    if bands == 4:
        color_interp.append(gdal.GCI_AlphaBand)
    for idx in range(bands):
        band = ds.GetRasterBand(idx + 1)
        band.WriteArray(arr[:, :, idx])
        band.SetColorInterpretation(color_interp[idx])
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


def dissolve_gpkg(src_path, out_dir, layer_name, overwrite=False):
    """GPKG の全ポリゴンを1つの dissolved GPKG として保存する。"""
    if not HAS_GDAL:
        raise RuntimeError("GDAL is not available")
    if not os.path.exists(src_path):
        raise FileNotFoundError(src_path)
    os.makedirs(out_dir, exist_ok=True)

    src_ds = ogr.Open(src_path)
    if src_ds is None:
        raise RuntimeError(f"Could not open vector: {src_path}")
    src_lyr = src_ds.GetLayer(0)
    if src_lyr is None:
        src_ds = None
        raise RuntimeError(f"No vector layer: {src_path}")

    geom_collection = ogr.Geometry(ogr.wkbGeometryCollection)
    for feat in src_lyr:
        geom = feat.GetGeometryRef()
        if geom is not None and not geom.IsEmpty():
            geom_collection.AddGeometry(geom)

    dissolved = None
    if not geom_collection.IsEmpty():
        dissolved = geom_collection.UnaryUnion()
    if dissolved is None or dissolved.IsEmpty():
        src_ds = None
        return None
    if ogr.GT_Flatten(dissolved.GetGeometryType()) == ogr.wkbPolygon:
        multi = ogr.Geometry(ogr.wkbMultiPolygon)
        multi.AddGeometry(dissolved)
        dissolved = multi

    path = _resolve_path(out_dir, layer_name, ".gpkg", overwrite)
    drv = ogr.GetDriverByName("GPKG")
    if overwrite and os.path.exists(path):
        drv.DeleteDataSource(path)
    dst_ds = drv.CreateDataSource(path)
    if dst_ds is None:
        src_ds = None
        raise RuntimeError(f"Could not create vector: {path}")

    srs = src_lyr.GetSpatialRef()
    dst_lyr = dst_ds.CreateLayer(layer_name, srs=srs, geom_type=ogr.wkbMultiPolygon)
    fd = ogr.FieldDefn("value", ogr.OFTInteger)
    dst_lyr.CreateField(fd)

    out_feat = ogr.Feature(dst_lyr.GetLayerDefn())
    out_feat.SetField("value", 1)
    out_feat.SetGeometry(dissolved)
    dst_lyr.CreateFeature(out_feat)

    out_feat = None
    dst_ds.FlushCache()
    dst_ds = None
    src_ds = None
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
