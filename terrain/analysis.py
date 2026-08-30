"""
地形解析アルゴリズム群
  compute_slope_deg  : Horn法で傾斜角[度]を計算
  compute_curvature  : 平均曲率（ラプラシアン）を計算
  compute_shc        : SHC = 曲率の局所標準偏差（FOP用）
  d8_flow_direction  : D8流向コードを返す
  flow_accumulation  : 上流集水セル数を累積（D8）
  mfd_proportions    : MFD（多方向）の分配率 (rows,cols,8) を返す
  dinf_proportions   : D∞（Tarboton）の分配率 (rows,cols,8) を返す
  multi_flow_accumulation : 分配率から上流集水を累積（MFD/D∞ 共通）
  compute_twi        : TWI = ln(A / tan(β))
  stability_fs       : 無限斜面安定解析（FS）
  rational_flow      : 合理式による流量推測 [m³/s]
  cum_travel_time_to_outlet : 各セル→出口の累積移動時間（時間‐面積法の基準）
  triangular_hyetograph     : 三角形の設計ハイエトグラフ
  time_area_flow_metrics    : 時間‐面積法/Clark 単位図法による負荷3指標
"""
import numpy as np


def compute_slope_deg(dem, cell_size):
    """Horn法で傾斜角（度）を計算"""
    pad = np.pad(dem, 1, mode="edge")
    dzdx = (pad[1:-1, 2:] - pad[1:-1, :-2]) / (2.0 * cell_size)
    dzdy = (pad[2:, 1:-1] - pad[:-2, 1:-1]) / (2.0 * cell_size)
    slope_rad = np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2))
    result = np.degrees(slope_rad)
    result[np.isnan(dem)] = np.nan
    return result


def compute_curvature(dem, cell_size):
    """
    平均曲率（ラプラシアン近似）を計算する。
    正値=凸地形（尾根）、負値=凹地形（谷）。
    """
    pad = np.pad(dem, 1, mode="edge")
    r = (pad[1:-1, 2:] - 2.0 * dem + pad[1:-1, :-2]) / (cell_size ** 2)  # d²z/dx²
    t = (pad[2:, 1:-1] - 2.0 * dem + pad[:-2, 1:-1]) / (cell_size ** 2)  # d²z/dy²
    curv = -(r + t)
    curv[np.isnan(dem)] = np.nan
    return curv


def compute_shc(dem, cell_size, window=5):
    """
    SHC（Surface Height Complexity）: 曲率の局所標準偏差。

    もりぞんの災害リスク軸「地形の複雑さ」に相当する指標。
    FOLでは可視化レイヤとしては使用せず、FOP のゾーニング計算用に
    ラスタとして出力する。

    window : 標準偏差を計算するウィンドウサイズ（奇数推奨）。デフォルト=5
    """
    from scipy.ndimage import generic_filter

    curv = compute_curvature(dem, cell_size)

    def _nanstd(v):
        v = v[~np.isnan(v)]
        return np.std(v) if len(v) > 1 else 0.0

    shc = generic_filter(curv, _nanstd, size=window, mode="nearest")
    shc[np.isnan(dem)] = np.nan
    return shc.astype(np.float32)


def d8_flow_direction(dem):
    """
    D8流向: E=1,SE=2,S=4,SW=8,W=16,NW=32,N=64,NE=128
    最急降下方向を返す。平坦/ピット/NoData=0。
    """
    rows, cols = dem.shape
    # (dr, dc, code, 距離係数)
    NEIGHBORS = [
        (-1, -1, 32, 1.4142), (-1, 0, 64, 1.0), (-1, 1, 128, 1.4142),
        (0, -1, 16, 1.0),                          (0, 1,   1, 1.0),
        (1, -1,   8, 1.4142),  (1, 0,  4, 1.0),  (1, 1,   2, 1.4142),
    ]
    flow_dir = np.zeros((rows, cols), dtype=np.int16)
    max_slope = np.full((rows, cols), -np.inf)
    pad = np.pad(dem, 1, constant_values=np.nan)
    center = pad[1:-1, 1:-1]
    valid = ~np.isnan(center)

    for dr, dc, code, dist in NEIGHBORS:
        nb = pad[1 + dr: rows + 1 + dr, 1 + dc: cols + 1 + dc]
        slope = (center - nb) / dist
        nb_valid = ~np.isnan(nb)
        mask = valid & nb_valid & (slope > max_slope)
        flow_dir[mask] = code
        max_slope[mask] = slope[mask]

    # 平坦（max_slope==0）またはピット（max_slope<0）は流向なし=0
    flow_dir[valid & (max_slope <= 0)] = 0

    return flow_dir


