# -*- coding: utf-8 -*-
"""
ForestryPathFinder — DEMLoader の numpy 配列を使った A* 経路探索。
optipath_designer の RouteOptimizerLogic をベースに、林業向けパラメータに対応。
"""
import heapq
import math
import numpy as np


class ForestryPathFinder:
    """
    DEMLoader の numpy 配列でグリッド A* 探索を行うクラス。

    Parameters
    ----------
    dem_loader : DEMLoader
        open_metadata() 済みの DEMLoader インスタンス。
        find_path() 呼び出し時に必要なら read_data() を自動実行する。
    """

    # 16方向移動ベクトル (dc, dr, dist倍率)
    # 8近傍 + 桂馬飛び8方向 → より滑らかな経路
    _BASE_MOVES = [
        ( 1,  0, 1.000), ( 1,  1, 1.414), ( 0,  1, 1.000), (-1,  1, 1.414),
        (-1,  0, 1.000), (-1, -1, 1.414), ( 0, -1, 1.000), ( 1, -1, 1.414),
        ( 2,  1, 2.236), ( 1,  2, 2.236), (-1,  2, 2.236), (-2,  1, 2.236),
        (-2, -1, 2.236), (-1, -2, 2.236), ( 1, -2, 2.236), ( 2, -1, 2.236),
    ]

    def __init__(self, dem_loader):
        self._loader = dem_loader
        self._data = None
        self.rows = 0
        self.cols = 0
        self._ps = abs(dem_loader.gt[1])   # ピクセルサイズ [m]

    # ── データアクセス ──────────────────────────────────────────────────────

    def _ensure_data(self):
        if self._data is None:
            self._loader.read_data()
            self._data = self._loader.data
            self.rows, self.cols = self._data.shape

    def get_elev(self, col, row):
        """ピクセル座標の標高を返す。範囲外・NoData は None。"""
        if not (0 <= col < self.cols and 0 <= row < self.rows):
            return None
        v = self._data[row, col]
        return None if np.isnan(v) else float(v)

    def coord_to_pixel(self, x, y):
        """DEM CRS 地理座標 → (col, row)。"""
        gt = self._loader.gt
        col = int((x - gt[0]) / gt[1])
        row = int((y - gt[3]) / gt[5])
        return col, row

    def pixel_to_coord(self, col, row):
        """(col, row) → セル中心の DEM CRS 地理座標 (x, y)。"""
        gt = self._loader.gt
        x = gt[0] + (col + 0.5) * gt[1]
        y = gt[3] + (row + 0.5) * gt[5]
        return x, y

    # ── 公開 API ────────────────────────────────────────────────────────────

    def find_path(self, waypoints_dem_crs,
                  max_slope_deg,
                  min_radius_m=None,
                  max_slope_dist=None,
                  allowed_mask=None):
        """
        A* で waypoints を順番に経路探索し、DEM CRS の (x, y, elev) リストを返す。

        Parameters
        ----------
        waypoints_dem_crs : list of (x, y)
            DEM CRS 座標の通過点リスト。先頭=開始、末尾=終了。
        max_slope_deg : float
            最大許容勾配 [°]
        min_radius_m : float | None
            最小旋回半径 [m]。None or 0 で制約なし。
        max_slope_dist : float | None
            連続登坂距離の上限 [m]。None or 0 で制約なし。

        Returns
        -------
        list of (x, y, elev) | None
            DEM CRS 座標＋標高のリスト。探索失敗時は None。
        """
        self._ensure_data()
        if len(waypoints_dem_crs) < 2:
            return None

        ps = self._ps
        TARGET_STEP_M = max(ps, 3.0)
        step_px = max(1, int(TARGET_STEP_M / ps))
        moves = [(dc * step_px, dr * step_px, cm) for dc, dr, cm in self._BASE_MOVES]

        full_pixels = []
        for i in range(len(waypoints_dem_crs) - 1):
            seg = self._search_segment(
                waypoints_dem_crs[i],
                waypoints_dem_crs[i + 1],
                max_slope_deg, min_radius_m, max_slope_dist,
                moves, step_px,
                allowed_mask=allowed_mask,
            )
            if seg is None or isinstance(seg, str):
                return None
            full_pixels.extend(seg if i == 0 else seg[1:])

        # ピクセル座標 → (x, y, elev)
        result = []
        for col, row in full_pixels:
            x, y = self.pixel_to_coord(col, row)
            elev = self.get_elev(col, row)
            result.append((x, y, elev if elev is not None else 0.0))
        return result

    # ── 内部メソッド ────────────────────────────────────────────────────────

    def _search_segment(self, start_xy, end_xy,
                        max_slope_deg, min_radius_m, max_slope_dist,
                        moves, step_px, allowed_mask=None):
        """1区間分の A* 探索。(col, row) のリストを返す。"""
        ps = self._ps
        sc, sr = self.coord_to_pixel(*start_xy)
        ec, er = self.coord_to_pixel(*end_xy)

        if self.get_elev(sc, sr) is None:
            return "Start_NoData"
        if self.get_elev(ec, er) is None:
            return "End_NoData"

        # 状態: (col, row, prev_dc, prev_dr, cum_climb_int)
        start_state = (sc, sr, 0, 0, 0)
        open_set = []
        heapq.heappush(open_set, (0.0, start_state))
        came_from  = {}
        g_score    = {start_state: 0.0}

        H_WEIGHT  = 1.3
        MAX_ITER  = 1_000_000
        MIN_ANGLE = 0.5
        radius_on    = min_radius_m  is not None and min_radius_m  > 0
        slopedist_on = max_slope_dist is not None and max_slope_dist > 0

        for _ in range(MAX_ITER):
            if not open_set:
                break
            _, cur = heapq.heappop(open_set)
            cc, cr, pdc, pdr, c_climb = cur

            # ゴール判定
            if abs(cc - ec) <= step_px and abs(cr - er) <= step_px:
                return self._reconstruct(came_from, cur)

            z_cur = self.get_elev(cc, cr)

            for dc, dr, _ in moves:
                nc, nr = cc + dc, cr + dr
                z_next = self.get_elev(nc, nr)
                if z_next is None:
                    continue
                if allowed_mask is not None and not allowed_mask[nr, nc]:
                    continue

                dist_m = math.sqrt((dc * ps) ** 2 + (dr * ps) ** 2)
                diff   = z_next - (z_cur if z_cur is not None else z_next)
                slope_deg = math.degrees(math.atan(abs(diff) / dist_m)) if dist_m > 0 else 0.0

                if slope_deg > max_slope_deg:
                    continue

                # 旋回半径チェック
                turn_penalty = 0.0
                if radius_on and (pdc != 0 or pdr != 0):
                    dot    = pdc * dc + pdr * dr
                    mag_p  = math.sqrt(pdc ** 2 + pdr ** 2)
                    mag_c  = math.sqrt(dc  ** 2 + dr  ** 2)
                    cos_t  = max(-1.0, min(1.0, dot / (mag_p * mag_c)))
                    theta  = math.acos(cos_t)
                    max_turn = max(dist_m / min_radius_m, MIN_ANGLE)
                    if theta > max_turn:
                        continue
                    turn_penalty = theta * dist_m * 5.0

                # 連続登坂距離チェック
                next_climb = 0
                if slopedist_on:
                    if diff > 0 and slope_deg > 2.0:
                        next_climb = c_climb + int(dist_m)
                        if next_climb > max_slope_dist:
                            continue
                    else:
                        next_climb = 0

                # コスト計算（勾配が急なほど二乗ペナルティ）
                step_cost = dist_m * (1.0 + (slope_deg / 15.0) ** 2) + turn_penalty
                new_g     = g_score[cur] + step_cost

                next_state = (nc, nr, dc, dr, next_climb)
                if next_state not in g_score or new_g < g_score[next_state]:
                    g_score[next_state]    = new_g
                    came_from[next_state]  = cur
                    h = math.sqrt((nc - ec) ** 2 + (nr - er) ** 2) * ps * H_WEIGHT
                    heapq.heappush(open_set, (new_g + h, next_state))

        return None  # 経路なし / タイムアウト

    def _reconstruct(self, came_from, state):
        path = []
        while state in came_from:
            path.append((state[0], state[1]))
            state = came_from[state]
        path.append((state[0], state[1]))
        path.reverse()
        return path


