"""Where is the money lost? Split the trade into the untradable overnight leg and the intraday leg,
then test the same names entered the NEXT MORNING instead of the prior tail."""
import pandas as pd, numpy as np, os, warnings
warnings.filterwarnings("ignore")
SP = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
COST = 0.003
M = pd.read_pickle(SP + "/v3_verdict.pkl").reset_index(drop=True)
P = np.load(SP + "/v3_paths.npz")["P"].astype(float)
O, H, L, C, V = (P[:, i, :] for i in range(5))
n = len(M); o0 = O[:, 0]


def nw(x, lag=5):
    x = np.asarray(x, float); k = len(x); m = x.mean(); e = x - m
    g = (e * e).sum() / k
    for j in range(1, lag + 1):
        g += 2 * (1 - j / (lag + 1)) * (e[j:] * e[:-j]).sum() / k
    return m, np.sqrt(max(g, 0) / k)


def rep(s, col, tag):
    d = s.groupby("date")[col].mean(); m, se = nw(d.values)
    print("    %-30s ev=%5d day=%4d  dayEW %+7.4f%%  HAC_t %+5.2f  95%%LB %+7.4f%%  d>0 %4.1f%%"
          % (tag, len(s), len(d), 100 * m, m / se if se else np.nan, 100 * (m - 1.645 * se), 100 * (d > 0).mean()))


def paired(a, b, col, tag):
    j = pd.concat([a.groupby("date")[col].mean().rename("A"), b.groupby("date")[col].mean().rename("B")], axis=1).dropna()
    m, se = nw((j.A - j.B).values)
    print("    PAIRED %-23s day=%4d  %+7.4f%%  HAC_t %+5.2f  95%%LB %+7.4f%%  d>0 %4.1f%%"
          % (tag, len(j), 100 * m, m / se if se else np.nan, 100 * (m - 1.645 * se), 100 * ((j.A - j.B) > 0).mean()))


M["leg_overnight"] = o0 / M.entry - 1                 # 14:51 fill -> next open. Untradable, unavoidable.
M["leg_intraday"] = C[:, 119] / o0 - 1                # next open -> 11:30. Everything a rule can touch.
E45 = O[:, 15]                                        # 09:46 open: first fillable bar after the 09:45 state
M["morn_ret"] = C[:, 119] / E45 - 1 - COST            # enter next morning instead of the prior tail
M["morn_mfe"] = np.array([H[i, 15:].max() for i in range(n)]) / E45 - 1
tp = E45 * 1.015
hit = np.array([np.any((H[i, 15:] >= tp[i]) & (V[i, 15:] > 0)) for i in range(n)])
M["morn_tp"] = np.where(hit, 0.015, C[:, 119] / E45 - 1) - COST
M["st45"] = C[:, 14] / o0 - 1

print("=== 1. WHERE THE MONEY GOES: overnight leg vs intraday leg (day-equal-weight) ===")
for name, msk in [("2023-2024", (M.date <= "2024-12-31")), ("2025", (M.date >= "2025-01-01") & (M.date <= "2025-12-31")),
                  ("2026H1", M.date >= "2026-01-01"), ("ALL", M.date > "2000-01-01")]:
    print("  --- %s ---" % name)
    for g in ["TARGET", "MARKET"]:
        s = M[msk & (M.grp == g) & M.entry.notna()]
        rep(s, "leg_overnight", g + " overnight  (14:51 -> next open)")
        rep(s, "leg_intraday", g + " intraday   (open -> 11:30)")
    t = M[msk & (M.grp == "TARGET") & M.entry.notna()]; mk = M[msk & (M.grp == "MARKET") & M.entry.notna()]
    paired(t, mk, "leg_overnight", "overnight T-M")
    paired(t, mk, "leg_intraday", "intraday  T-M")

print("\n=== 2. SAME NAMES, ENTERED NEXT MORNING AT 09:46 INSTEAD (skips the overnight leg) ===")
for name, msk in [("2023-2024", M.date <= "2024-12-31"), ("2025", (M.date >= "2025-01-01") & (M.date <= "2025-12-31")),
                  ("2026H1", M.date >= "2026-01-01")]:
    print("  --- %s ---" % name)
    t = M[msk & (M.grp == "TARGET")]; mk = M[msk & (M.grp == "MARKET")]
    rep(t, "morn_ret", "TARGET 09:46->11:30")
    rep(mk, "morn_ret", "MARKET 09:46->11:30")
    paired(t, mk, "morn_ret", "T-M hold")
    paired(t, mk, "morn_tp", "T-M +1.5% limit")
    tf = t[(t.st45 >= -0.01) & (t.st45 < 0.01)]; mf = mk[(mk.st45 >= -0.01) & (mk.st45 < 0.01)]
    rep(tf, "morn_ret", "TARGET flat-at-09:45 only")
    paired(tf, mf, "morn_ret", "T-M flat-only")

print("\n=== 3. is the amplitude excess itself stable? (paired MFE from 09:46, by year) ===")
for name, msk in [("2023-2024", M.date <= "2024-12-31"), ("2025", (M.date >= "2025-01-01") & (M.date <= "2025-12-31")),
                  ("2026H1", M.date >= "2026-01-01")]:
    paired(M[msk & (M.grp == "TARGET")], M[msk & (M.grp == "MARKET")], "morn_mfe", "%s  MFE T-M" % name)
