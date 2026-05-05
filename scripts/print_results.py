"""Print variant comparison results."""
import json
from pathlib import Path

results = json.loads(Path("state/variant_results/all_variants_comparison.json").read_text())

headers = ["Variant", "Return%", "Trades", "WinRate%", "PF", "AvgR", "MedR", "MaxDD%", "Expectancy"]
fmt = "{:<32} {:>8} {:>7} {:>8} {:>6} {:>6} {:>6} {:>7} {:>10}"
print(fmt.format(*headers))
print("-" * 100)
for m in results:
    print(fmt.format(
        m.get("label", "?")[:32],
        f"{m.get('total_return_pct', 0):+.1f}",
        str(m.get("total_trades", 0)),
        f"{m.get('win_rate_pct', 0):.1f}",
        f"{m.get('profit_factor', 0):.2f}",
        f"{m.get('avg_r_multiple', 0):.2f}",
        f"{m.get('median_r_multiple', 0):.2f}",
        f"{m.get('max_drawdown_pct', 0):.1f}",
        f"${m.get('expectancy', 0):,.0f}",
    ))

print()
baseline = results[0]
for m in results[1:]:
    dr = m.get("total_return_pct", 0) - baseline.get("total_return_pct", 0)
    ddd = m.get("max_drawdown_pct", 0) - baseline.get("max_drawdown_pct", 0)
    dpf = m.get("profit_factor", 0) - baseline.get("profit_factor", 0)
    dwr = m.get("win_rate_pct", 0) - baseline.get("win_rate_pct", 0)
    dar = m.get("avg_r_multiple", 0) - baseline.get("avg_r_multiple", 0)
    print(f"  {m['label']}")
    print(f"    Return: {dr:+.1f}% | WinRate: {dwr:+.1f}% | PF: {dpf:+.2f} | AvgR: {dar:+.2f} | DD: {ddd:+.1f}%")
    exits = m.get("exit_reasons", {})
    if exits:
        print(f"    Exits: {exits}")
    print()