# ── 例外ロジック 1: 自己交差を本線＋支線に分離 ─────────────────────────────

def split_at_crossings(path_xye):
    """
    例外ロジック 1 — A* 経路の自己交差を検出し、本線と行き止まり支線に分離する。

    交差が見つかった場合:
        本線: ... → P[i] → 交点2# → P[j+1] → ...
        支線: 2# → P[i+1] → ... → P[j]  （行き止まり）

    Parameters
    ----------
    path_xye : list of (x, y, elev)

    Returns
    -------
    main    : list of (x, y, elev)   本線
    branches: list of list of (x, y, elev)   支線リスト
    """
    main = list(path_xye)
    branches = []

    # 交差がなくなるまで繰り返す（多重交差に対応）
    changed = True
    while changed:
        changed = False
        n = len(main)
        for i in range(n - 3):
            for j in range(i + 2, n - 1):
                ix, iy = _seg_intersect(
                    main[i][0], main[i][1],
                    main[i + 1][0], main[i + 1][1],
                    main[j][0], main[j][1],
                    main[j + 1][0], main[j + 1][1],
                )
                if ix is None:
                    continue

                # 交点の標高: 前後セグメントから線形補間して平均
                ez = _interp_elev(main[i], main[i + 1], ix, iy,
                                  main[j], main[j + 1])
                cross = (ix, iy, ez)

                # 支線: 交点 → P[i+1] → ... → P[j]  (行き止まり)
                branch = [cross] + main[i + 1: j + 1]
                branches.append(branch)

                # 本線: ..P[i] → 交点 → P[j+1]..
                main = main[: i + 1] + [cross] + main[j + 1:]
                changed = True
                break
            if changed:
                break

    return main, branches


# ── 補助関数 ─────────────────────────────────────────────────────────────────

def _seg_intersect(x1, y1, x2, y2, x3, y3, x4, y4):
    """2セグメント (x1y1–x2y2) と (x3y3–x4y4) の交点を返す。なければ (None, None)。"""
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-12:
        return None, None   # 平行
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return x1 + t * (x2 - x1), y1 + t * (y2 - y1)
    return None, None


def _interp_elev(pa, pb, x, y, pc=None, pd=None):
    """交点 (x, y) における標高を前後セグメントの線形補間で求める。"""
    def _lerp(p1, p2, qx, qy):
        dx, dy = p2[0] - p1[0], p2[1] - p1[1]
        seg = math.sqrt(dx * dx + dy * dy)
        if seg < 1e-12:
            return p1[2]
        t = ((qx - p1[0]) * dx + (qy - p1[1]) * dy) / (seg * seg)
        t = max(0.0, min(1.0, t))
        return p1[2] + t * (p2[2] - p1[2])

    e1 = _lerp(pa, pb, x, y)
    if pc is None:
        return e1
    e2 = _lerp(pc, pd, x, y)
    return (e1 + e2) / 2.0
