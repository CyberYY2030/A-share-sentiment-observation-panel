"""Decisive tests agreed with Codex Sol (round 2).
G  geometry: pre-peak drawdown -- the quantity that decides whether a stop is even reachable
S  STOP COUNTERFACTUAL: R(-2% stop from entry) - R(no stop, 11:30 exit). <=0 kills the price stop.
B  continuation, day-equal-weight, paired TARGET-MARKET inside each bucket
L  the LOCKED rule: +1.5% limit from entry, 11:30 time exit, NO price stop, 0.3% cost, design set only
"""
import pandas as pd, numpy as np, os, warnings
warnings.filterwarnings("ignore")
SP = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
COST, TP_R, SL_R = 0.003, 0.015, 0.02

M = pd.read_pickle(SP + "/v3_meta.pkl").reset_index(drop=True)
P = np.load(SP + "/v3_paths.npz")["P"].astype(float)          # (n,5,120) = o,h,l,c,v
O, H, L, C, V = (P[:, i, :] for i in range(5))
M["lim"] = np.where(M.code.str.startswith(("300", "301", "688", "689")), 20.0, 10.0)
M["prevc"] = M.d_close / (1 + M.ret / 100)
M["ret1450"] = M.c1450 / M.prevc - 1
M["pos1450"] = (M.c1450 - M.lo1450) / (M.hi1450 - M.lo1450).replace(0, np.nan)
M["ma5up"] = M.ma5_14_45 > M.ma5_14_30
M["gate"] = (M.ret1450 > 0.07) & (M.pos1450 >= 0.8) & (M.ret1450 < (M["lim"] - 0.5) / 100)
o0 = O[:, 0]
M["gap_e"] = o0 / M.entry - 1
print("cached n=%d  entry NaN=%d" % (len(M), M.entry.isna().sum()))
print(M.groupby(["yr", "grp"]).size().unstack(fill_value=0), "\n")


def nw(x, lag=5):
    x = np.asarray(x, float); n = len(x); m = x.mean(); e = x - m
    g = (e * e).sum() / n
    for k in range(1, lag + 1):
        g += 2 * (1 - k / (lag + 1)) * (e[k:] * e[:-k]).sum() / n
    return m, np.sqrt(max(g, 0) / n)


def rep(sub, col, tag):
    d = sub.groupby("date")[col].mean(); m, se = nw(d.values)
    print("    %-32s ev=%5d day=%4d  dayEW %+7.4f%%  HAC_t %+5.2f  95%%LB %+7.4f%%  d>0 %4.1f%%  pooled %+7.4f%%"
          % (tag, len(sub), len(d), 100 * m, m / se if se > 0 else np.nan,
             100 * (m - 1.645 * se), 100 * (d > 0).mean(), 100 * sub[col].mean()))


def paired(a_, b_, col, tag):
    j = pd.concat([a_.groupby("date")[col].mean().rename("A"),
                   b_.groupby("date")[col].mean().rename("B")], axis=1).dropna()
    m, se = nw((j.A - j.B).values)
    print("    PAIRED %-25s day=%4d  %+7.4f%%  HAC_t %+5.2f  95%%LB %+7.4f%%  d>0 %4.1f%%"
          % (tag, len(j), 100 * m, m / se if se > 0 else np.nan, 100 * (m - 1.645 * se), 100 * ((j.A - j.B) > 0).mean()))


# ---------- G ----------
print("=== G. PATH GEOMETRY: what a stop would have to survive ===")
ipk, itr = H.argmax(1), L.argmin(1)
n = len(M)
M["ipk"], M["itr"] = ipk, itr
M["pre_dd"] = [L[i, :ipk[i] + 1].min() / o0[i] - 1 for i in range(n)]
M["post_gb"] = C[:, 119] / H.max(1) - 1
print("  %-8s %6s %8s %9s %8s %11s %12s %10s" % ("grp", "n", "low1st", "same_bar", "med_ipk", "pre_dd_med", "pre_dd<=-2%", "post_gb"))
for g in ["TARGET", "MARKET"]:
    s = M[M.grp == g]
    print("  %-8s %6d %7.1f%% %8.1f%% %8d %10.2f%% %11.1f%% %9.2f%%" % (
        g, len(s), 100 * (s.itr < s.ipk).mean(), 100 * (s.itr == s.ipk).mean(), s.ipk.median(),
        100 * s.pre_dd.median(), 100 * (s.pre_dd <= -0.02).mean(), 100 * s.post_gb.mean()))
print("  pre_dd = worst drawdown from the open BEFORE the morning high is made")

