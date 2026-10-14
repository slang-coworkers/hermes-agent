"""YAML for the compose render and the OpenShell installer, without PyYAML.

Hermes 0.22 ships ruamel-backed ``hermes_yaml`` and no PyYAML. Loads go through
``hermes_yaml.safe_load`` unchanged. ``hermes_yaml.safe_dump`` indents block
sequences (``sequence=4, offset=2``), so dumping through it would change the
bytes of every rendered file the fleet's goldens pin; this emitter reproduces
PyYAML's ``safe_dump`` layout instead. The width is load-bearing: PyYAML never
folds a plain scalar with no spaces, while ruamel's default width folds a long
home path onto a second line.

No package imports: ``compose.py`` and ``openshell/installer.py`` load this file
by path, and the installer also runs as a standalone script in a sandbox.
"""

from io import StringIO

from ruamel.yaml import YAML

import hermes_yaml
from hermes_yaml import safe_load  # re-exported: callers use ``yaml.safe_load``


def safe_dump(data, stream=None, *, sort_keys=True, default_flow_style=False):
    y = YAML(typ="safe", pure=True)
    y.Resolver = hermes_yaml._Yaml11Resolver
    y.default_flow_style = default_flow_style
    y.allow_unicode = False
    y.sort_base_mapping_type_on_output = sort_keys
    y.indent(mapping=2, sequence=2, offset=0)
    y.width = hermes_yaml.ROUNDTRIP_YAML_WIDTH
    if stream is not None:
        y.dump(data, stream)
        return None
    out = StringIO()
    y.dump(data, out)
    return out.getvalue()
