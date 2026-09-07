"""Mechanism check on the whole daily panel: is the overnight leg systematically worse the higher
the stock closes in its own daily range? De-market by the same-day same-board mean so the result
cannot be a board-composition artifact."""
import pandas as pd, numpy as np, os, warnings
warnings.filterwarnings("ignore")
SP = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
a = pd.read_pickle(SP + "/daily_panel_full.pkl")
a["board"] = np.where(a.code.str.startswith(("300", "301")), "C",
             np.where(a.code.str.startswith(("688", "689")), "S", "M"))
a = a.dropna(subset=["pos", "gap"])
a["gx"] = a.gap - a.groupby(["date", "board"]).gap.transform("mean")   # same-day same-board excess
a["yr"] = a.date.dt.year
print("panel rows=%d days=%d %s..%s" % (len(a), a.date.nunique(), a.date.min().date(), a.date.max().date()))


def nw(x, lag=5):
    x = np.asarray(x, float); n = len(x); m = x.mean(); e = x - m
    g = (e * e).sum() / n
    for k in range(1, lag + 1):
        g += 2 * (1 - k / (lag + 1)) * (e[k:] * e[:-k]).sum() / n
    return m, np.sqrt(max(g, 0) / n)


def line(s, tag):
    if len(s) < 500: return
    d = s.groupby("date").gx.mean(); m, se = nw(d.values)
    print("    %-30s n=%7d day=%4d  gap_excess dayEW %+7.4f%%  HAC_t %+6.2f  d>0 %4.1f%%"
          % (tag, len(s), len(d), 100 * m, m / se if se else np.nan, 100 * (d > 0).mean()))


print("\n=== A. overnight gap excess by close-position, ALL days (n=%d) ===" % len(a))
a["pb"] = pd.cut(a.pos, [-.01, .2, .4, .6, .8, 1.01], labels=["0-.2", ".2-.4", ".4-.6", ".6-.8", ".8-1"])
for b, s in a.groupby("pb"): line(s, "pos %s" % b)

print("\n=== B. same, restricted to big up days (ret>7%%, non-limit) ===")
lim = np.where(a.board.isin(["C", "S"]), 20.0, 10.0)
big = a[(a.ret > 7) & (a.ret < lim - 0.5)]
for b, s in big.groupby("pb"): line(s, "big-yang pos %s" % b)

print("\n=== C. is it stable year by year? (big-yang, pos>=0.8) ===")
t = big[big.pos >= 0.8]
for y, s in t.groupby("yr"): line(s, "%d" % y)
line(t, "ALL 2023-2026H1")

print("\n=== D. control: ordinary up days 0<ret<=7%%, pos>=0.8 ===")
for y, s in a[(a.ret > 0) & (a.ret <= 7) & (a.pos >= 0.8)].groupby("yr"): line(s, "%d" % y)
line(a[(a.ret > 0) & (a.ret <= 7) & (a.pos >= 0.8)], "ALL")

print("\n=== E. mirror: big DOWN days closing at the LOW (ret<-7%%, pos<=0.2) ===")
line(a[(a.ret < -7) & (a.ret > -(lim - 0.5)) & (a.pos <= 0.2)], "ALL 2023-2026H1")
