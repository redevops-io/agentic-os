# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the trusted_import probe (D-NEW-3): the sidecar's OWN spec, with only the entry script and the output name
# replaced, so the probe has exactly the sidecar's excludes, hidden imports and data. Whatever the sidecar spec excludes or collects
# next, the probe follows. Built by packaging/probes/build-trusted-import-probe.sh; never shipped.
import os

_main = os.path.join(SPECPATH, "..", "redevops-sidecar.spec")
_src = open(_main, encoding="utf-8").read()
_probe = os.path.join(SPECPATH, "trusted_import_probe.py")
assert _src.count('["sidecar_main.py"]') == 1 and _src.count('name="redevops-sidecar"') == 1, "sidecar spec changed shape: update the probe spec"
_src = _src.replace('["sidecar_main.py"]', repr([_probe])).replace('name="redevops-sidecar"', 'name="redevops-trusted-import-probe"')
exec(compile(_src, _main, "exec"))
