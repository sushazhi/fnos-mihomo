# -*- coding: utf-8 -*-
"""unittest discover 的发现垫片。

`python3 -m unittest discover -s tests` 默认只收 `test*.py`，而本套件的文件名
`cfopt_test.py` 是冻结契约的一部分（不匹配默认模式），所以这里用一个 shim 把它的
用例转发出来，让裸 discover 也能跑全：

    python3 -m unittest discover -s tests          # 通过本 shim 收齐用例
    python3 tests/cfopt_test.py                    # 直接跑，带分组汇总

shim 只做转发，不改任何断言；分组汇总仍由 `cfopt_test.py` 自己打印。
"""

try:
    from cfopt_test import *  # noqa: F401,F403  (discover 时 tests/ 在 sys.path[0])
except ImportError:          # 以 `python3 -m unittest tests.test_cfopt` 从仓库根跑时
    from tests.cfopt_test import *  # noqa: F401,F403