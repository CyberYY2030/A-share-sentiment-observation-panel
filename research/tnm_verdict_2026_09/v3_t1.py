"""Falsifiable prediction of the T+1 story: under T+1 the traders who bought on day D can only sell
from D+1's open, so the overnight give-back should scale with D's turnover (the size of the locked-in
inventory), not merely with the size of the move. If turnover does not order it, the story is wrong."""
import pandas as pd, numpy as np, os, warnings
warnings.filterwarnings("ignore")
SP = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
a = pd.read_pickle(SP + "/daily_panel_full.pkl")
a["board"] = np.where(a.code.str.startswith(("300", "301")), "C",
             np.where(a.code.str.startswith(("688", "689")), "S", "M"))
a = a.dropna(subset=["pos", "gap", "turn"])
a["gx"] = a.gap - a.groupby(["date", "board"]).gap.transform("mean")
lim = np.where(a.board.isin(["C", "S"]), 20.0, 10.0)


def nw(x, lag=5):
    x = np.asarray(x, float); n = len(x); m = x.mean(); e = x - m
    g = (e * e).sum() / n
    for k in range(1, lag + 1):
        g += 2 * (1 - k / (lag + 1)) * (e[k:] * e[:-k]).sum() / n
    return m, np.sqrt(max(g, 0) / n)


def line(s, tag):
    if len(s) < 300: return
    d = s.groupby("date").gx.mean(); m, se = nw(d.values)
    print("    %-26s n=%6d day=%4d  turn_med %5.1f%%  gap_excess %+7.4f%%  HAC_t %+6.2f  d>0 %4.1f%%"
          % (tag, len(s), len(d), s.turn.median(), 100 * m, m / se if se else np.nan, 100 * (d > 0).mean()))


print("=== turnover ordering, big-yang non-limit, pos>=0.8 (the actual TARGET pool) ===")
t = a[(a.ret > 7) & (a.ret < lim - 0.5) & (a.pos >= 0.8)].copy()
t["tq"] = pd.qcut(t.turn, 5, labels=["Q1 low", "Q2", "Q3", "Q4", "Q5 high"])
for b, s in t.groupby("tq"): line(s, "%s" % b)

print("\n=== same ordering on ordinary up days 0<ret<=7%%, pos>=0.8 (placebo) ===")
c = a[(a.ret > 0) & (a.ret <= 7) & (a.pos >= 0.8)].copy()
c["tq"] = pd.qcut(c.turn, 5, labels=["Q1 low", "Q2", "Q3", "Q4", "Q5 high"])
for b, s in c.groupby("tq"): line(s, "%s" % b)

print("\n=== turnover ordering INSIDE matched turnover bands: is it the move or the turnover? ===")
u = a[(a.ret > 0) & (a.ret < lim - 0.5) & (a.pos >= 0.8)].copy()
u["tq"] = pd.qcut(u.turn, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
u["mv"] = np.where(u.ret > 7, "big>7%", "small<=7%")
for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
    for mv in ["small<=7%", "big>7%"]:
        line(u[(u.tq == q) & (u.mv == mv)], "%s  %s" % (q, mv))
