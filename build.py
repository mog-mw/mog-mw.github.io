from pathlib import Path, PurePosixPath
import argparse
import dataclasses
from configparser import ConfigParser
from io import StringIO
import json
import yaml
import os
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

parser = argparse.ArgumentParser()
parser.add_argument("input", type=Path, default="mog", nargs="?", help="input directory (default: %(default)s)")
parser.add_argument("output", type=Path, default="mog_output", nargs="?", help="output directory (default: %(default)s)")
parser.add_argument("-n", "--name", help="modlist name (default: input directory name)")
parser.add_argument("-m", "--minify", action="store_const", default={"indent": 2}, const={"separators": (",", ":")}, help="minify JSON output")
parser.add_argument("-s", "--server", action="store_true", help="start an HTTP server")
parser.add_argument("-p", "--port", type=int, default=1119, help="HTTP server port (default: %(default)s)")


@dataclasses.dataclass
class LoadOrder:
    field: str
    prefix: str
    lines: list[str] = dataclasses.field(default_factory=list)  # lines in the corresponding file
    actual_list: list[str] = dataclasses.field(default_factory=list)  # entries actually used in the modlist. updates lines.


load_orders = {
    "fallbacks":   LoadOrder("fallback values",   "fallback="),
    "data_paths":  LoadOrder("data paths",        "data=C:\\games\\OpenMWMods\\"),
    "archives":    LoadOrder("fallback archives", "fallback-archive="),
    "plugins":     LoadOrder("content files",     "content="),
    "groundcover": LoadOrder("groundcover files", "groundcover="),
}


output_list = []
output_cfg = {"openmw_cfg": {}, "settings_cfg": ""}
path_categories = {} # key: path, value: category it belongs to
settings = ConfigParser()


def main():
    args = parser.parse_args()
    if args.name == None:
        args.name = args.input.name

    for filename in sorted(args.input.joinpath("mods").iterdir()):
        with open(filename) as f:
            y = yaml.load(f, Loader=yaml.Loader)
        for mod in y["mods"]:
            handle_mod(mod, y["category"])

    for filename, load_order in load_orders.items():
        if filename == "fallbacks":
            load_order.lines = load_order.actual_list
        else:
            handle_load_order(args.input.joinpath("load_orders", filename), load_order)

        output_cfg["openmw_cfg"][load_order.field] = ""
        for line in load_order.lines:
            if line.startswith("#") or line == "":
                continue
            if filename == "data_paths":
                line = add_path_prefix(line, path_categories[line])
                line = line.replace("/", "\\")
            output_cfg["openmw_cfg"][load_order.field] += load_order.prefix + line + "\n"

    settings_string = StringIO()
    settings.write(settings_string)
    output_cfg["settings_cfg"] = settings_string.getvalue()

    list_dir = args.output.joinpath("api/lists")
    cfg_dir = args.output.joinpath("api/cfg-generator")
    list_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    with open(list_dir.joinpath(args.name), "w") as f:
        json.dump(output_list, f, **args.minify)
    with open(cfg_dir.joinpath(args.name), "w") as f:
        json.dump(output_cfg, f, **args.minify)

    with open(args.output.joinpath("api/lists/index.html"), "w") as f:
        json.dump([args.name], f)

    print("Build successful.")
    if not args.server:
        return

    os.chdir(args.output)

    with ThreadingHTTPServer(("127.0.0.1", args.port), SimpleHTTPRequestHandler) as httpd:
        print("\nServer running.\n")
        if os.name == "nt":
            print(f"Download mods:\n  umo.exe install --momw-url http://127.0.0.1:{args.port} --sync {args.name}")
            print(f"Generate configuration:\n  momw-configurator.exe config --momw-url http://127.0.0.1:{args.port} --run-navmeshtool --run-validator {args.name}")
            print(f"Generate configuration (quick):\n  momw-configurator.exe config --momw-url http://127.0.0.1:{args.port} --no-delta-plugin --no-groundcoverify --no-lightfixes --run-validator {args.name}")
        else:
            print(f"Download mods:\n  umo install --momw-url http://127.0.0.1:{args.port} --sync {args.name}")
            print(f"Generate configuration:\n  momw-configurator-linux-amd64 config --momw-url http://127.0.0.1:{args.port} --run-navmeshtool --run-validator {args.name}")
            print(f"Generate configuration (quick):\n  momw-configurator-linux-amd64 config --momw-url http://127.0.0.1:{args.port} --no-delta-plugin --no-groundcoverify --no-lightfixes --run-validator {args.name}")
        print()

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nInterrupting.")


