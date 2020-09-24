import contextlib
import importlib
import json as stblib_json


MODULES = ("orjson", "ujson")
mainjson = None


__all__ = ["dump", "dumps", "load", "loads", "json_module"]


def import_modules():
    for name in MODULES:
        with contextlib.suppress(Exception):
            yield importlib.import_module(name)
            break


MODULES_IMPORTS = list(import_modules())
MODULES_NAME = [module.__name__ for module in MODULES_IMPORTS]

for item in MODULES:
    with contextlib.suppress(ValueError):
        index = MODULES_NAME.index(item)
        mainjson = MODULES_IMPORTS[index]
        if mainjson:
            json_module = item
            break

if mainjson is None:
    mainjson = stblib_json
    json_module = "json"


def dumps(obj, **kw):
    output = mainjson.dumps(obj)
    if json_module == "orjson" and hasattr(output, "decode"):
        output = output.decode("utf-8")
    return output


def loads(obj, **kw):
    output = mainjson.loads(obj)
    if json_module == "orjson" and hasattr(output, "decode"):
        output = output.decode("utf-8")
    return output


def dump(obj, fp, **kw):
    return fp.write(dumps(obj, **kw))


def load(fp, **kw):
    data = fp.read()
    return loads(data, **kw)
