"""パーサレジストリの公開API。

各発行会社モジュールを import することで register() が走り、レジストリに載る。
smcc/jcb は表示順を保つため明示 import するが、それ以外は parsers/ 配下に
モジュール（例: parsers/mycard.py）を置くだけで自動的に読み込まれる。
このファイル自体の編集は不要。
"""
import importlib
import pkgutil

from .base import (  # noqa: F401
    CardParser,
    ParseResult,
    all_parsers,
    get,
    register,
    type_keys,
    type_labels,
)

# ── 対応カード（表示順を保つため明示 import） ──
from . import smcc  # noqa: F401,E402
from . import jcb   # noqa: F401,E402

# ── 追加カードの自動探索 ──
# 上記2つ・base・先頭が "_" のモジュールを除く、同ディレクトリの残り全モジュールを
# import する。新しい発行会社を足すには parsers/<issuer>.py を置くだけでよい。
_explicit = {"base", "smcc", "jcb"}
for _finder, _mod_name, _is_pkg in pkgutil.iter_modules(__path__):
    if _mod_name in _explicit or _mod_name.startswith("_"):
        continue
    importlib.import_module(f".{_mod_name}", __name__)
