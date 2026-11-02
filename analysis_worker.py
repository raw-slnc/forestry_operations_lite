"""
地形解析の計算をワーカースレッドで行う。

- TerrainAnalysisWorker: 斜度・集水・斜面安定・沢地形・流量・統合リスクを
  計算して一時フォルダへ保存する。画面（ウィジェット・QgsProject）には
  一切触らない。必要な値は開始前に params（dict）として写し取って渡す。
- 進捗は stage（段階キー）と progress（段階内の 0..1）で送り、表示は
  dockwidget 側の _AnalysisProgress が行う。
- 中止は cancel() でフラグを立て、計算側の progress_cb の中で止める。
  一時フォルダの片付け・フォルダ名の変更・レイヤー表示は dockwidget 側
  （メインスレッド）が done を受けてから行う。
"""

import traceback

import numpy as np
from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.core import Qgis, QgsMessageLog

from .terrain import analysis as ta
from .terrain import integration as ti
from .terrain import result_writer as rw


class AnalysisCancelled(Exception):
    """解析の中止（ワーカー内部用）。"""


class TerrainAnalysisWorker(QThread):
    """地形解析をバックグラウンドで行う。"""

    # 段階キー（_ANALYSIS_STAGE_WEIGHTS のキー）
    stage = pyqtSignal(str)
    # 段階内の進み具合 0..1
    progress = pyqtSignal(float)
    # (saved [(label, path, kind)], cancelled, error, warning)
    done = pyqtSignal(object, bool, str, str)

    # progress を送る最小の変化量（スレッド間の signal を増やしすぎない）
    _EMIT_STEP = 0.002

    def __init__(self, dem, dsm_data, params, tmp_folder, parent=None):
        """
        dem       : 切り出し・リサンプル済みの DEMLoader（data/gt/crs_wkt/cell_size）
        dsm_data  : DEM と同じ形に切り出した DSM 配列、無ければ None
        params    : 開始時に画面から写し取った解析条件（dict）
        """
        super().__init__(parent)
        self._dem = dem
        self._dsm_data = dsm_data
        self._p = params
        self._tmp = tmp_folder
        self._cancelled = False
        self._last_frac = -1.0
        self.flow_conditions = None

    def cancel(self):
        self._cancelled = True

    def _check(self):
        if self._cancelled:
            raise AnalysisCancelled()

    def _stage(self, key):
        self._check()
        self._last_frac = -1.0
        self.stage.emit(key)

    def _prog(self, lo=0.0, hi=1.0):
        """段階内の lo..hi に割り当てた progress_cb を返す（中止の確認も兼ねる）。"""
        def _cb(frac):
            self._check()
            f = lo + (hi - lo) * frac
            if f - self._last_frac >= self._EMIT_STEP or f >= 1.0:
                self._last_frac = f
                self.progress.emit(f)
        return _cb

    def run(self):
        saved = []
        try:
            warning = self._compute(saved)
        except AnalysisCancelled:
            self.done.emit(saved, True, "", "")
            return
        except Exception as exc:  # ワーカーの外へ例外を出さない
            QgsMessageLog.logMessage(
                "Terrain analysis failed:\n" + traceback.format_exc(),
                "Forestry Operations Lite", Qgis.Warning)
            self.done.emit(saved, False, str(exc), "")
            return
        self.done.emit(saved, False, "", warning)

    def _compute(self, saved):
        dem = self._dem
        p = self._p
        tmp_folder = self._tmp
        multi_flow = p["flow_method"] in ("mfd", "dinf")

        # ── 共通前処理（斜度・SHC・D8流向・流積） ──
        self._stage("prep")
        slope = ta.compute_slope_deg(dem.data, dem.cell_size)
        # slope / SHC: FOP ゾーニング用。QGIS レイヤには追加しない。
        rw.save_raster(slope.astype(np.float32), dem.gt, dem.crs_wkt,
                       tmp_folder, "slope", overwrite=True)
        shc = ta.compute_shc(dem.data, dem.cell_size)
        rw.save_raster(shc, dem.gt, dem.crs_wkt, tmp_folder,
                       "shc", overwrite=True)
        self._prog()(1.0)

        if p["stability"] or p["valley"] or p["flow"]:
            self._stage("routing")
            fdir = ta.d8_flow_direction(dem.data)
            self._prog()(0.05)
            accum = ta.flow_accumulation(dem.data, fdir,
                                         progress_cb=self._prog(0.05, 1.0))

        # ── 斜面安定解析 ──
        if p["stability"]:
            self._stage("stability")
            fs = ta.stability_fs(
                slope,
                phi_deg=p["phi_deg"],
                c_kpa=p["c_kpa"],
                z_m=p["z_m"],
                m=p["m_sat"],
            )
            path = rw.save_raster(fs, dem.gt, dem.crs_wkt, tmp_folder,
                                  "stability_fs", overwrite=True)
            saved.append(("Slope Stability FS", path, "raster"))
            mask = (fs < p["fs_thresh"]) & ~np.isnan(fs)
            self._prog()(0.02)
            if mask.any():
                path = rw.mask_to_polygons(
                    mask, dem.gt, dem.crs_wkt, tmp_folder,
                    "unstable_zones", overwrite=True,
                    progress_cb=self._prog(0.02, 1.0))
                saved.append(("Unstable zones", path, "vector"))

        # ── 沢地形判定（TWI） ──
        if p["valley"]:
            self._stage("valley")
            # MFD/D∞ は集水ループが段階の約95%（実測）。D8 はポリゴン化が大半。
            poly0 = 0.95 if multi_flow else 0.02
            # Flow Direction Method（Valley/TWI 専用）の選択で集水を計算
            if p["flow_method"] == "mfd":
                prop = ta.mfd_proportions(dem.data, dem.cell_size)
            elif p["flow_method"] == "dinf":
                prop = ta.dinf_proportions(dem.data, dem.cell_size)
            if multi_flow:
                self._prog()(0.02)
                accum_v = ta.multi_flow_accumulation(
                    dem.data, prop, progress_cb=self._prog(0.02, 0.95))
            else:  # D8（既定・Flow Estimation と共通の集水）
                accum_v = accum
            twi = ta.compute_twi(accum_v, slope, dem.cell_size)
            path = rw.save_raster(twi, dem.gt, dem.crs_wkt, tmp_folder,
                                  "twi", overwrite=True)
            saved.append(("TWI", path, "raster"))
            min_cells = p["min_area"] / (dem.cell_size ** 2)
            vmask = (twi >= p["twi_thresh"]) \
                & (accum_v >= min_cells) & ~np.isnan(twi)
            self._prog()(poly0)
            if vmask.any():
                path = rw.mask_to_polygons(
                    vmask, dem.gt, dem.crs_wkt, tmp_folder,
                    "valley_zones", overwrite=True,
                    progress_cb=self._prog(poly0, 1.0))
                saved.append(("Valley Terrain", path, "vector"))

        # ── 流量推測（修正合理式 + 到達時間 Tc ルーティング） ──
        if p["flow"]:
            self._stage("flow")
            c_local, velocity_coef, self.flow_conditions = ta.prepare_flow_coefficients(
                dem.data, self._dsm_data,
                runoff_coef=p["runoff"], velocity_coef=p["velocity_coef"])
            self.flow_conditions["dsm"] = dict(p.get("dsm_info", {}))
            if (self._dsm_data is not None
                    and not self.flow_conditions["dsm_valid_cells"]):
                self.flow_conditions["dsm"]["status"] = "no_valid_cells"
            self.flow_conditions["plugin_version"] = p.get("plugin_version", "")
            self.flow_conditions["grid"] = {
                "shape": list(dem.data.shape),
                "geotransform": [float(value) for value in dem.gt],
                "crs_wkt": dem.crs_wkt,
                "cell_size_m": float(dem.cell_size),
            }
            self._prog()(0.01)
            local_tt = ta.compute_travel_time(
                dem.data, fdir, dem.cell_size,
                velocity_coef=velocity_coef,
            )
            # 実測の内訳：Tc 約27% / 時間‐面積法 約69% / 保存 残り
            tc = ta.compute_tc(dem.data, fdir, local_tt,
                               progress_cb=self._prog(0.01, 0.27))

            # 時間‐面積法（Clark 単位図法）による負荷3指標
            q_peak, q_mean, _tc = ta.time_area_flow_metrics(
                dem.data, fdir, local_tt, tc, dem.cell_size,
                c_grid=c_local,
                duration_h=p["duration_h"],
                i_peak_mmh=p["rainfall_mmh"],
                total_mm=p["total_mm"],
                **ta.FLOW_ROUTING_OPTIONS,
                progress_cb=self._prog(0.27, 0.96),
            )
            path = rw.save_raster(tc, dem.gt, dem.crs_wkt, tmp_folder,
                                  "tc", overwrite=True)
            saved.append(("Time load Tc[h]", path, "raster"))
            path = rw.save_raster(q_peak, dem.gt, dem.crs_wkt, tmp_folder,
                                  "flow_peak", overwrite=True)
            saved.append(("Peak load Qp[m³/s]", path, "raster"))
            path = rw.save_raster(q_mean, dem.gt, dem.crs_wkt, tmp_folder,
                                  "flow_mean", overwrite=True)
            saved.append(("Mean load Qm[m³/s]", path, "raster"))

        # ── 統合リスク指標（FS/TWI/流量のいずれかがあれば自動生成） ──
        self._stage("integrate")
        warning = ""
        try:
            int_result = ti.build_integrated_index(
                tmp_folder,
                analysis_prefix="",
                fs_caution=p["fs_thresh"],
                progress_cb=self._prog(),
            )
            if int_result["integrated_risk_index"]:
                saved.append(("Overall Risk Index", int_result["integrated_risk_index"], "raster"))
            if int_result["integrated_high_risk"]:
                saved.append(("High Risk Areas", int_result["integrated_high_risk"], "vector"))
        except FileNotFoundError:
            pass  # 対象ラスタなし（単独の湧水のみ解析時など）
        except AnalysisCancelled:
            raise
        except Exception as e_int:  # 統合指標の失敗は警告に留める
            warning = f"Integrated risk error: {e_int}"
        self._check()
        return warning
