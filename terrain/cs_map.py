"""CS MAP export raster synthesis.

This module implements an original CS-style terrain visualization for FOL.
It uses the plugin's DEM-derived slope and curvature arrays, and does not
reuse CSMapMaker code, layer files, or bundled assets.
"""
import numpy as np

from .analysis import compute_curvature, compute_slope_deg


CUSTOM_CS_COLOR_PRESET = {
    "name": "custom_cs",
    "description": "FOL custom CS color balance, tuned against same-area DEM/reference comparison.",
    "smooth_sigma_px": 2.4,
    "slope_percentiles": (3.0, 97.0),
    "elevation_percentiles": (2.0, 98.0),
    "curvature_percentile": 97.5,
    "ridge_gamma": 0.44,
    "valley_gamma": 0.60,
    "elevation_gray": (28.0, 122.0),
    "elevation_gamma": 1.0,
    "slope_brown_light": (228, 216, 190),
    "slope_brown_dark": (112, 62, 30),
    "slope_brown_alpha": 0.38,
    "warm_neutral": (166, 138, 118),
    "warm_neutral_alpha": 0.06,
    "curvature_neutral": (214, 194, 166),
    "valley_blue": (28, 58, 142),
    "valley_alpha": (0.38, 0.76),
    "valley_core_start": 0.55,
    "valley_core_light": (98, 108, 130),
    "valley_core_dark": (8, 34, 150),
    "valley_core_alpha": 0.24,
    "ridge_red": (150, 20, 12),
    "ridge_alpha": (0.64, 0.54),
    "slope_black": (18, 18, 18),
    "slope_black_alpha": 0.45,
    "contrast": (104.0, 1.17, 102.0),
}


SHIZUOKA_REFERENCE_TUNED_PRESET = {
    **CUSTOM_CS_COLOR_PRESET,
    "name": "shizuoka_reference_tuned",
    "description": "Reference-tuned CS color balance with restored valley blue and softer neutral tones.",
    "smooth_sigma_px": 1.0,
    "ridge_gamma": 0.28,
    "valley_gamma": 0.28,
    "elevation_gray": (26.0, 114.0),
    "elevation_gamma": 1.78,
    "slope_brown_alpha": 0.36,
    "warm_neutral_alpha": 0.045,
    "valley_blue": (8, 42, 132),
    "valley_alpha": (0.90, 0.34),
    "valley_core_start": 0.28,
    "valley_core_light": (62, 78, 122),
    "valley_core_dark": (1, 14, 118),
    "valley_core_alpha": 0.40,
    "ridge_red": (132, 42, 24),
    "ridge_alpha": (1.12, 0.28),
    "ridge_slope_cutoff": 0.135,
    "ridge_slope_gate_gamma": 0.24,
    "ridge_valley_fade": 0.12,
    "slope_black_alpha": 0.40,
    "contrast": (104.0, 1.14, 108.0),
    "plain_desaturate_cutoff": 0.22,
    "plain_desaturate_strength": 0.28,
    "plain_desaturate_gamma": 1.25,
    "color_mask_sigma_px": 0.9,
    "valley_spread_sigma_px": 3.25,
    "valley_ridge_fade": 0.08,
    "valley_depth_sigma_px": 8.0,
    "valley_depth_spread_sigma_px": 7.0,
    "valley_depth_percentiles": (34.0, 98.5),
    "valley_depth_gamma": 0.44,
    "valley_depth_floor": 0.66,
    "valley_depth_low_range": (0.06, 0.24),
    "valley_depth_mid_range": (0.24, 0.56),
    "valley_depth_high_range": (0.56, 0.86),
    "valley_depth_low_boost": 0.09,
    "valley_depth_mid_boost": 0.23,
    "valley_depth_peak_boost": 0.52,
    "valley_depth_support": 0.34,
    "valley_color_smooth_sigma_px": 1.10,
    "valley_convex_block": 0.92,
    "valley_plain_depth_range": (0.18, 0.42),
    "valley_plain_depth_support": 0.88,
    "valley_slope_gate_gamma": 0.28,
    "valley_slope_cutoff": 0.075,
    "final_saturation": 1.68,
    "final_luma_contrast": 1.32,
    "final_luma_center": 118.0,
    "final_luma_offset": 5.0,
    "final_density": 0.96,
}


ACTIVE_CS_COLOR_PRESET = SHIZUOKA_REFERENCE_TUNED_PRESET


def _robust_unit(data, p_low=2.0, p_high=98.0):
    valid = np.isfinite(data)
    if not np.any(valid):
        return np.zeros(data.shape, dtype=np.float32)
    lo, hi = np.nanpercentile(data[valid], [p_low, p_high])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.zeros(data.shape, dtype=np.float32)
    unit = (data - lo) / (hi - lo)
    return np.clip(unit, 0.0, 1.0).astype(np.float32)


