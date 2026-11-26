"""仓库按组件分目录：host/、relay/、shared/ 是三个源码根。

测试从仓库根收集，这里把三个目录放进 sys.path，测试里的
`import server` / `import relay` / `import branding` 不用各自加路径。
发布包里是扁平的，与这里无关。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
for _d in ("host", "relay", "shared"):
    _p = ROOT / _d
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
