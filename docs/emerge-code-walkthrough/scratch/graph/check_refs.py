import re, sys
from pathlib import Path
ROOT = Path("/data/yuqingchi/Code/EMERGE-Policy2.0")
doc = Path("/data/yuqingchi/tmp/emerge-code-walkthrough/parts/03-graph.md").read_text(encoding="utf-8")
# match emerge/.../x.py:N[-M](, N[-M])*
pat = re.compile(r"(emerge/[\w/]+\.py):(\d+)(?:-(\d+))?((?:、\d+(?:-\d+)?)*)")
seen = set()
for m in pat.finditer(doc):
    path = m.group(1)
    ranges = [(int(m.group(2)), int(m.group(3) or m.group(2)))]
    for extra in re.findall(r"、(\d+)(?:-(\d+))?", m.group(4) or ""):
        a, b = extra
        ranges.append((int(a), int(b or a)))
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    for a, b in ranges:
        key = (path, a, b)
        if key in seen: continue
        seen.add(key)
        first = lines[a-1].strip()[:90] if a <= len(lines) else "<OUT OF RANGE>"
        last = lines[b-1].strip()[:60] if b <= len(lines) else "<OUT OF RANGE>"
        print(f"{path}:{a}-{b}: {first}  ||  {last}" if b != a else f"{path}:{a}: {first}")
