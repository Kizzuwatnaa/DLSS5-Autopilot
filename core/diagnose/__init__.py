r"""Read the logs a game left behind and say what happened.

One module until 1.9.0, and 3,700 lines of it: the shapes, the readers,
one reader per route, the bug report's text and the chain that decides
the order - all in the file every fix had to touch. Split by what each
part answers, with the import path unchanged:

    model.py     the Report, the constants, the phrases
    layer.py     whether ReShade reaches the game as a Vulkan layer
    evidence.py  what the folder, the logs and the process say
    process.py   what the game's process had in it: ours, and whose else
    helper.py    the 64-bit helper a 32-bit game's feed hands frames to
    routes.py    one reader per route
    body.py      the text of a bug report
    chain.py     analyse(), and the order it reads things in

`from core import diagnose` and `diagnose._manifest(...)` work exactly
as before - everything is re-exported here. STANDALONE_LOG,
_layer_state and _user_data_roots are patched by the suite and by the
replay tools: patch them on `diagnose.model`, which is where they live
and what the package reads.
"""
from . import model, layer, evidence, process, helper, routes, body, chain  # noqa: F401

# Everything the eight parts define, under this name, so every caller
# outside the package - and 1,500 checks in the suite - keeps reaching
# diagnose.analyse, diagnose._manifest and the rest exactly as before.
# What the suite and the replay tools patch. They are NOT copied onto the
# package: a copy would be a value that looks right and is not the one the
# code reads. Patch them where they live - diagnose.model.
PATCHED = ("STANDALONE_LOG", "_layer_state", "_user_data_roots")

for _part in (model, layer, evidence, process, helper, routes, body, chain):
    for _name in getattr(_part, '__all__', ()):
        if _name not in PATCHED:
            globals().setdefault(_name, getattr(_part, _name))
del _part, _name