def handle_mod(mod: dict, category: str):
    dirname = get_dirname(mod["name"])

    if "data_paths" not in mod:
        if "url" in mod:
            mod["data_paths"] = [""]
        else:
            mod["data_paths"] = []
    for i, path in enumerate(mod["data_paths"]):
        path = add_path_prefix(path, dirname)
        mod["data_paths"][i] = path
        path_categories[path] = category

    for field, load_order in load_orders.items():
        if field in mod:
            load_order.actual_list.extend(mod[field])

    if "settings" in mod:
        settings.read_string(mod["settings"])

    # skip for umo
    if "url" not in mod or "download_info" not in mod:
        return

    new_entry = generate_list_entry()

    new_entry["category"] = category
    for field in ("name", "url", "dl_url", "data_paths", "plugins"):
        if field in mod:
            new_entry[field] = mod[field]
    new_entry["url"] = new_entry["url"].removeprefix("https://modding-openmw.com")
    new_entry["dl_url"] = new_entry["dl_url"].removeprefix("https://modding-openmw.com")
    if "groundcover" in mod:
        new_entry["plugins"].extend(mod["groundcover"])

    new_entry["dir"] = dirname
    new_entry["slug"] = dirname

    for dl_info in mod["download_info"]:
        new_dl_info = generate_dl_info_entry()
        for field in new_dl_info.keys():
            if field in dl_info:
                new_dl_info[field] = dl_info[field]

        new_dl_info["extract_to"] = add_path_prefix(new_dl_info["extract_to"], dirname)
        for action in new_dl_info["actions"]:
            for field in ("path", "src", "dst"):
                if field in action:
                    action[field] = add_path_prefix(action[field], dirname)
            if "paths" in action:
                for i, path in enumerate(action["paths"]):
                    action["paths"][i] = add_path_prefix(path, dirname)
        new_entry["download_info"].append(new_dl_info)

    output_list.append(new_entry)


def handle_load_order(path: str, load_order: LoadOrder):
    with open(path) as f:
        load_order.lines = f.read().split("\n")

    new_lines = []
    insert_index = -1
    for i, line in enumerate(load_order.lines):
        line = line.strip()
        if line == "" or line.startswith("#"):
            if line.startswith("# insert new entries above"):
                insert_index = len(new_lines)
            new_lines.append(line)
            continue

        if line in load_order.actual_list:
            load_order.actual_list.remove(line)
            new_lines.append(line)

    if insert_index == -1:
        load_order.lines = new_lines + load_order.actual_list
    else:
        load_order.lines = \
            new_lines[:insert_index] + \
            load_order.actual_list + \
            new_lines[insert_index:]

    with open(path, "w") as f:
        f.write("\n".join(load_order.lines))


def get_dirname(s: str):
    o = ""
    for c in s:
        if c.isalnum():
            o += c
    return o


def add_path_prefix(path, prefix):
    prefix = PurePosixPath(prefix)
    path = prefix.joinpath(path)
    prefix_dot_dot = prefix.joinpath("..")
    if path.is_relative_to(prefix_dot_dot):
        path = path.relative_to(prefix_dot_dot)
    return str(path)


def generate_list_entry():
    return {
        "name": "",
        "author": "",
        "description": "",
        "url": "",
        "category": "",
        "dl_url": "",
        "usage_notes": "",
        "compat": 0,
        "dir": "",
        "slug": "",
        "date_added": "1970-01-01 00:00:00",
        "date_updated": "1970-01-01 00:00:00",
        "download_info": [],
        "tags": [],
        "on_lists": [],
        "data_paths": [],
        "plugins": [],
    }


def generate_dl_info_entry():
    return {
        "direct_download": None,
        "file_name": None,
        "extract_to": "",
        "nexus_file_id": None,
        "pinned": False,
        "actions": [],
    }


if __name__ == "__main__":
    main()