def flow_accumulation(dem, flow_dir, weight=None):
    """D8流向から上流集水セル数（または重み和）を累積（標高降順に処理）

    weight: None の場合は各セル=1（通常の集水セル数）。
            2D配列を渡すと各セルの値を重みとして上流側に累積する。
            上流加重平均 = flow_accumulation(weight) / flow_accumulation()
    """
    rows, cols = dem.shape
    accum = (np.ones((rows, cols), dtype=np.float64)
             if weight is None
             else weight.astype(np.float64).copy())
    DIR_OFFSET = {
        1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
        16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
    }
    valid_mask = ~np.isnan(dem)
    r_arr, c_arr = np.where(valid_mask)
    elev_arr = dem[r_arr, c_arr]
    order = np.argsort(-elev_arr)  # 高い順

    for idx in order:
        r, c = int(r_arr[idx]), int(c_arr[idx])
        d = int(flow_dir[r, c])
        if d in DIR_OFFSET:
            dr, dc = DIR_OFFSET[d]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid_mask[nr, nc]:
                accum[nr, nc] += accum[r, c]

    return accum


# 8近傍の順序（multi-flow 分配率 prop の第3軸インデックス → (dr, dc)）
_NB8 = [(-1, -1), (-1, 0), (-1, 1),
        (0, -1),           (0, 1),
        (1, -1),  (1, 0),  (1, 1)]


def mfd_proportions(dem, cell_size, p=1.1):
    """MFD（Freeman/Holmgren 多方向流）の分配率を返す。

    各セルの流量を「下り勾配のすべての隣接セル」へ勾配の p 乗に比例して
    分配する。戻り値: shape (rows, cols, 8) の float32。第3軸は _NB8 の順。
    分配先が無い（周囲がすべて同高以上）セルは全成分 0（シンク）。

    p : 分配の集中度。Freeman(1991) は 1.1。大きいほど D8 に近づく。
    """
    rows, cols = dem.shape
    pad = np.pad(dem, 1, constant_values=np.nan)
    e0 = pad[1:-1, 1:-1]
    prop = np.zeros((rows, cols, 8), dtype=np.float64)
    for k, (dr, dc) in enumerate(_NB8):
        nb = pad[1 + dr: rows + 1 + dr, 1 + dc: cols + 1 + dc]
        dist = cell_size * (1.4142135623730951 if dr != 0 and dc != 0 else 1.0)
        s = (e0 - nb) / dist
        s = np.where(np.isnan(nb), 0.0, np.maximum(s, 0.0))
        prop[:, :, k] = s ** p
    total = prop.sum(axis=2)
    nz = total > 0
    prop[nz] /= total[nz][:, None]
    prop[np.isnan(e0)] = 0.0
    return prop.astype(np.float32)


