"""v3 Stage-1: full-range daily panel (2023-2026H1) + morning PATH GEOMETRY and CONTINUATION test.
No P&L simulation, no exit parameters. Answers: does the low come before the high, and once the
morning move starts, does it extend? That is the 'volatility expansion extends' hypothesis, testable.
"""
import pandas as pd, numpy as np, os, glob, time, warnings
warnings.filterwarnings("ignore")
SP = "C:/Users/TY_trader1/AppData/Local/Temp/claude/D--BaiduNetdiskDownload-cursor-workflow-adata-sentiment-dashboard/7b0440d5-b856-4519-a4e5-56ed06d6e4ca/scratchpad"
MR = "E:/分钟数据"
PANEL = SP + "/daily_panel_full.pkl"


def lim(c):
    return 20.0 if c.startswith(("300", "301", "688", "689")) else 10.0


def build_panel():
    files = sorted(glob.glob("E:/日K线全部至202606/*.xlsx"))
    t0, rows, ok = time.time(), [], 0
    for f in files[::9]:
        code = os.path.basename(f)[:6]
        if code.startswith(("4", "8", "9")):
            continue
        try:
            d = pd.read_excel(f, usecols=["date", "open", "high", "low", "close", "amount", "涨幅%", "换手率", "复权因子"])
        except Exception:
            continue
        d = d.rename(columns={"涨幅%": "zf", "换手率": "turn", "复权因子": "adjf"})
        d["date"] = pd.to_datetime(d["date"])
        d = d.sort_values("date")
        d = d[(d.date >= "2022-11-01") & (d.date <= "2026-06-30")].reset_index(drop=True)
        if len(d) < 150:
            continue
        ok += 1
        L = lim(code)
        d["ret"] = -d.zf                                       # SOURCE DEFECT: 涨幅% is sign-inverted
        chk = (d.close / d.close.shift(1) - 1) * 100 - d.ret
        if np.nanmedian(np.abs(chk[np.isclose(d.adjf, d.adjf.shift(1))])) > 0.02:
            continue                                           # per-file semantic guard
        d["big"] = (d.ret > 7).astype(int)
        d["prev9"] = d.big.shift(1).rolling(9, min_periods=9).sum()
        d["lu"] = (d.ret >= L - 0.5).astype(int)
        d["pos"] = (d.close - d.low) / (d.high - d.low).replace(0, np.nan)
        d["clean"] = np.isclose(d.adjf, d.adjf.shift(-1))
        d["gap"] = d.open.shift(-1) / d.close - 1
        d["code"] = code
        rows.append(d[d.date >= "2023-01-01"][["code", "date", "ret", "turn", "big", "prev9", "lu", "pos", "clean", "gap"]])
    a = pd.concat(rows, ignore_index=True).dropna(subset=["prev9", "gap"])
    a = a[a.clean]
    print("panel: files=%d %.0fs rows=%d days=%d  %s..%s" % (
        ok, time.time() - t0, len(a), a.date.nunique(), a.date.min().date(), a.date.max().date()))
    a.to_pickle(PANEL)
    return a


a = build_panel() if not os.path.exists(PANEL) else pd.read_pickle(PANEL)
print("minute-data year coverage:", sorted(os.listdir(MR)))


def load(code, d):
    ex = "sh" if code[0] == "6" else ("sz" if code[0] in "03" else "bj")
    p = "%s/%s/%02d/%s/%s/%s.csv" % (MR, d.year, d.month, d.strftime("%Y%m%d"), ex, code)
    if not os.path.exists(p):
        return None
    try:
        m = pd.read_csv(p)
    except Exception:
        return None
    return m if len(m) == 240 else None


cal = np.array(sorted(a.date.unique()))
a["d1"] = a.date.map({cal[i]: cal[i + 1] for i in range(len(cal) - 1)})
bd = lambda s: np.where(s.str.startswith(("300", "301")), "C", np.where(s.str.startswith(("688", "689")), "S", "M"))
a["board"] = bd(a.code)
tgt = a[(a.big == 1) & (a.lu == 0) & (a.prev9 <= 1) & (a.pos >= 0.8)].copy()
tgt["grp"] = "TARGET"
rng = np.random.default_rng(3)
need = tgt.groupby(["date", "board"]).size()
out = []
for (d, b), k in need.items():
    q = a[(a.date == d) & (a.board == b)]
    if len(q):
        out.append(q.sample(n=min(k, len(q)), random_state=int(rng.integers(1e6))))
