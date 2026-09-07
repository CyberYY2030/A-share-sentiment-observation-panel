"""Cache D-day tail features + D+1 morning minute paths for TARGET and date x board matched MARKET.
One I/O pass; every later analysis runs in memory. Instruments why events are dropped."""
import pandas as pd, numpy as np, os, time, warnings, collections
warnings.filterwarnings("ignore")
SP = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
MR = "E:/分钟数据"

a = pd.read_pickle(SP + "/daily_panel_full.pkl")
cal = np.array(sorted(a.date.unique()))
a["d1"] = a.date.map({cal[i]: cal[i + 1] for i in range(len(cal) - 1)})
a["board"] = np.where(a.code.str.startswith(("300", "301")), "C",
             np.where(a.code.str.startswith(("688", "689")), "S", "M"))

tgt = a[(a.big == 1) & (a.lu == 0) & (a.prev9 <= 1) & (a.pos >= 0.8)].copy(); tgt["grp"] = "TARGET"
rng = np.random.default_rng(3); out = []
for (d, b), k in tgt.groupby(["date", "board"]).size().items():
    q = a[(a.date == d) & (a.board == b)]
    if len(q): out.append(q.sample(n=min(k, len(q)), random_state=int(rng.integers(1e6))))
mkt = pd.concat(out, ignore_index=True); mkt["grp"] = "MARKET"
ev = pd.concat([tgt, mkt], ignore_index=True).dropna(subset=["d1"])
print("events TARGET=%d MARKET=%d dates=%d" % ((ev.grp=="TARGET").sum(), (ev.grp=="MARKET").sum(), ev.date.nunique()))

def load(code, d):
    """2023-2025: <day>/<ex>/<code>.csv.  2026: <ex> prefix on the filename, and some days
    carry an extra nested <day>/<day>/ level.  Try every observed layout before giving up."""
    ex = "sh" if code[0] == "6" else ("sz" if code[0] in "03" else "bj")
    ds = d.strftime("%Y%m%d")
    base = "%s/%s/%02d/%s" % (MR, d.year, d.month, ds)
    if not os.path.isdir(base):
        return None, ("zip_day" if os.path.exists(base + ".zip") else "no_day")
    for root in (base, base + "/" + ds):
        for nm in (code, ex + code):
            p = "%s/%s/%s.csv" % (root, ex, nm)
            if os.path.exists(p):
                try: m = pd.read_csv(p)
                except Exception: return None, "unreadable"
                return (m, "ok") if len(m) == 240 else (None, "bad_len")
    return None, "no_file"

meta, P, D0, why, t0 = [], [], [], collections.Counter(), time.time()
for n, r in enumerate(ev.itertuples()):
    if n % 2000 == 0: print("  %d/%d  %.0fs" % (n, len(ev), time.time()-t0), flush=True)
    m0, w0 = load(r.code, pd.Timestamp(r.date))
    if m0 is None: why["D_" + w0] += 1; continue
    m1, w1 = load(r.code, pd.Timestamp(r.d1))
    if m1 is None: why["D1_" + w1] += 1; continue
    o0, h0, l0, c0, v0 = (m0[k].values.astype(float) for k in ["open","high","low","close","volume"])
    o1, h1, l1, c1, v1 = (m1[k].values.astype(float) for k in ["open","high","low","close","volume"])
    if not (np.isfinite(o1[0]) and o1[0] > 0 and np.isfinite(c0[229]) and c0[229] > 0):
        why["bad_px"] += 1; continue
    pc = o1[0] / (1 + r.gap)                                  # D close, from the daily gap
    entry = o1[0] if v1[0] == 0 else np.nan                   # placeholder, replaced below
    entry = o0[230] if (v0[230] > 0 and np.isfinite(o0[230]) and o0[230] > 0) else np.nan
    k15 = np.array([c0[15*j+14] for j in range(16)])          # 15-min bar closes, bar j ends at index 15j+14
    meta.append({"code": r.code, "date": r.date, "d1": r.d1, "grp": r.grp, "board": r.board,
                 "yr": pd.Timestamp(r.date).year, "gap_daily": r.gap, "d_close": pc,
                 "c1450": c0[229], "lo1450": l0[:230].min(), "hi1450": h0[:230].max(),
                 "entry": entry, "ma5_14_45": k15[10:15].mean(), "ma5_14_30": k15[9:14].mean(),
                 "o1": o1[0], "ret": r.ret, "turn": r.turn, "pos": r.pos})
    P.append(np.vstack([o1[:120], h1[:120], l1[:120], c1[:120], v1[:120]]))
M = pd.DataFrame(meta)
np.savez_compressed(SP + "/v3_paths.npz", P=np.array(P, dtype=np.float32))
M.to_pickle(SP + "/v3_meta.pkl")
print("\nkept=%d  dropped=%s  %.0fs" % (len(M), dict(why), time.time()-t0))
print(M.groupby(["yr","grp"]).size().unstack(fill_value=0))