def dinf_proportions(dem, cell_size):
    """D∞（Tarboton 1997）の分配率を返す。

    各セル周囲の8つの三角facetから最急流下方向（連続角）を求め、その角度を
    挟む隣接2セルへ角度比で分配する。戻り値: shape (rows, cols, 8) float32。
    第3軸は _NB8 の順。流下先が無いセルは全成分 0。
    """
    rows, cols = dem.shape
    pad = np.pad(dem, 1, constant_values=np.nan)
    e0 = pad[1:-1, 1:-1]
    diag = cell_size * 1.4142135623730951
    ang_f = np.pi / 4.0
    # facet = (cardinal 隣接オフセット, diagonal 隣接オフセット)
    FACETS = [
        ((0, 1), (-1, 1)),  ((-1, 0), (-1, 1)),  ((-1, 0), (-1, -1)), ((0, -1), (-1, -1)),
        ((0, -1), (1, -1)), ((1, 0), (1, -1)),   ((1, 0), (1, 1)),    ((0, 1), (1, 1)),
    ]
    idx_of = {off: i for i, off in enumerate(_NB8)}

    best_s = np.full((rows, cols), -np.inf)
    best_r = np.zeros((rows, cols))
    best_card = np.full((rows, cols), -1, dtype=np.int64)
    best_diag = np.full((rows, cols), -1, dtype=np.int64)

    for (cr, cc), (gr, gc) in FACETS:
        e1 = pad[1 + cr: rows + 1 + cr, 1 + cc: cols + 1 + cc]  # cardinal
        e2 = pad[1 + gr: rows + 1 + gr, 1 + gc: cols + 1 + gc]  # diagonal
        s1 = (e0 - e1) / cell_size
        s2 = (e1 - e2) / cell_size
        r = np.arctan2(s2, s1)
        s = np.hypot(s1, s2)
        # 角度を [0, π/4] に拘束（範囲外は端の純方向勾配に丸める）
        neg = r < 0
        r = np.where(neg, 0.0, r)
        s = np.where(neg, s1, s)
        big = r > ang_f
        r = np.where(big, ang_f, r)
        s = np.where(big, (e0 - e2) / diag, s)

        take = (~np.isnan(e1)) & (~np.isnan(e2)) & (s > 0) & (s > best_s)
        best_s = np.where(take, s, best_s)
        best_r = np.where(take, r, best_r)
        best_card = np.where(take, idx_of[(cr, cc)], best_card)
        best_diag = np.where(take, idx_of[(gr, gc)], best_diag)

    prop = np.zeros((rows, cols, 8), dtype=np.float64)
    has = best_card >= 0
    rr, ccc = np.where(has)
    frac_d = best_r[rr, ccc] / ang_f          # diagonal 側へ回す割合
    prop[rr, ccc, best_card[rr, ccc]] = 1.0 - frac_d
    prop[rr, ccc, best_diag[rr, ccc]] = frac_d
    prop[np.isnan(e0)] = 0.0
    return prop.astype(np.float32)


def multi_flow_accumulation(dem, prop, weight=None):
    """multi-flow 分配率 prop から上流集水を累積する（標高降順に処理）。

    prop   : mfd_proportions / dinf_proportions の戻り値 (rows,cols,8)。
    weight : None=各セル 1（集水セル数相当）。2D配列で重み付き累積。
             上流加重平均 = multi_flow_accumulation(weight) / multi_flow_accumulation()
    """
    rows, cols = dem.shape
    accum = (np.ones((rows, cols), dtype=np.float64)
             if weight is None
             else weight.astype(np.float64).copy())
    valid = ~np.isnan(dem)
    r_arr, c_arr = np.where(valid)
    order = np.argsort(-dem[r_arr, c_arr])  # 高い順
    for idx in order:
        r, c = int(r_arr[idx]), int(c_arr[idx])
        base = accum[r, c]
        for k, (dr, dc) in enumerate(_NB8):
            pk = prop[r, c, k]
            if pk > 0.0:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                    accum[nr, nc] += base * pk
    return accum


def compute_twi(accum, slope_deg, cell_size):
    """
    TWI = ln(A / tan(β))
    A: 上流集水面積[m²], β: 傾斜[rad]
    """
    area = accum * cell_size * cell_size
    slope_rad = np.radians(np.maximum(slope_deg, 0.1))  # ゼロ除算防止
    twi = np.log(area / np.tan(slope_rad))
    twi[np.isnan(slope_deg)] = np.nan
    return twi