def _smooth_nan(data, sigma_px=2.4):
    valid = np.isfinite(data)
    if not np.any(valid):
        return data
    if sigma_px <= 0:
        return data
    try:
        from scipy.ndimage import gaussian_filter
    except Exception:
        return data

    filled = np.where(valid, data, 0.0).astype(np.float64)
    weight = valid.astype(np.float64)
    smoothed = gaussian_filter(filled, sigma_px, mode="nearest")
    weights = gaussian_filter(weight, sigma_px, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        out = smoothed / weights
    out[weights <= 0.0] = np.nan
    return out


def _lerp_color(a, b, t):
    t3 = np.clip(t, 0.0, 1.0)[..., None]
    ca = np.asarray(a, dtype=np.float32)
    cb = np.asarray(b, dtype=np.float32)
    return ca + (cb - ca) * t3


def _overlay(base, top, alpha):
    return base * (1.0 - alpha) + top * alpha


def _soft_alpha(t, strength, gamma=0.7):
    t = np.clip(t, 0.0, 1.0)
    return strength * np.power(t, gamma)[..., None]


def _smooth_range(data, edge0, edge1):
    if edge1 <= edge0:
        return np.where(data >= edge1, 1.0, 0.0).astype(np.float32)
    t = np.clip((data - edge0) / (edge1 - edge0), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def compute_cs_map(dem, cell_size):
    """
    Build a CS-style RGBA terrain visualization from a DEM.

    The image follows the public CS topographic map recipe: smoothed elevation,
    curvature, and slope are rendered as several semi-transparent layers.
    Returns uint8 data with shape (rows, cols, 4).
    """
    preset = ACTIVE_CS_COLOR_PRESET
    smooth_dem = _smooth_nan(dem, preset["smooth_sigma_px"])
    slope = compute_slope_deg(smooth_dem, cell_size)
    curv = compute_curvature(smooth_dem, cell_size)
    valid = np.isfinite(dem) & np.isfinite(slope) & np.isfinite(curv)

    slope_u = _robust_unit(slope, *preset["slope_percentiles"])
    elev_u = _robust_unit(smooth_dem, *preset["elevation_percentiles"])
    elev_u = np.power(elev_u, preset["elevation_gamma"])

    abs_curv = np.abs(curv)
    curv_valid = np.isfinite(abs_curv)
    if np.any(curv_valid):
        cmax = np.nanpercentile(abs_curv[curv_valid], preset["curvature_percentile"])
    else:
        cmax = 0.0
    if not np.isfinite(cmax) or cmax <= 0:
        cmax = 1.0

    curv_s = np.clip(curv / cmax, -1.0, 1.0)
    ridge = np.clip(curv_s, 0.0, 1.0)
    valley = np.clip(-curv_s, 0.0, 1.0)
    ridge_mask = _smooth_nan(ridge, preset["color_mask_sigma_px"])
    valley_mask = _smooth_nan(
        valley,
        preset.get("valley_spread_sigma_px", preset["color_mask_sigma_px"]),
    )
    valley_color_mask = valley_mask * (
        1.0 - preset.get("valley_ridge_fade", 0.0) * np.clip(ridge_mask, 0.0, 1.0)
    )
    depth_surface = _smooth_nan(smooth_dem, preset["valley_depth_sigma_px"])
    local_depth = np.clip(depth_surface - smooth_dem, 0.0, None)
    depth_u = _robust_unit(local_depth, *preset["valley_depth_percentiles"])
    depth_u = np.power(depth_u, preset["valley_depth_gamma"])
    depth_spread = np.clip(_smooth_nan(depth_u, preset["valley_depth_spread_sigma_px"]), 0.0, 1.0)
    depth_area = np.clip(0.30 * depth_u + 0.70 * depth_spread, 0.0, 1.0)
    depth_low = _smooth_range(depth_area, *preset["valley_depth_low_range"])
    depth_mid = _smooth_range(depth_area, *preset["valley_depth_mid_range"])
    depth_high = _smooth_range(depth_area, *preset["valley_depth_high_range"])
    depth_gate = (
        preset["valley_depth_floor"]
        + preset["valley_depth_low_boost"] * depth_low
        + preset["valley_depth_mid_boost"] * depth_mid
        + preset["valley_depth_peak_boost"] * depth_high
    )
    slope_gate = np.clip(
        (slope_u - preset["valley_slope_cutoff"]) / (1.0 - preset["valley_slope_cutoff"]),
        0.0,
        1.0,
    )
    slope_gate = np.power(slope_gate, preset["valley_slope_gate_gamma"])
    plain_depth_gate = _smooth_range(depth_area, *preset["valley_plain_depth_range"])
    slope_gate = np.maximum(slope_gate, preset["valley_plain_depth_support"] * plain_depth_gate)
    valley_color_mask = np.maximum(
        valley_color_mask,
        preset["valley_depth_support"] * depth_area,
    )
    valley_color_mask = valley_color_mask * depth_gate
    valley_color_mask = _smooth_nan(valley_color_mask, preset["valley_color_smooth_sigma_px"])
    valley_color_mask *= 1.0 - preset["valley_convex_block"] * np.clip(ridge, 0.0, 1.0)
    valley_color_mask = valley_color_mask * slope_gate
    ridge_t = np.power(np.clip(ridge_mask, 0.0, 1.0), preset["ridge_gamma"])
    valley_t = np.power(np.clip(valley_color_mask, 0.0, 1.0), preset["valley_gamma"])

    gray_base, gray_scale = preset["elevation_gray"]
    gray = (gray_base + gray_scale * elev_u).astype(np.float32)
    rgb = np.stack([gray, gray, gray], axis=-1)

    # Slope base: white-to-brown color and white-to-black shading.
    slope_brown = _lerp_color(preset["slope_brown_light"], preset["slope_brown_dark"], slope_u)
    rgb = _overlay(rgb, slope_brown, preset["slope_brown_alpha"])
    rgb = _overlay(
        rgb,
        np.full_like(rgb, preset["warm_neutral"], dtype=np.float32),
        preset["warm_neutral_alpha"],
    )

    curv_neutral = preset["curvature_neutral"]
    # Ridge emphasis layer: convex terrain receives the red-brown shoulder tone
    # used as FOL's detailed terrain signal.
    ridge_red = _lerp_color(curv_neutral, preset["ridge_red"], ridge_t)
    ridge_alpha = _soft_alpha(ridge_mask, *preset["ridge_alpha"])
    ridge_slope_gate = np.clip(
        (slope_u - preset["ridge_slope_cutoff"]) / (1.0 - preset["ridge_slope_cutoff"]),
        0.0,
        1.0,
    )
    ridge_alpha *= np.power(ridge_slope_gate, preset["ridge_slope_gate_gamma"])[..., None]
    ridge_alpha *= 1.0 - preset["ridge_valley_fade"] * np.clip(valley_mask, 0.0, 1.0)[..., None]
    rgb = _overlay(rgb, ridge_red, ridge_alpha)

    # Valley blue is a moisture-like overlay: the area spreads softly from
    # depressions, while the core strength still follows the unsmoothed hollow.
    curv_blue = _lerp_color(curv_neutral, preset["valley_blue"], valley_t)
    rgb = _overlay(rgb, curv_blue, _soft_alpha(valley_color_mask, *preset["valley_alpha"]))
    valley_core_start = preset["valley_core_start"]
    valley_core = np.clip((valley - valley_core_start) / (1.0 - valley_core_start), 0.0, 1.0)
    valley_core *= 0.50 + 0.50 * depth_area
    core_blue = _lerp_color(
        preset["valley_core_light"],
        preset["valley_core_dark"],
        np.sqrt(valley_core),
    )
    rgb = _overlay(rgb, core_blue, preset["valley_core_alpha"] * valley_core[..., None])

    slope_black = _lerp_color((255, 255, 255), preset["slope_black"], slope_u)
    rgb = _overlay(rgb, slope_black, preset["slope_black_alpha"])

    # A mild contrast lift keeps micro-relief crisp without washing out color.
    contrast_center, contrast_factor, contrast_offset = preset["contrast"]
    rgb = (rgb - contrast_center) * contrast_factor + contrast_offset
    luminance = (
        0.299 * rgb[..., 0:1]
        + 0.587 * rgb[..., 1:2]
        + 0.114 * rgb[..., 2:3]
    )
    rgb = luminance + (rgb - luminance) * preset["final_saturation"]
    plain_u = 1.0 - np.clip(
        slope_u / preset.get("plain_desaturate_cutoff", 0.22),
        0.0,
        1.0,
    )
    plain_u = np.power(plain_u, preset.get("plain_desaturate_gamma", 1.25))
    plain_sat = 1.0 - preset.get("plain_desaturate_strength", 0.0) * plain_u
    rgb = luminance + (rgb - luminance) * plain_sat[..., None]
    rgb = (rgb - preset["final_luma_center"]) * preset["final_luma_contrast"]
    rgb += preset["final_luma_center"] + preset["final_luma_offset"]
    rgb *= preset["final_density"]
    red = np.where(valid, rgb[..., 0], 0.0)
    green = np.where(valid, rgb[..., 1], 0.0)
    blue = np.where(valid, rgb[..., 2], 0.0)

    rgba = np.zeros(dem.shape + (4,), dtype=np.uint8)
    rgba[..., 0] = np.clip(red, 0, 255).astype(np.uint8)
    rgba[..., 1] = np.clip(green, 0, 255).astype(np.uint8)
    rgba[..., 2] = np.clip(blue, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0).astype(np.uint8)
    return rgba
