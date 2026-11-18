"""按 49 条可检索标记核对源码，证据文件与本脚本同目录。

用法（在 PA_Agent/ 下）：
    .venv/Scripts/python.exe docs/自检证据/_verify_fixes.py
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]          # docs/自检证据 -> docs -> PA_Agent
checks = json.loads((HERE / "_verify_fixes.json").read_text(encoding="utf-8"))
rows = []
for item, rel, needle in checks:
    path = ROOT / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        rows.append((item, rel, "FILE_MISSING", 0))
        continue
    count = text.count(needle)
    rows.append((item, rel, "OK" if count else "MISSING", count))

print("item  file  verdict  hits")
for item, rel, verdict, count in rows:
    print(f"{item:6} {rel:46} {verdict:8} {count}")
bad = [r for r in rows if r[2] != "OK"]
print()
print("SUMMARY:", len(rows) - len(bad), "/", len(rows), "verified")