def stability_fs(slope_deg, phi_deg=30.0, c_kpa=3.0, z_m=0.5,
                 m=0.5, gamma_s=18.0, gamma_w=9.81):
    """
    無限斜面安定解析
    FS = (c' + (γs - m*γw)*z*cos²θ*tanφ') / (γs*z*sinθ*cosθ)

    slope_deg : 傾斜[度]
    phi_deg   : 内部摩擦角φ'[度]  default=30
    c_kpa     : 粘着力c'[kPa]     default=3
    z_m       : 土壌深度[m]        default=0.5
    m         : 飽和率(0-1)        default=0.5
    gamma_s   : 土壌単位重量[kN/m³] default=18.0
    gamma_w   : 水単位重量[kN/m³]   default=9.81
    戻り値     : FS ラスタ（FS<1.0=不安定, <1.5=要注意）
    """
    theta = np.radians(slope_deg)
    phi = np.radians(phi_deg)
    cos2 = np.cos(theta) ** 2
    sincos = np.sin(theta) * np.cos(theta)

    resistance = c_kpa + (gamma_s - m * gamma_w) * z_m * cos2 * np.tan(phi)
    driving = gamma_s * z_m * sincos

    fs = np.where(driving > 1e-6, resistance / driving, np.inf)
    fs = np.where(np.isnan(slope_deg), np.nan, fs)
    return fs


def rational_flow(accum, cell_size, rainfall_mmh=50.0, runoff_coef=0.8):
    """
    合理式: Q = (1/360) * C * r * A  [m³/s]
    accum        : 上流集水セル数
    cell_size    : セルサイズ[m]
    rainfall_mmh : 降雨強度[mm/h]
    runoff_coef  : 流出係数C (0-1)
    """
    area_ha = (accum * cell_size * cell_size) / 10000.0
    Q = (1.0 / 360.0) * runoff_coef * rainfall_mmh * area_ha
    return Q


def rational_flow_3metrics(accum, cell_size,
                            i_peak_mmh=50.0, runoff_coef=0.8,
                            total_mm=100.0, duration_h=6.0):
    """
    合理式ベース流量の3指標

    Q_peak  [m³/s] = (1/360) * C * i_peak * A_ha
    Q_mean  [m³/s] = (1/360) * C * (total_mm / duration_h) * A_ha
    V_total [m³]   = Q_mean * duration_h * 3600

    i_peak_mmh  : 最大降雨強度 [mm/h]
    runoff_coef : 流出係数 C (0–1)
    total_mm    : 期間総降水量 [mm]
    duration_h  : 降雨継続時間 [h]

    戻り値: (Q_peak, Q_mean, V_total) の tuple（いずれも numpy 配列）
    """
    area_ha = (accum * cell_size * cell_size) / 10000.0
    Q_peak = (1.0 / 360.0) * runoff_coef * i_peak_mmh * area_ha
    i_mean_mmh = total_mm / max(duration_h, 0.1)
    Q_mean = (1.0 / 360.0) * runoff_coef * i_mean_mmh * area_ha
    V_total = Q_mean * duration_h * 3600.0
    return Q_peak, Q_mean, V_total


def compute_travel_time(dem, flow_dir, cell_size,
                        velocity_coef=0.3, slope_exp=0.5):
    """
    各セルの局所到達時間 local_tt [hours] を計算する。

    velocity = v_coef × max(tan(slope), 0.001)^slope_exp  [m/s]
    local_tt = 移動距離 / velocity / 3600               [h]
      移動距離: 斜め方向は cell_size×√2、直交方向は cell_size

    velocity_coef : 速度係数 [m/s]（林地標準≈0.3）
    slope_exp     : 傾斜の指数（0.5 が標準 Manning 則）
    """
    pad = np.pad(dem, 1, mode="edge")
    dzdx = (pad[1:-1, 2:] - pad[1:-1, :-2]) / (2.0 * cell_size)
    dzdy = (pad[2:, 1:-1] - pad[:-2, 1:-1]) / (2.0 * cell_size)
    tan_slope = np.maximum(np.sqrt(dzdx ** 2 + dzdy ** 2), 0.001)

    velocity = velocity_coef * tan_slope ** slope_exp  # m/s

    # 対角方向は √2 倍の距離
    DIAG = {2, 8, 32, 128}
    dist = np.where(np.isin(flow_dir, list(DIAG)),
                    cell_size * 1.4142, cell_size).astype(np.float64)

    local_tt = dist / velocity / 3600.0  # hours
    local_tt[np.isnan(dem)] = np.nan
    return local_tt


