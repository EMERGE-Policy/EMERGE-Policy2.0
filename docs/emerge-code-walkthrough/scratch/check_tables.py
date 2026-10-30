import re, sys, pathlib
def cells(line):
    # count cells split by unescaped pipes (GFM splits on | even inside backticks)
    s = line.strip()
    s = re.sub(r"\\\|", "", s)
    return s.strip("|").count("|") + 1
bad = 0
for path in sys.argv[1:]:
    lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    in_code = False
    i = 0
    while i < len(lines):
        if lines[i].startswith("```"):
            in_code = not in_code
        if not in_code and lines[i].startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i+1].strip()):
            n = cells(lines[i])
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                if cells(lines[j]) != n:
                    bad += 1
                    print(f"{path}:{j+1}: {cells(lines[j])} cells, header has {n}: {lines[j][:100]}")
                j += 1
            i = j
            continue
        i += 1
print("bad rows:", bad)
