#!/usr/bin/env python3
"""PINT worker: isolated subprocess for timing fits (DESIGN.md section 2).

Protocol: one JSON request on stdin -> one JSON response on stdout.

    {"op": "fit"|"diagnose", "par": path, "tim": path, ...}
    -> {"ok": true, ...} | {"ok": false, "code": "E_...", "message": "..."}

Running PINT here keeps the asyncio event loop responsive, isolates crashes and
makes cancellation a process-group kill.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any


def _fail(code: str, message: str) -> int:
    json.dump({"ok": False, "code": code, "message": message}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    try:
        req = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        return _fail("E_INVALID_INPUT", f"bad request JSON: {exc}")

    op = req.get("op")
    try:
        import pint  # noqa: F401  (probe: report version when available)
        from pint.fitter import WLSFitter
        from pint.models import get_model
        from pint.toa import get_TOAs
    except Exception as exc:  # pragma: no cover - depends on env
        return _fail("E_ENV_MISSING", f"PINT import failed: {exc}")

    try:
        # allow_T2: psrcat catalogs commonly use the T2 binary model, which PINT 1.1+ refuses to load by default
        model = get_model(req["par"], allow_T2=True)
        toas = get_TOAs(req["tim"], model=model, usepickle=False)
    except Exception as exc:
        return _fail("E_PROCESS_FAILED", f"failed to load par/tim: {exc}")

    try:
        if op == "fit":
            resp = _fit(req, model, toas, WLSFitter)
        elif op == "diagnose":
            resp = _diagnose(req, model, toas)
        else:
            return _fail("E_INVALID_INPUT", f"unknown op: {op!r}")
    except Exception as exc:
        return _fail("E_PROCESS_FAILED", f"{type(exc).__name__}: {exc}")

    json.dump(resp, sys.stdout, allow_nan=False)
    sys.stdout.write("\n")
    return 0


def _residual_payload(fitter: Any, model: Any, toas: Any) -> dict[str, Any]:
    # PINT 1.1+: fitter.resids / Residuals.time_resids; the TOA error vector lives on toas
    res = getattr(fitter, "resids", None) or fitter.residual
    data_us = [float(x) for x in res.time_resids.to_value("us")]
    err_us = [float(x) for x in toas.get_errors().to_value("us")]
    mjds = [float(x) for x in toas.get_mjds().to_value("day")]
    freqs = _toa_freqs_mhz(toas)
    chi2 = float(res.chi2) if math.isfinite(float(res.chi2)) else None
    dof = int(res.dof) if res.dof and res.dof > 0 else max(1, len(data_us) - 1)
    red = chi2 / dof if chi2 is not None else None
    rms = _rms(data_us)
    wrms = _wrms(data_us, err_us)
    return {
        "n_toas": len(data_us),
        "chi2": chi2,
        "dof": dof,
        "reduced_chi2": red,
        "rms_us": rms,
        "wrms_us": wrms,
        "series": {
            "mjd": mjds,
            "res_us": data_us,
            "err_us": err_us,
            "freq_mhz": freqs,
        },
    }


def _toa_freqs_mhz(toas: Any) -> list[float]:
    try:
        return [float(x) for x in toas.table["freq"].quantity.to_value("MHz")]
    except Exception:
        return [float("nan")] * len(toas.table)


def _rms(values: list[float]) -> float:
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def _wrms(values: list[float], errors: list[float]) -> float:
    if not values:
        return 0.0
    wsum = 0.0
    wvsum = 0.0
    for v, e in zip(values, errors):
        w = 1.0 / (e * e) if e and e > 0 else 1.0
        wsum += w
        wvsum += w * v * v
    return math.sqrt(max(0.0, wvsum / wsum)) if wsum > 0 else 0.0


def _fit(req: dict[str, Any], model: Any, toas: Any, fitter_cls: type) -> dict[str, Any]:
    par_path = Path(req["out_par"])
    fit_params = req.get("fit_params")
    if fit_params:
        unknown = [n for n in fit_params if n not in model.params]
        if unknown:
            return {"ok": False, "code": "E_INVALID_INPUT",
                    "message": f"fit_params not present in par file: {', '.join(unknown)}"}
        for name in model.params:
            getattr(model, name).frozen = name not in fit_params

    weighting = req.get("weighting", "1/sigma^2")
    warnings: list[str] = []
    if weighting == "uniform":
        # Uniform weights: assign every TOA the median uncertainty. The choice
        # is recorded so it is never silent (DESIGN.md 7.2).
        errs = [abs(float(e)) for e in toas.get_errors().to_value("us")] or [1.0]
        ref = sorted(errs)[len(errs) // 2] or 1.0
        toas.table["error"][:] = ref
        warnings.append(f"uniform weighting: all TOA errors set to {ref:.3g} us")

    fitter = fitter_cls(toas, model)
    fitter.fit_toas(maxiter=int(req.get("max_iterations", 100)))

    payload = _residual_payload(fitter, model, toas)
    par_path.parent.mkdir(parents=True, exist_ok=True)
    par_path.write_text(fitter.model.as_parfile(), encoding="utf-8")

    parameters = []
    for name in fitter.model.params:
        p = getattr(fitter.model, name)
        q = p.quantity
        # strParameter quantities are bare strings; TZRMJD and the like are astropy Time
        if q is None or isinstance(q, str):
            continue
        raw = getattr(q, "value", q)
        if hasattr(raw, "mjd"):
            value = float(raw.mjd)
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
        unc = None
        if p.uncertainty is not None:
            u = getattr(p.uncertainty, "value", p.uncertainty)
            try:
                unc = float(u)
            except (TypeError, ValueError):
                unc = None
        parameters.append({
            "name": name,
            "value": value,
            "uncertainty": unc,
            "units": str(p.units) if p.units is not None else None,
            "fitted": not bool(p.frozen),
        })

    payload["parameters"] = parameters
    payload["warnings"] = warnings
    payload["ok"] = True
    return payload


def _diagnose(req: dict[str, Any], model: Any, toas: Any) -> dict[str, Any]:
    checks = set(req.get("checks") or
                 ["outliers", "dm", "jumps", "reduced_chi2", "residual_distribution"])
    sigma = float(req.get("outlier_sigma", 5.0))

    from pint.fitter import WLSFitter

    prefit = WLSFitter(toas, model)  # no fit: residuals from the given model
    res = getattr(prefit, "resids", None) or prefit.residual  # PINT 1.1+: resids
    data_us = [float(x) for x in res.time_resids.to_value("us")]
    err_us = [float(x) for x in toas.get_errors().to_value("us")]
    mjds = [float(x) for x in toas.get_mjds().to_value("day")]
    dof = int(res.dof) if res.dof and res.dof > 0 else max(1, len(data_us) - 1)
    chi2 = float(res.chi2) if math.isfinite(float(res.chi2)) else None
    red = chi2 / dof if chi2 is not None else None

    findings: list[dict[str, Any]] = []
    recommendations: list[str] = []
    n_outliers = 0

    if "outliers" in checks and data_us:
        robust_sigma = _mad_sigma(data_us)
        n_outliers = 0
        for i, v in enumerate(data_us):
            if robust_sigma > 0 and abs(v) > sigma * robust_sigma:
                n_outliers += 1
                findings.append({
                    "code": "OUTLIER",
                    "severity": "warning",
                    "message": f"TOA #{i} (MJD {mjds[i]:.4f}) deviates by {abs(v):.1f} us (> {sigma} sigma)",
                    "details": {"index": i, "res_us": v, "err_us": err_us[i]},
                })
        if n_outliers:
            recommendations.append(f"consider removing the {n_outliers} TOAs beyond {sigma} sigma and refitting")

    if "jumps" in checks:
        jumps = [n for n in model.params if n.upper().startswith("JUMP")]
        if jumps:
            findings.append({
                "code": "JUMPS_PRESENT",
                "severity": "info",
                "message": f"model contains {len(jumps)} JUMP parameters: {', '.join(jumps)}",
                "details": {"jumps": jumps},
            })

    if "dm" in checks:
        dm_param = getattr(model, "DM", None)
        if dm_param is not None and dm_param.quantity is not None:
            findings.append({
                "code": "DM_INFO",
                "severity": "info",
                "message": f"model DM = {float(dm_param.quantity.value):.6f} pc cm^-3",
                "details": {"dm": float(dm_param.quantity.value)},
            })

    if "reduced_chi2" in checks and red is not None:
        if red > 3.0:
            findings.append({
                "code": "RED_CHI2_HIGH",
                "severity": "error",
                "message": f"reduced chi2 = {red:.2f} is far above 1; residual structure is unmodeled",
                "details": {"reduced_chi2": red},
            })
            recommendations.append("check whether the ephemeris is stale, whether EPHVER/JUMP/DMX are needed, or whether TOA weights are sensible")
        elif red < 0.3:
            findings.append({
                "code": "RED_CHI2_LOW",
                "severity": "info",
                "message": f"reduced chi2 = {red:.2f} is well below 1; TOA errors may be overestimated",
                "details": {"reduced_chi2": red},
            })

    if "residual_distribution" in checks and len(data_us) >= 8:
        skew, exkurt = _skew_kurtosis(data_us)
        findings.append({
            "code": "DISTRIBUTION",
            "severity": "info",
            "message": f"residual distribution: skew={skew:.2f}, excess kurtosis={exkurt:.2f}",
            "details": {"skew": skew, "excess_kurtosis": exkurt},
        })
        if abs(skew) > 1.0:
            recommendations.append("residuals are clearly skewed; check whether TOAs from different backends/frequencies are mixed in")

    return {
        "ok": True,
        "summary": {
            "n_toas": len(data_us),
            "reduced_chi2": red,
            "rms_us": _rms(data_us),
            "n_outliers": n_outliers,
        },
        "findings": findings,
        "recommendations": recommendations,
        "series": {
            "mjd": mjds,
            "res_us": data_us,
            "err_us": err_us,
            "freq_mhz": _toa_freqs_mhz(toas),
        },
    }


def _mad_sigma(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    med = s[len(s) // 2]
    dev = sorted(abs(v - med) for v in values)
    mad = dev[len(dev) // 2]
    return 1.4826 * mad  # consistency constant for normal samples


def _skew_kurtosis(values: list[float]) -> tuple[float, float]:
    n = len(values)
    mean = sum(values) / n
    m2 = sum((v - mean) ** 2 for v in values) / n
    if m2 <= 0:
        return 0.0, 0.0
    m3 = sum((v - mean) ** 3 for v in values) / n
    m4 = sum((v - mean) ** 4 for v in values) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2 - 3.0


if __name__ == "__main__":
    raise SystemExit(main())