def compute_tc(dem, flow_dir, local_tt):
    """
    各セルの流達時間 Tc [hours] を計算する。

    Tc[C] = 最も遠い上流セルから C に到達するまでの最大移動時間。

    アルゴリズム:
      1. 出口から headwater への累積到達時間 cum_tt を計算
         （標高昇順 = 出口優先で処理し、 cum_tt[C] = local_tt[C] + cum_tt[下流]）
      2. 最大累積時間 max_cum_tt を headwater から出口方向に伝播
         （標高降順で max_cum_tt[下流] = max(max_cum_tt[下流], max_cum_tt[上流])）
      3. Tc[C] = max_cum_tt[C] - cum_tt[C]
    """
    rows, cols = dem.shape
    DIR_OFFSET = {
        1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
        16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
    }
    valid = ~np.isnan(dem)
    r_arr, c_arr = np.where(valid)
    elev = dem[r_arr, c_arr]

    # --- Step 1: cum_tt_to_outlet（標高昇順 = 出口→ headwater） ---
    cum_tt = np.where(valid, local_tt, 0.0)
    for idx in np.argsort(elev):          # 低い順
        r, c = int(r_arr[idx]), int(c_arr[idx])
        d = int(flow_dir[r, c])
        if d in DIR_OFFSET:
            dr, dc = DIR_OFFSET[d]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                cum_tt[r, c] = local_tt[r, c] + cum_tt[nr, nc]

    # --- Step 2: max_cum_tt（標高降順 = headwater→出口へ伝播） ---
    max_cum_tt = cum_tt.copy()
    for idx in np.argsort(-elev):         # 高い順
        r, c = int(r_arr[idx]), int(c_arr[idx])
        d = int(flow_dir[r, c])
        if d in DIR_OFFSET:
            dr, dc = DIR_OFFSET[d]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                if max_cum_tt[r, c] > max_cum_tt[nr, nc]:
                    max_cum_tt[nr, nc] = max_cum_tt[r, c]

    # --- Step 3: Tc = max upstream cum_tt − own cum_tt ---
    tc = max_cum_tt - cum_tt
    tc[~valid] = np.nan
    return tc


def flow_routing_3metrics(accum, tc, cell_size, duration_h,
                           i_peak_mmh=50.0, runoff_coef=0.8,
                           total_mm=100.0):
    """
    [DEPRECATED] time_area_flow_metrics() に置換。回帰比較用に保持。

    到達時間（Tc）を用いた修正合理式による流量3指標。

    有効集水面積 A_eff = A_total × min(1, duration_h / Tc)
      ・Tc < duration_h の小流域: A_eff ≈ A_total（全域から寄与）
      ・Tc > duration_h の大流域: A_eff < A_total（遠方は未到達）

    Q_routed_peak [m³/s] = (1/360) × C × i_peak × A_eff_ha
    Q_routed_mean [m³/s] = (1/360) × C × i_mean × A_eff_ha
    V_routed_total [m³]  = Q_routed_mean × duration_h × 3600

    計算根拠: 修正合理式（modified rational method）
      参考: 土地改良事業設計指針「排水」ほか

    戻り値: (Q_peak, Q_mean, V_total, tc) の tuple
    """
    eps = 1e-6
    tc_safe = np.where(np.isnan(tc), eps, np.maximum(tc, eps))

    # 有効集水面積比（短時間降雨では遠方集水域の寄与を抑制）
    eff_ratio = np.minimum(1.0, duration_h / tc_safe)

    area_ha = (accum * cell_size * cell_size) / 10000.0
    area_eff_ha = area_ha * eff_ratio

    Q_peak = (1.0 / 360.0) * runoff_coef * i_peak_mmh * area_eff_ha
    i_mean = total_mm / max(duration_h, 0.1)
    Q_mean = (1.0 / 360.0) * runoff_coef * i_mean * area_eff_ha
    V_total = Q_mean * duration_h * 3600.0
    return Q_peak, Q_mean, V_total