mkt = pd.concat(out, ignore_index=True)
mkt["grp"] = "MARKET"
ev = pd.concat([tgt, mkt], ignore_index=True).dropna(subset=["d1"])
print("events TARGET=%d MARKET=%d dates=%d" % ((ev.grp == "TARGET").sum(), (ev.grp == "MARKET").sum(), ev.date.nunique()))

rec, miss = [], 0
for r in ev.itertuples():
    m = load(r.code, pd.Timestamp(r.d1))
    if m is None:
        miss += 1
        continue
    o, h, l, c, v = (m[k].values for k in ["open", "high", "low", "close", "volume"])
    if not np.isfinite(o[0]) or o[0] <= 0:
        miss += 1
        continue
    pc = o[0] / (1 + r.gap)                      # D close
    H, L_, C = h[:120], l[:120], c[:120]
    imax, imin = int(np.argmax(H)), int(np.argmin(L_))
    d = {"code": r.code, "date": r.date, "grp": r.grp, "yr": pd.Timestamp(r.date).year,
         "gap": r.gap, "imax": imax, "imin": imin, "low_first": int(imin < imax),
         "mfe": H.max() / pc - 1, "mae": L_.min() / pc - 1,
         # geometry measured from the OPEN, which is what a stop can actually act on
         "mfe_o": H.max() / o[0] - 1, "mae_o": L_.min() / o[0] - 1,
         "r1130": C[119] / pc - 1}
    for lab, k in [("0945", 14), ("1000", 29), ("1030", 59)]:
        d["r_" + lab] = C[k] / o[0] - 1                          # move so far, from the open
        d["fwd_max_" + lab] = H[k + 1:].max() / C[k] - 1         # further upside after that point
        d["fwd_min_" + lab] = L_[k + 1:].min() / C[k] - 1        # further downside after that point
        d["fwd_end_" + lab] = C[119] / C[k] - 1                  # terminal move after that point
    rec.append(d)
p = pd.DataFrame(rec)
p.to_pickle(SP + "/v3_geom.pkl")
print("loaded=%d missing=%d\n" % (len(p), miss))

print("=== A. MORNING PATH GEOMETRY: does the low come before the high? ===")
print("  %-8s %5s %9s %9s %9s %9s" % ("grp", "n", "low_first", "med_imax", "med_imin", "mfe_after_mae"))
for g in ["TARGET", "MARKET"]:
    s = p[p.grp == g]
    print("  %-8s %5d %8.1f%% %9d %9d" % (g, len(s), 100 * s.low_first.mean(), s.imax.median(), s.imin.median()))
print("  (bar index: 0=09:31, 14=09:45, 29=10:00, 59=10:30, 119=11:30)")

print("\n=== B. CONTINUATION: once the morning move has started, does it extend? ===")
print("  measured from the OPEN, so the overnight gap is excluded and a stop could act")
for lab in ["0945", "1000", "1030"]:
    print("\n  --- state at %s ---" % lab)
    print("  %-8s %-14s %5s %10s %10s %10s" % ("grp", "bucket", "n", "fwd_max", "fwd_min", "fwd_end"))
    for g in ["TARGET", "MARKET"]:
        s = p[p.grp == g]
        for lo, hi, nm in [(-9, -0.01, "down >1%"), (-0.01, 0.01, "flat"), (0.01, 0.03, "up 1-3%"), (0.03, 9, "up >3%")]:
            q = s[(s["r_" + lab] >= lo) & (s["r_" + lab] < hi)]
            if len(q) < 30:
                continue
            print("  %-8s %-14s %5d %9.3f%% %9.3f%% %9.3f%%" % (
                g, nm, len(q), 100 * q["fwd_max_" + lab].mean(), 100 * q["fwd_min_" + lab].mean(), 100 * q["fwd_end_" + lab].mean()))

print("\n=== C. YEAR-BY-YEAR (now that 2025/2026 are in play) ===")
print("  %-6s %-8s %5s %9s %9s %9s %9s" % ("yr", "grp", "n", "gap", "mfe_o", "mae_o", "r1130"))
for y in sorted(p.yr.unique()):
    for g in ["TARGET", "MARKET"]:
        s = p[(p.yr == y) & (p.grp == g)]
        if len(s) < 30:
            continue
        print("  %-6d %-8s %5d %8.3f%% %8.3f%% %8.3f%% %8.3f%%" % (
            y, g, len(s), 100 * s.gap.mean(), 100 * s.mfe_o.mean(), 100 * s.mae_o.mean(), 100 * s.r1130.mean()))
