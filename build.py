from pathlib import Path, PurePosixPath
import argparse
import dataclasses
from configparser import ConfigParser
from io import StringIO
import json
import yaml
from os import chdir
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from threading import Thread
import shutil
import subprocess
import platform
import re

parser = argparse.ArgumentParser()
parser.add_argument("input", type=Path, default="mog", nargs="?", help="input directory (default: %(default)s)")
parser.add_argument("output", type=Path, default="mog_output", nargs="?", help="output directory (default: %(default)s)")
parser.add_argument("--name", help="modlist name (default: input directory name)")
parser.add_argument("-a", "--bind", default="127.0.0.1", help="HTTP server bind address (default: %(default)s)")
parser.add_argument("-p", "--port", type=int, default=1119, help="HTTP server port (default: %(default)s)")
parser.add_argument("--momw-dir", type=Path, help="Path to the MOMW Tools Pack + Greenmote")
parser.add_argument("-e", "--exclude", action="append", default=[], help="Excludes the specified YAML file when building the modlist. Can be specified multiple times.")

parser.add_argument("-b", "--build", action="store_true", help="Build the modlist")
parser.add_argument("-m", "--minify", action="store_const", default={"indent": 2}, const={"separators": (",", ":")}, help="minify JSON output")
parser.add_argument("-u", "--umo", action="store_true", help="Install mods with umo (slow)")
parser.add_argument("-c", "--configurator", action="store_true", help="Install modlist configuration with MOMW Configurator")
parser.add_argument("-d", "--delta-plugin", action="store_true", help="Run DeltaPlugin (slow, required after deleting plugins)")
parser.add_argument("-n", "--navmesh", action="store_true", help="Run the OpenMW Navmeshtool (very slow)")
parser.add_argument("-v", "--verbose", action="store_true", help="Pass --verbose to MOMW Configurator")
parser.add_argument("-g", "--greenmote", action="store_true", help="Convert groundcover with Greenmote")


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
path_categories = {}  # key: path, value: category it belongs to
settings = ConfigParser()
dirnames = set()

args: argparse.Namespace
versions_json = {}
system = platform.system().lower()
if system == "darwin":
    system = "macos"
arch = platform.machine()
if arch == "x86_64":
    arch = "amd64"


def main():
    global args, versions_json

    args = parser.parse_args()

    if args.name == None:
        args.name = args.input.name
    if args.momw_dir:
        args.momw_dir = args.momw_dir.resolve()
    if not (args.build or args.configurator or args.greenmote):
        args.build, args.configurator, args.greenmote = True, True, True

    with open(args.input.joinpath("versions.json")) as f:
        versions_json = json.load(f)

    if args.build:
        print(f'Building modlist "{args.name}"...')
        build()
        print(f'Modlist "{args.name}" built successfully.')

    configurator = "momw-configurator"
    if system != "windows":
        configurator += f"-{system}-{arch}"

    if (args.umo or args.configurator):
        chdir(args.output)

        with ThreadingHTTPServer((args.bind, args.port), SimpleHTTPRequestHandler) as httpd:
            server_thread = Thread(target=httpd.serve_forever)
            server_thread.start()

            server_address = f"http://{args.bind}:{args.port}"
            print(f"Started HTTP server on {server_address}.")

            if args.umo:
                print("Running umo...")
                umo_args = [get_tool_path("umo"), "install",
                            "--momw-url", server_address,
                            "--sync", args.name]
                subprocess.run(umo_args)

            if args.configurator:
                print("Running mowm-configurator...")
                configurator = get_tool_path(configurator)

                configurator_args = [get_tool_path(configurator), "config",
                                     "--momw-url", server_address,
                                     "--no-groundcoverify", "--run-validator",
                                     args.name]
                if args.navmesh:
                    configurator_args.insert(5, "--run-navmeshtool")
                if not args.delta_plugin:
                    configurator_args.insert(5, "--no-delta-plugin")
                if args.verbose:
                    configurator_args.insert(2, "--verbose")
                subprocess.run(configurator_args)

            print("Stopping HTTP server.")
            httpd.shutdown()
            httpd.server_close()
            server_thread.join()

    if args.greenmote:
        print("Running greenmote...")
        umo_dir = Path()
        with subprocess.Popen((get_tool_path(configurator), "info"),
                              stdout=subprocess.PIPE, text=True,
                              bufsize=1) as info:
            if not info.stdout:
                print("failed to get info from momw-configurator")
                return
            for line in info.stdout:
                if line.startswith("ModBaseDir:"):
                    umo_dir = Path(line.removeprefix("ModBaseDir:").strip())

        greenmote_args = [get_tool_path("greenmote"), "convert",
                          "--output", umo_dir.joinpath(args.name, "Tools", "MOMWToolsPack")]
        subprocess.run(greenmote_args)


def get_tool_path(name):
    if args.momw_dir:
        return args.momw_dir.joinpath(name)
    path = shutil.which(name)
    if path == None:
        print(f"Couldn't find \"{name}\", make sure your MOMW Tools directory is in PATH, or specify --momw-dir")
        exit(1)
    return path


def build():
    for filename in sorted(args.input.joinpath("mods").iterdir()):
        if filename.name in args.exclude:
            continue

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


def handle_mod(mod: dict, category: str):
    if "dir" in mod:
        dirname = mod["dir"]
    else:
        dirname = get_dirname(mod["name"])

    # duplicate dirnames would likely cause big issues,
    # both with umo, and with load_orders/data_paths
    if dirname in dirnames:
        raise ValueError(f"Duplicate dir name: {dirname}")
    dirnames.add(dirname)

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
        if field not in mod:
            continue
        for entry in mod[field]:
            # don't add duplicate entries, in case things are being overwritten
            if entry not in load_order.actual_list:
                load_order.actual_list.append(entry)

    if "settings" in mod:
        settings.read_string(mod["settings"])

    # skip for umo
    if "url" not in mod:
        return

    new_entry = generate_list_entry()

    dl_type = "direct"
    if re.match(r"https://www.nexusmods.com/(.*?)/.*/([0-9]+)", mod["url"]):
        dl_type = "nexus"

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

    mod.setdefault("download_info", [])
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

        if dl_type == "nexus" and not new_dl_info["nexus_file_id"]:
            url = mod["url"]
            file_name = dl_info["file_name"]
            if url in versions_json and file_name in versions_json[url]:
                new_dl_info["nexus_file_id"] = versions_json[url][file_name]
                new_dl_info["pinned"] = True

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