# ── 時間‐面積法（Clark 単位図法） ─────────────────────────────────────────
#   参考: Clark, C.O. (1945) Storage and the unit hydrograph. Trans. ASCE 110.
#         Chow, Maidment & Mays (1988) Applied Hydrology, §7–8.
#         USDA NRCS National Engineering Handbook, Part 630 (Hydrology).
#   適用範囲: 線形・時間不変系、地表流主体の小〜中流域（目安 〜数 km²）、
#             単一設計ハイエトグラフ、D8 単一流向。指針であって厳密解ではない。

def cum_travel_time_to_outlet(dem, flow_dir, local_tt):
    """各セルから下流の領域出口までの累積移動時間 [h]。

    compute_tc の Step 1 と同一。時間‐面積法の等到達時間（isochrone）基準
    に使う。点 p の上流セル x の p への到達時間は
    cum_tt[x] - cum_tt[p]（両者の差＝x→p 区間）で得られる。
    """
    rows, cols = dem.shape
    DIR_OFFSET = {
        1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
        16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
    }
    valid = ~np.isnan(dem)
    r_arr, c_arr = np.where(valid)
    elev = dem[r_arr, c_arr]
    cum_tt = np.where(valid, local_tt, 0.0).astype(np.float64)
    for idx in np.argsort(elev):          # 低い順（出口 → headwater）
        r, c = int(r_arr[idx]), int(c_arr[idx])
        d = int(flow_dir[r, c])
        if d in DIR_OFFSET:
            dr, dc = DIR_OFFSET[d]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                cum_tt[r, c] = local_tt[r, c] + cum_tt[nr, nc]
    cum_tt[~valid] = np.nan
    return cum_tt


def triangular_hyetograph(total_mm, duration_h, i_peak_mmh, peak_frac=0.4,
                          n_sub=60):
    """三角形の設計ハイエトグラフ。

    高さ i_peak・面積 total_mm の三角形を作る（底辺 = 2·total/i_peak）。
    底辺が duration_h を超える場合は底辺 = duration_h に詰め、実効ピークを
    2·total/duration に下げる（＝ i_peak が平均強度を下回る指定は無効）。
    山の位置は底辺の peak_frac（既定 0.4）。区間外は 0。

    戻り値: (edges, intensity) — intensity[k] は [edges[k], edges[k+1]) の
            平均強度 [mm/h]。edges は [0, duration_h] を n_sub 分割。
    """
    dur = max(float(duration_h), 1e-3)
    tot = max(float(total_mm), 0.0)
    i_mean = tot / dur
    ipk = max(float(i_peak_mmh), 1e-6)
    base = min(dur, 2.0 * tot / ipk) if ipk > 0 else dur
    base = max(base, 1e-3)
    ipk_eff = 2.0 * tot / base                      # 面積 = tot を保証
    if ipk_eff < i_mean:                            # 念のため（数値誤差）
        ipk_eff = 2.0 * i_mean
        base = min(dur, 2.0 * tot / max(ipk_eff, 1e-9))
    tp = peak_frac * base
    edges = np.linspace(0.0, dur, n_sub + 1)
    ctr = 0.5 * (edges[:-1] + edges[1:])
    up = ipk_eff * (ctr / max(tp, 1e-9))
    down = ipk_eff * (1.0 - (ctr - tp) / max(base - tp, 1e-9))
    inten = np.where(ctr <= tp, up, down)
    inten = np.where(ctr <= base, inten, 0.0)
    inten = np.clip(inten, 0.0, None)
    return edges, inten.astype(np.float64)


def _downstream_order(dem):
    """標高降順のセル座標列（累積処理用）を1回だけ作る。"""
    valid = ~np.isnan(dem)
    r_arr, c_arr = np.where(valid)
    order = np.argsort(-dem[r_arr, c_arr])
    return r_arr[order], c_arr[order]