# ---------- S ----------
print("\n=== S. STOP COUNTERFACTUAL (levels measured from the real 14:51 entry price) ===")
E = M.entry.values
r_hold = C[:, 119] / E - 1
r_stop = np.full(n, np.nan); r_tp = np.full(n, np.nan); r_both = np.full(n, np.nan)
hit_tp = np.zeros(n, bool); hit_sl = np.zeros(n, bool)
for i in range(n):
    e = E[i]
    if not np.isfinite(e) or e <= 0:
        continue
    S_, T_ = e * (1 - SL_R), e * (1 + TP_R)
    h, l, c, v = H[i], L[i], C[i], V[i]
    js = np.where((l <= S_) & (v > 0))[0]
    jt = np.where((h >= T_) & (v > 0))[0]
    op = O[i, 0]
    # stop only
    if op <= S_: r_stop[i], hit_sl[i] = op / e - 1, True
    elif len(js): r_stop[i], hit_sl[i] = S_ / e - 1, True
    else: r_stop[i] = c[119] / e - 1
    # take-profit only
    if op >= T_: r_tp[i], hit_tp[i] = op / e - 1, True
    elif len(jt): r_tp[i], hit_tp[i] = T_ / e - 1, True
    else: r_tp[i] = c[119] / e - 1
    # both; a bar touching each level resolves to the stop (conservative)
    if op <= S_ or op >= T_: r_both[i] = op / e - 1
    elif len(js) and (not len(jt) or js[0] <= jt[0]): r_both[i] = S_ / e - 1
    elif len(jt): r_both[i] = T_ / e - 1
    else: r_both[i] = c[119] / e - 1
for k, v_ in [("hold", r_hold), ("stop", r_stop), ("tp", r_tp), ("both", r_both)]:
    M["R_" + k] = v_ - COST
M["hit_tp"], M["hit_sl"] = hit_tp, hit_sl
M["d_stop"] = M.R_stop - M.R_hold
M["d_tp"] = M.R_tp - M.R_hold
M["mfe_e"] = H.max(1) / M.entry - 1
for g in ["TARGET", "MARKET"]:
    s = M[(M.grp == g) & M.entry.notna()]
    print("  --- %s (n=%d) ---" % (g, len(s)))
    for k in ["R_hold", "R_stop", "R_tp", "R_both"]:
        rep(s, k, k)
    rep(s, "d_stop", "DELTA  stop - hold  <== VERDICT")
    rep(s, "d_tp", "DELTA  tp   - hold")
    print("    stop fired %4.1f%%   tp filled %4.1f%%   entry->D+1 open %+.3f%%   MFE_from_entry %+.3f%%" % (
        100 * s.hit_sl.mean(), 100 * s.hit_tp.mean(), 100 * s.gap_e.mean(), 100 * s.mfe_e.mean()))
    st = s[s.hit_sl]
    print("    on the %d stopped paths: stop exit %+.3f%%  vs  holding to 11:30 %+.3f%%  (gap = what the stop cost)" % (
        len(st), 100 * st.R_stop.mean(), 100 * st.R_hold.mean()))

# ---------- B ----------
print("\n=== B. CONTINUATION, day-equal-weight, paired inside each bucket ===")
for lab, k in [("0945", 14), ("1030", 59)]:
    M["st"] = C[:, k] / o0 - 1
    M["fmax"] = [H[i, k + 1:].max() / C[i, k] - 1 for i in range(n)]
    M["fmin"] = [L[i, k + 1:].min() / C[i, k] - 1 for i in range(n)]
    M["fend"] = C[:, 119] / C[:, k] - 1
    print("  --- state at %s ---" % lab)
    for lo, hi, nm in [(-9, -0.01, "down>1%"), (-0.01, 0.01, "flat"), (0.01, 0.03, "up1-3%"), (0.03, 9, "up>3%")]:
        t = M[(M.grp == "TARGET") & (M.st >= lo) & (M.st < hi)]
        m_ = M[(M.grp == "MARKET") & (M.st >= lo) & (M.st < hi)]
        if len(t) < 60 or len(m_) < 60:
            continue
        for col in ["fmax", "fmin", "fend"]:
            paired(t, m_, col, "%-8s %-5s T-M" % (nm, col))

# ---------- L ----------
print("\n=== L. LOCKED RULE: +1.5%% limit from entry, 11:30 exit, NO price stop, 0.3%% cost ===")
seg = {"DESIGN 2023-2024": (M.date >= "2023-01-01") & (M.date <= "2024-12-31"),
       "CONFIRM 2025": (M.date >= "2025-01-01") & (M.date <= "2025-12-31"),
       "PARTIAL 2026H1": (M.date >= "2026-01-01") & (M.date <= "2026-06-30")}
for name, msk in seg.items():
    print("  --- %s ---" % name)
    t = M[msk & (M.grp == "TARGET") & M.entry.notna()]
    mk = M[msk & (M.grp == "MARKET") & M.entry.notna()]
    tg = t[t.gate & t.ma5up]
    rep(t, "R_tp", "TARGET raw")
    rep(tg, "R_tp", "TARGET +14:50gate+MA5up")
    rep(mk, "R_tp", "MARKET matched")
    paired(t, mk, "R_tp", "TARGET-MARKET")
    paired(tg, mk, "R_tp", "TARGETgated-MARKET")
print("\n  P(MFE from entry >= x), design set:")
d = (M.date >= "2023-01-01") & (M.date <= "2024-12-31")
print("    %-8s %8s %8s %8s %8s %8s" % ("grp", "1.0%", "1.5%", "2.0%", "3.0%", "5.0%"))
for g in ["TARGET", "MARKET"]:
    s = M[d & (M.grp == g) & M.entry.notna()]
    print("    %-8s %7.1f%% %7.1f%% %7.1f%% %7.1f%% %7.1f%%" % (g, *[100 * (s.mfe_e >= x).mean() for x in [.01, .015, .02, .03, .05]]))
M.to_pickle(SP + "/v3_verdict.pkl")
