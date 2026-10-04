"""Throwaway numerical checks for the statistics review (delete after)."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
from scipy import stats

from climate.analytics import baseline as B
from climate.analytics.daily import DayRow
from climate.experiments import analysis as A
from climate.experiments import switchback as S
from climate.analytics import coupling as C


def test_obf_mc():
    rng = np.random.default_rng(0)
    for K, alpha in ((3, 0.10), (3, 0.05), (2, 0.10)):
        cps = S.plan_checkpoints(42, K, alpha)
        t = np.array([c.info_fraction for c in cps])
        z = np.array([c.z_crit for c in cps])
        inc = np.array([c.alpha_spent for c in cps])
        N = 400_000
        dt = np.diff(np.concatenate([[0], t]))
        incs = rng.standard_normal((N, K)) * np.sqrt(dt)
        Sc = np.cumsum(incs, axis=1)
        Z = Sc / np.sqrt(t)
        up = Z >= z
        lo = Z <= -z
        cross = up | lo
        first = np.argmax(cross, axis=1)
        anyc = cross.any(axis=1)
        anyup = (up & (np.cumsum(cross, axis=1) == 1)).any(axis=1)
        per = np.array([((first == k) & anyc).mean() for k in range(K)])
        print(K, alpha, "z", z.round(4), "inc", inc.round(5), "mc per look", per.round(5), "total", anyc.mean(), "win side", anyup.mean())


def test_seq_inflation():
    for K in (2, 3):
        print("K", K, "infl a=.05 p=.8", S.sequential_inflation(0.05, 0.8, K), "a=.10", S.sequential_inflation(0.10, 0.8, K))


def _ar1(rng, n, rho, sigma, size):
    e = rng.standard_normal((size, n)) * sigma * math.sqrt(1 - rho**2)
    x = np.empty((size, n))
    x[:, 0] = rng.standard_normal(size) * sigma
    for i in range(1, n):
        x[:, i] = rho * x[:, i - 1] + e[:, i]
    return x


def test_block_deff():
    rng = np.random.default_rng(1)
    for rho in (0.3, 0.6):
        for b in (1, 2, 3, 7):
            x = _ar1(rng, b, rho, 1.0, 400_000)
            mc = x.mean(axis=1).var() * b
            print("deff rho", rho, "b", b, "code", round(A._block_design_effect(rho, b), 4), "mc", round(mc, 4))


def _sim_power(n_per_arm, cv, rho, block, alpha, effect, n_looks=3, R=4000, seed=2):
    """Monte Carlo power of the analyze() decision rule (stop_win at any look) for a switchback."""
    rng = np.random.default_rng(seed)
    n_days = 2 * n_per_arm
    cps = S.plan_checkpoints(n_days, n_looks, alpha)
    deff = A._block_design_effect(rho, block)
    mean = 1000.0
    wins = 0
    for r in range(R):
        sched = S.assign_days(["a", "b"], date(2026, 1, 1), n_days, block, int(rng.integers(1 << 30)))
        arms = np.array([a for _, a in sched])
        noise = _ar1(rng, n_days, rho, cv * mean, 1)[0]
        resid = noise + np.where(arms == "b", -effect * mean, 0.0)
        for cp in cps:
            sel = slice(0, cp.day)
            ra = resid[sel][arms[sel] == "a"].tolist()
            rb = resid[sel][arms[sel] == "b"].tolist()
            lk = A._welch(ra, rb, [mean] * cp.day, cp.z_crit, deff)
            if lk is not None and lk.hi_pct < 0:
                wins += 1
                break
    return wins / R


def test_power_vs_mc():
    cv, rho, alpha = 0.21, 0.29, 0.10
    n = A.days_per_arm(10.0, cv, rho, alpha, 0.8)
    print("days_per_arm formula", n)
    for blk in (1, 3, 7):
        print("block", blk, "MC power at formula n", _sim_power(n, cv, rho, blk, alpha, 0.10, R=1500))
        n0 = math.ceil(n / ((1 + rho) / (1 - rho)))
        print("block", blk, "MC power at n without AR inflation", n0, _sim_power(n0, cv, rho, blk, alpha, 0.10, R=1500))
    # null false-win rate
    for blk in (1, 7):
        print("null false win block", blk, _sim_power(60, cv, 0.6, blk, alpha, 0.0, R=3000, seed=5))


def _rows(temps_daily, runtime, unit="main", start=date(2026, 5, 1)):
    out = []
    for i, (t, y) in enumerate(zip(temps_daily, runtime)):
        hourly = [float(t)] * 24
        out.append(DayRow(day=start + timedelta(days=i), unit_key=unit, cool_s=float(max(y, 0)), heat_s=0.0, aux_s=0.0,
                          fan_s=0.0, slots=288, mode="cool", outdoor_mean_f=float(t), outdoor_max_f=float(t),
                          hourly_outdoor_f=hourly, expected_slots=288))
    return out


def test_g14_coverage():
    rng = np.random.default_rng(3)
    for rho in (0.0, 0.5, 0.8):
        cover_m = cover_1 = 0
        R = 600
        n, m = 90, 14
        widths = []
        for r in range(R):
            T = 70 + 12 * rng.random(n + m)
            noise = _ar1(rng, n + m, rho, 1500.0, 1)[0]
            y = 1800.0 * np.maximum(T - 65, 0) + 3000 + noise
            rows = _rows(T, y)
            fit = B.fit_baseline(rows[:n], "cool")
            st = B.residual_stats([(fit, 1.0)])
            exp = np.array([B.expected_seconds(fit, rr) for rr in rows[n:]])
            act = np.array([rr.cool_s for rr in rows[n:]])
            saved = exp.sum() - act.sum()
            hw = st.halfwidth(m, exp.mean())
            cover_m += abs(saved) <= hw
            hw1 = st.halfwidth(1, exp[0])
            cover_1 += abs(exp[0] - act[0]) <= hw1
            widths.append(hw1 / (st.sigma_s * stats.t.ppf(0.95, n - 2)))
        print("rho", rho, "coverage m=14", cover_m / R, "coverage single day", cover_1 / R, "1-day width / correct PI", np.mean(widths))


def test_drift_null():
    rng = np.random.default_rng(4)
    for rho in (0.0, 0.5, 0.8):
        R, n, m = 600, 90, 7
        alarms = 0
        for r in range(R):
            T = 70 + 12 * rng.random(n + m)
            noise = _ar1(rng, n + m, rho, 1500.0, 1)[0]
            y = 1800.0 * np.maximum(T - 65, 0) + 3000 + noise
            rows = _rows(T, y)
            fit = B.fit_baseline(rows[:n], "cool")
            res = [rr.cool_s - B.expected_seconds(fit, rr) for rr in rows[n:]]
            rr_ = min(max(fit.resid_lag1, 0), 0.9)
            se = fit.resid_std_s * math.sqrt((1 + rr_) / ((1 - rr_) * m))
            alarms += abs(np.mean(res) / se) > 2
        print("drift null alarm rate rho", rho, alarms / R, "(nominal 0.0455)")


def test_newey_west():
    rng = np.random.default_rng(6)
    n = 500
    x = rng.standard_normal(n)
    X = np.column_stack([np.ones(n), x])
    y = 1 + 2 * x + rng.standard_normal(n)
    t = np.arange(n)
    res = C.ols_hac(X, y, t, 3)
    # hand NW
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    u = y - X @ beta
    s = X * u[:, None]
    Sm = s.T @ s
    for l in range(1, 4):
        G = s[l:].T @ s[:-l]
        Sm += (1 - l / 4) * (G + G.T)
    inv = np.linalg.inv(X.T @ X)
    cov = inv @ Sm @ inv * n / (n - 2)
    print("NW max abs diff", np.abs(cov - res.cov).max())
    # Coverage of HAC CI for AR(1) regressors & errors
    cover = 0
    R = 2000
    for r in range(R):
        e = _ar1(rng, 300, 0.6, 1.0, 1)[0]
        xx = _ar1(rng, 300, 0.6, 1.0, 1)[0]
        X = np.column_stack([np.ones(300), xx])
        yy = 1 + 0.5 * xx + e
        rr = C.ols_hac(X, yy, np.arange(300), 3)
        b, lo, hi = rr.ci(1)
        cover += lo <= 0.5 <= hi
    print("HAC lag3 coverage AR0.6 (nominal .90)", cover / R)


def _cool_rows_hourly(rng, n, Tmean, amp, bp, slope, icpt, sigma, rho, start):
    days = []
    noise = _ar1(rng, n, rho, sigma, 1)[0]
    for i in range(n):
        tm = Tmean[i]
        hourly = [float(tm + amp * math.sin((h - 9) / 24 * 2 * math.pi)) for h in range(24)]
        dd = sum(max(0.0, t - bp) for t in hourly) / 24
        y = max(0.0, icpt + slope * dd + noise[i])
        days.append(DayRow(day=start + timedelta(days=i), unit_key="main", cool_s=y, heat_s=0.0, aux_s=0.0, fan_s=0.0,
                           slots=288, mode="cool", outdoor_mean_f=float(np.mean(hourly)), outdoor_max_f=max(hourly),
                           hourly_outdoor_f=hourly, expected_slots=288))
    return days


def test_bp_selection_rule():
    rng = np.random.default_rng(11)
    R = 300
    stats_ = {"pinned": 0, "fail_nmbe": 0, "fail_nmbe_only": 0, "cv_ok": 0}
    alt_pass = 0
    bias_same, bias_fall = [], []
    for r in range(R):
        n = 90
        Tm = 72 + 6 * np.sin(np.arange(n + 28) / 9.3 * 2 * np.pi) + rng.normal(0, 3, n + 28)
        Tm[n:] -= 6  # cooler reporting month (autumn)
        rows = _cool_rows_hourly(rng, n + 28, Tm, 9.0, 66.0, 1700.0, 600.0, 900.0, 0.3, date(2026, 6, 1))
        fit = B.fit_baseline(rows[:n], "cool")
        if fit is None:
            continue
        pinned = "constrained" in (fit.notes or "")
        stats_["pinned"] += pinned
        if fit.cvrmse <= 0.2:
            stats_["cv_ok"] += 1
            if abs(fit.nmbe) > 0.005:
                stats_["fail_nmbe_only"] += 1
        stats_["fail_nmbe"] += abs(fit.nmbe) > 0.005
        exp = np.array([B.expected_seconds(fit, rr) for rr in rows[n:]])
        act = np.array([rr.cool_s for rr in rows[n:]])
        bias_fall.append((exp.sum() - act.sum()) / exp.sum() * 100)
    print({k: v for k, v in stats_.items()}, "of", R)
    print("mean claimed savings % (true 0) on cooler month", np.mean(bias_fall), "sd", np.std(bias_fall))


def test_dst_and_house_day(db):
    from datetime import UTC, datetime
    from tests.factories import make_history
    from climate.analytics import daily as D
    make_history(db, days=6, end=datetime(2026, 11, 4, 6, tzinfo=UTC))
    db.flush()
    rows = D.daily_rows(db, date(2026, 10, 31), date(2026, 11, 2), "America/Chicago")
    for r in rows:
        if r.unit_key == "main":
            print(r.day, "slots", r.slots, "exp", r.expected_slots, "hours", len(r.hourly_outdoor_f), "complete", D.is_complete(r),
                  "cool_min", round(r.cool_s / 60, 1), "cdd65", r.cdd65)
    # cross-check fall-back day totals directly
    from sqlalchemy import text
    from climate.timeutil import day_bounds_utc
    a, b = day_bounds_utc(date(2026, 11, 1), "America/Chicago")
    tot = db.execute(text("select sum(comp_cool1), count(*) from runtime_5m where unit_key='main' and ts>=:a and ts<:b"), {"a": a, "b": b}).one()
    print("direct sql nov1", tot[0] / 60, tot[1])


def test_house_day_coverage_bias():
    # A complete (>= 90%) day with 261 of 288 slots: actual is covered seconds, expected is full-day.
    hourly = [80.0] * 24
    fit = B.BaselineFit(unit_key="main", mode="cool", balance_point_f=65.0, intercept_s=0.0, slope_s_per_dd=1800.0,
                        n_days=60, r2=0.9, cvrmse=0.1, nmbe=0.0, resid_std_s=100.0, resid_lag1=0.0,
                        train_start=date(2026, 6, 1), train_end=date(2026, 7, 30))
    full = 1800.0 * 15
    rows = {u: DayRow(day=date(2026, 8, 1), unit_key=u, cool_s=full * 261 / 288, heat_s=0, aux_s=0, fan_s=0, slots=261,
                      mode="cool", outdoor_mean_f=80, outdoor_max_f=80, hourly_outdoor_f=hourly, expected_slots=288)
            for u in ("main", "up", "bed")}
    fits = {(u, "cool"): fit for u in rows}
    hd = A._house_day(date(2026, 8, 1), rows, list(rows), {u: 1.0 for u in rows}, fits, "America/Chicago")
    print("house day: included", hd.included, "actual", hd.actual_s, "expected", hd.expected_s,
          "residual % of expected", round((hd.actual_s - hd.expected_s) / hd.expected_s * 100, 2), "(truth 0)")
