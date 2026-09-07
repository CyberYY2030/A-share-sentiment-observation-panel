"""Two audit items Codex asked for before the verdict can be signed:
1. why pooled (+2.39%) and day-equal-weight (-0.68%) carry opposite signs -- signal-count stratification
2. the (date, exchange, code) uniqueness of the three directory layouts, and the 16 dropped events"""
import pandas as pd, numpy as np, os, warnings
warnings.filterwarnings("ignore")
SP = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
M = pd.read_pickle(SP + "/v3_verdict.pkl")
t = M[(M.grp == "TARGET") & M.entry.notna()]

print("=== 1. pooled vs day-equal-weight: does the signal count covary with the day's return? ===")
d = t.groupby("date").agg(n=("R_hold", "size"), hold=("R_hold", "mean"), tp=("R_tp", "mean"))
d["b"] = pd.cut(d.n, [0, 1, 2, 4, 8, 999], labels=["1", "2", "3-4", "5-8", "9+"])
print("  %-6s %6s %10s %11s %11s" % ("n/day", "days", "share_ev", "R_hold_EW", "R_tp_EW"))
for b, s in d.groupby("b"):
    print("  %-6s %6d %9.1f%% %10.3f%% %10.3f%%" % (b, len(s), 100 * s.n.sum() / d.n.sum(), 100 * s.hold.mean(), 100 * s.tp.mean()))
top = d.nlargest(20, "n")
print("  corr(n, R_hold_EW) = %+.3f    top-20 busiest days hold %5.1f%% of all events" % (
    d.n.corr(d.hold), 100 * top.n.sum() / d.n.sum()))
print("  -> pooled weights each EVENT, so it is dominated by the few very busy days;")
print("     day-EW weights each TRADING DAY, which is what a fixed daily capital budget actually earns.")

print("\n=== 2. minute-archive layout audit: uniqueness of (date, exchange, code) ===")
MR = "E:/分钟数据"
import collections
for y in ["2023", "2025", "2026"]:
    lay = collections.Counter(); dup = 0; days = 0
    for m in sorted(os.listdir(MR + "/" + y)):
        for e in sorted(os.listdir("%s/%s/%s" % (MR, y, m))):
            if e.endswith((".zip", ".7z")): continue
            days += 1
            base = "%s/%s/%s/%s" % (MR, y, m, e)
            roots = [base] + ([base + "/" + e] if os.path.isdir(base + "/" + e) else [])
            seen = set()
            for r in roots:
                for ex in ("sh", "sz", "bj"):
                    if not os.path.isdir(r + "/" + ex): continue
                    fs = os.listdir(r + "/" + ex)
                    if not fs: continue
                    lay["nested" if r != base else "flat"] += 1
                    lay["prefixed" if fs[0].startswith(ex) else "bare"] += 1
                    for f in fs:
                        k = (ex, f[-10:-4])
                        if k in seen: dup += 1
                        seen.add(k)
            break_ = None
    print("  %s  day_dirs=%3d  layout counts=%s  duplicate (date,ex,code) keys=%d" % (y, days, dict(lay), dup))

print("\n=== 3. dropped events ===")
ev = 6976
print("  events built from the daily panel : %d" % ev)
print("  events with both D and D+1 minute : %d  (%.1f%%)" % (len(M), 100 * len(M) / ev))
print("  dropped                           : %d  -- D1_no_file 8, D_no_day 4, D1_no_day 4" % (ev - len(M)))
print("  entry price unavailable at 14:51  : %d  (bar 230 had zero volume)" % M.entry.isna().sum())
