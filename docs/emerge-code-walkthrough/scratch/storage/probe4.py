"""artifacts.py path and file behaviour, in a throwaway workspace."""

import os
import shutil
from pathlib import Path

from emerge.graph.ports import Ref
from emerge.storage import artifacts as A

ROOT = Path(__file__).parent / "tmp"
ws = ROOT / "art"
shutil.rmtree(ws, ignore_errors=True)
ws.mkdir(parents=True)


def t(label, fn):
    try:
        print(f"[{label}] {fn()!r}")
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] {type(e).__name__}: {e}")


# code versions
for v in ["v007", "7", "", "a/b", "a\\b", "beta", "-1", "v", "..", "V2"]:
    t(f"code_path {v!r}", lambda v=v: A.code_path(ws, "N", v).relative_to(ws).as_posix())
t("code_path name '../x'", lambda: A.code_path(ws, "../x", "1").relative_to(ws).as_posix())
d = ws / "code" / "N"
d.mkdir(parents=True)
for name in ["v10.py", "v9.py", "v010.py", "vbeta.py", "v.py", "valpha.py", "x1.py", "v2.txt"]:
    (d / name).write_text("#")
t("list_generated_versions", lambda: A.list_generated_versions(ws, "N"))
t("list missing type", lambda: A.list_generated_versions(ws, "Missing"))
t("read version ''", lambda: A.read_generated_code(ws, "N", ""))
t("read missing version", lambda: A.read_generated_code(ws, "N", "99"))
A.write_generated_code(ws, "N", "v9", "new")
t("write overwrites v9", lambda: A.read_generated_code(ws, "N", "9"))

# memory dirs
t("memory_dir global", lambda: A.memory_dir(ws, "global").relative_to(ws).as_posix())
t("memory_dir session", lambda: A.memory_dir(ws, "abc").relative_to(ws).as_posix())
t("memory_dir '../x'", lambda: A.memory_dir(ws, "../x").relative_to(ws).as_posix())
t("memory_dir ''", lambda: A.memory_dir(ws, "").relative_to(ws).as_posix())

# copy_memory_asset
src = ROOT / "img.PNG"
src.write_bytes(b"\x89PNG fake")
(ROOT / "img2.png").write_bytes(b"\x89PNG fake")
(ROOT / "img3.jpg").write_bytes(b"\x89PNG fake")
(ROOT / "noext").write_bytes(b"\x89PNG fake")
p1 = A.copy_memory_asset(ws, "global", src)
p2 = A.copy_memory_asset(ws, "global", ROOT / "img2.png")
p3 = A.copy_memory_asset(ws, "global", str(ROOT / "img3.jpg"))
p4 = A.copy_memory_asset(ws, "global", ROOT / "noext")
p5 = A.copy_memory_asset(ws, "S1", src)
print("copies", p1, p2 == p1, p3, p4, p5)
print("files in global asset", sorted(x.name[-8:] for x in (ws / "memory" / "asset").iterdir()))
t("copy missing source", lambda: A.copy_memory_asset(ws, "global", ROOT / "nope.png"))
t("copy directory source", lambda: A.copy_memory_asset(ws, "global", ROOT))
cwd = os.getcwd()
os.chdir(ROOT)
t("copy relative source (resolved against cwd)", lambda: A.copy_memory_asset(ws, "global", "img2.png"))
os.chdir(cwd)
# existing target with other content is not checked
target = ws / p1
target.write_bytes(b"tampered")
A.copy_memory_asset(ws, "global", src)
print("existing target kept as is:", target.read_bytes())

# ref dirs
t("ref_dir", lambda: A.ref_dir(ws, "R", Ref(producer="cam", producer_instance="i1", port="image", key="3")).relative_to(ws).as_posix())
t("Ref key '..'", lambda: Ref(producer="cam", producer_instance="i1", port="image", key=".."))
t("Ref key 'a/b'", lambda: Ref(producer="cam", producer_instance="i1", port="image", key="a/b"))
t("Ref producer_instance 'a/b'", lambda: A.ref_dir(ws, "R", Ref(producer="cam", producer_instance="a/../..", port="image", key="k")).as_posix())
t("node_dir raw", lambda: A.node_dir(ws, "R", "n", "i").relative_to(ws).as_posix())
(ws / "runs" / "R").mkdir(parents=True)
(ws / "runs" / "R" / "nodes").write_text("file in the way")
t("ensure_node_dir with a file in the way", lambda: A.ensure_node_dir(ws, "R", "n", "i"))

# remove_session_memory
(ws / "sessions" / "S1" / "memory").mkdir(parents=True, exist_ok=True)
(ws / "sessions" / "S1" / "other.txt").write_text("x")
(ws / "sessions" / "S2").mkdir(parents=True)
A.remove_session_memory(ws, "S1")
print("S1 gone entirely", not (ws / "sessions" / "S1").exists(), "S2 kept", (ws / "sessions" / "S2").exists())
A.remove_session_memory(ws, "never-existed")
print("missing session dir: no error")
A.remove_session_memory(ws, "")
print("empty id removes sessions/ itself:", not (ws / "sessions").exists())

# dump/load
print("dump_json", A.dump_json({"中文": 1.0, "t": (1, 2), "n": None}))
t("dump_json nan", lambda: A.dump_json(float("nan")))
