import numpy as np
from datetime import timedelta, datetime, UTC
def test_scratch(db):
    from tests.factories import make_history
    from climate.analytics.daily import daily_rows, degree_days_grid, full_day_seconds
    from climate.analytics.baseline import BP_GRID, fit_day_ok
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for i in range(0, 92, 4):
        make_history(db, days=4, end=end - timedelta(days=i), seed=i + 1)
    db.commit()
    for u in ("main", "up", "bed"):
        rows = daily_rows(db, (end - timedelta(days=95)).date(), end.date(), "America/Chicago", [u])
        rows = [r for r in rows if fit_day_ok(r)]
        y = np.array([full_day_seconds(r, "cool") for r in rows])
        X = degree_days_grid(rows, BP_GRID, "cool")
        out = []
        for j in range(len(BP_GRID)):
            x = X[:, j]
            if (x > 0).sum() < 10: continue
            A = np.c_[np.ones_like(x), x]; b, *_ = np.linalg.lstsq(A, y, rcond=None)
            if b[0] < 0:
                s = (x @ y) / (x @ x); b = np.array([0.0, s])
            r = y - A @ b
            out.append((BP_GRID[j], b[0], b[1], (r @ r)))
        smin = min(o[3] for o in out); n = len(y)
        print("\n", u, n)
        for bp, b0, b1, sse in out:
            if bp % 3 == 2 or bp in (65, 66, 67, 68, 69):
                print(f"{bp:.0f} b0={b0:.0f} b1={b1:.0f} lr={n*np.log(sse/smin):.2f}")