def _accumulate_downstream(rc_r, rc_c, flow_dir, weight, shape):
    """事前ソート済み座標列で D8 下流累積（weight は 2D）。"""
    rows, cols = shape
    accum = weight.astype(np.float64).copy()
    DIR_OFFSET = {
        1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
        16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
    }
    for k in range(rc_r.size):
        r, c = int(rc_r[k]), int(rc_c[k])
        d = int(flow_dir[r, c])
        if d in DIR_OFFSET:
            dr, dc = DIR_OFFSET[d]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and not np.isnan(accum[nr, nc]):
                accum[nr, nc] += accum[r, c]
    return accum


def _accumulate_downstream_multi(rc_r, rc_c, flow_dir, wstack, valid):
    """weight スタック (rows,cols,M) を D8 下流累積（1回のセルループで M 本分）。

    valid : 2D bool。無効セルへは伝播させない。
    """
    rows, cols, _ = wstack.shape
    accum = wstack.astype(np.float64).copy()
    DIR_OFFSET = {
        1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
        16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1),
    }
    for k in range(rc_r.size):
        r, c = int(rc_r[k]), int(rc_c[k])
        d = int(flow_dir[r, c])
        if d in DIR_OFFSET:
            dr, dc = DIR_OFFSET[d]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and valid[nr, nc]:
                accum[nr, nc, :] += accum[r, c, :]
    return accum


