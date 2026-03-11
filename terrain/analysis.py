"""
地形解析アルゴリズム群
  compute_slope_deg  : Horn法で傾斜角[度]を計算
  d8_flow_direction  : D8流向コードを返す
  flow_accumulation  : 上流集水セル数を累積
  compute_twi        : TWI = ln(A / tan(β))
  stability_fs       : 無限斜面安定解析（FS）
  rational_flow      : 合理式による流量推測 [m³/s]
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


def stability_fs(slope_deg, phi_deg=35.0, c_kpa=0.0, z_m=1.0,
                 m=0.5, gamma_s=18.0, gamma_w=9.81):
    """
    無限斜面安定解析
    FS = (c' + (γs - m*γw)*z*cos²θ*tanφ') / (γs*z*sinθ*cosθ)

    slope_deg : 傾斜[度]
    phi_deg   : 内部摩擦角φ'[度]  default=35
    c_kpa     : 粘着力c'[kPa]     default=0
    z_m       : 土壌深度[m]        default=1.0
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