def time_area_flow_metrics(dem, flow_dir, local_tt, tc, cell_size,
                           c_grid=0.8, duration_h=6.0,
                           i_peak_mmh=50.0, total_mm=100.0,
                           clark_k=0.75, n_time=24, progress_cb=None):
    """時間‐面積法（Clark 単位図法）による負荷3指標。

    各セルを流出点とみなし、上流域の等到達時間分布に設計ハイエトグラフを
    畳み込んでハイドログラフ Q(p,t) を構成する。Clark 線形貯留
    R = clark_k·Tc で平滑化してから:
      Peak 負荷 = max_t Q(p,t)                         [m³/s]
      Mean 負荷 = V(p) / ((duration_h + Tc(p))·3600)   [m³/s]
      時間負荷  = Tc（入力をそのまま返す）             [h]
    V(p) は総流出体積 [m³]（C·P_total·A、時間分布に不感）で内部中間値。

    c_grid : スカラー流出係数 or 2D グリッド（DSM 由来）。
    progress_cb : callable(frac) 省略可。0..1 の進捗を返す。

    戻り値: (q_peak, q_mean, tc)  float32 2D / tc は入力そのまま。
    """
    valid = ~np.isnan(dem)
    cum_tt = cum_travel_time_to_outlet(dem, flow_dir, local_tt)   # [h]
    # 到達時間チェーンで NaN になったセル（nodata 隣接など）は解析対象外。
    amask = (valid & np.isfinite(cum_tt) & np.isfinite(tc)
             & np.isfinite(local_tt))
    cum0 = np.where(amask, cum_tt, 0.0)
    tc_pos = np.where(amask, np.maximum(tc, 0.0), 0.0)

    edges, inten = triangular_hyetograph(total_mm, duration_h, i_peak_mmh)
    n_bins = inten.size
    storm_end = float(edges[-1])

    def intensity_at(hours):
        idx = np.searchsorted(edges, hours, side="right") - 1
        inb = (idx >= 0) & (idx < n_bins)
        return np.where(inb, inten[np.clip(idx, 0, n_bins - 1)], 0.0)

    # 評価時刻 u（cum_tt 基準）。点 p のハイドログラフは
    # u ∈ [cum_tt[p], cum_tt[p] + Tc[p] + storm_end] で非ゼロ。
    if amask.any():
        u_lo = float(cum0[amask].min())
        u_hi = float((cum0 + tc_pos)[amask].max()) + storm_end
        tc_med = float(np.median(tc_pos[amask]))
    else:
        u_lo, u_hi, tc_med = 0.0, storm_end, storm_end
    if u_hi <= u_lo:
        u_hi = u_lo + max(storm_end, 1.0)
    span = u_hi - u_lo
    # 分解能: 波形幅(storm_end)と中央値 Tc の小さい方を ~8 分割、n_time〜2*n_time
    du_target = max(min(storm_end, tc_med if tc_med > 0 else storm_end) / 8.0,
                    span / (2 * n_time), 1e-3)
    n_eval = int(np.clip(round(span / du_target) + 1, n_time, 2 * n_time))
    # 重みスタックのメモリ上限（〜240MB / float64）でステップ数を抑える
    n_cap = max(10, int(3.0e7 / max(dem.size, 1)))
    n_eval = int(min(n_eval, n_cap))
    u = np.linspace(u_lo, u_hi, n_eval)
    du = span / max(n_eval - 1, 1)                               # [h]

    cell_area_m2 = cell_size * cell_size
    mmh_to_ms = 1.0 / (1000.0 * 3600.0)                          # mm/h → m/s

    rc_r, rc_c = _downstream_order(dem)

    # 各 u の局所流出重み w(x, u_m) = C·面積·i(u_m − cum_tt[x])  [m³/s]
    wstack = np.zeros(dem.shape + (n_eval,), dtype=np.float64)
    cfac = c_grid * cell_area_m2 * mmh_to_ms
    for m in range(n_eval):
        i_u = intensity_at(u[m] - cum0)                          # [mm/h]
        wstack[:, :, m] = np.where(amask, cfac * i_u, 0.0)
        if progress_cb is not None:
            progress_cb(0.5 * (m + 1) / n_eval)
    g = _accumulate_downstream_multi(rc_r, rc_c, flow_dir, wstack, amask)
    del wstack

    # Clark 線形貯留（u 軸の離散リザーバ）: R = k·Tc
    R = clark_k * tc_pos                                          # [h]
    alpha = du / (R + du)                                        # per-cell 0..1
    S = np.zeros(dem.shape, dtype=np.float64)
    q_peak = np.zeros(dem.shape, dtype=np.float64)
    for m in range(n_eval):
        S = S + alpha * (np.where(amask, g[:, :, m], 0.0) - S)
        np.maximum(q_peak, S, out=q_peak)
        if progress_cb is not None:
            progress_cb(0.5 + 0.5 * (m + 1) / n_eval)
    del g

    # 総体積 V [m³] = C · (total_mm/1000) · セル面積 を下流累積
    v_w = np.where(amask, c_grid * (total_mm / 1000.0) * cell_area_m2, np.nan)
    V = _accumulate_downstream(rc_r, rc_c, flow_dir, v_w, dem.shape)
    V = np.where(amask, V, 0.0)

    t_base_s = (duration_h + tc_pos) * 3600.0
    q_mean = np.where(t_base_s > 0, V / t_base_s, 0.0)

    # ハイドログラフの最大は平均を必ず上回る（物理的下限）。
    # 離散評価の取りこぼしを埋めるため Mean で床を張る。
    q_peak = np.maximum(q_peak, q_mean)

    q_peak[~amask] = np.nan
    q_mean[~amask] = np.nan
    return q_peak.astype(np.float32), q_mean.astype(np.float32), tc


def cs_to_flow_coefficients(cs_grid,
                            c_forest=0.15, c_bare=0.55,
                            v_forest=0.30, v_bare=0.60,
                            cs_forest=10.0, cs_bare=3.0):
    """樹冠高さ（CS = DSM − DEM）から流出係数・流速係数の空間グリッドを生成。

    cs_bare  以下: 伐採地・裸地 → C=c_bare,  v=v_bare
    cs_forest以上: 密林         → C=c_forest, v=v_forest
    中間: 線形補間

    戻り値: (c_grid, v_grid) — いずれも float32 の 2D 配列
    """
    cs = np.where(np.isnan(cs_grid), cs_forest, cs_grid)  # NaN は密林扱い
    cs = np.clip(cs, cs_bare, cs_forest)
    t = (cs - cs_bare) / max(cs_forest - cs_bare, 1e-6)   # 0=裸地, 1=密林
    c_grid = (c_bare  + t * (c_forest - c_bare)).astype(np.float32)
    v_grid = (v_bare  + t * (v_forest - v_bare)).astype(np.float32)
    return c_grid, v_grid


