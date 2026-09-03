from urllib.request import urlopen, Request
from pathlib import Path
import argparse
import dataclasses
import json
import yaml
from concurrent.futures import ThreadPoolExecutor
import shutil
import subprocess
import re
from functools import cache

parser = argparse.ArgumentParser()
parser.add_argument("input", type=Path, default="mog", nargs="?", help="input directory (default: %(default)s)")
parser.add_argument("--momw-dir", type=Path, help="Path to the MOMW Tools Pack + Greenmote")
args = parser.parse_args()


@dataclasses.dataclass
class UmoFile:
    dinfo: dict
    mod: dict
    latest: int | str | None = None
    version_labels: dict = dataclasses.field(default_factory=dict)


nexus_key = ""


def main():
    get_nexus_key()

    files: list[UmoFile] = []

    for filename in sorted(args.input.joinpath("mods").iterdir()):
        with open(filename) as f:
            y = yaml.load(f, Loader=yaml.Loader)
        for mod in y["mods"]:
            if "download_info" in mod:
                for dinfo in mod["download_info"]:
                    files.append(UmoFile(dinfo=dinfo, mod=mod))

    with ThreadPoolExecutor(max_workers=48) as executor:
        results = executor.map(process_file, files)

        # raise errors
        for result in results:
            result

    with open(args.input.joinpath("versions.json")) as f:
        versions_json: dict = json.load(f)

    new_versions_json = {}

    for file in files:
        if not file.latest:
            continue

        mod_name = file.mod["name"]
        url = file.mod["url"]
        file_name = file.dinfo["file_name"]

        versions_json.setdefault(url, {})
        new_versions_json.setdefault(url, {})

        if file_name not in versions_json[url]:
            print(f'Adding versioning information for "{mod_name} : {file_name}"')
            new_versions_json[url][file_name] = file.latest
            continue

        current = versions_json[url][file_name]
        new_versions_json[url][file_name] = current

        if current == file.latest:
            continue

        if current in file.version_labels:
            current_label = file.version_labels[current]
        else:
            current_label = current

        if file.latest in file.version_labels:
            latest_label = file.version_labels[file.latest]
        else:
            latest_label = file.latest

        print()
        print(f'Update available for "{mod_name} : {file_name}": {current_label} -> {latest_label}')
        print(url)
        print()
        print("[a]pply/[s]kip: ", end="")

        if input().strip().lower() == "a":
            print("Applying update.")
            new_versions_json[url][file_name] = file.latest
        else:
            print("Skipping update.")

    with open(args.input.joinpath("versions.json"), "w") as f:
        json.dump(new_versions_json, f, indent=2, sort_keys=True)


def process_file(file: UmoFile):
    for process in (process_nexus,):
        id = process(file)
        if not id:
            continue

        file.latest = id
        return


def process_nexus(file: UmoFile):
    if "nexus_file_id" in file.dinfo:
        return

    nexus_match = re.match(r"https://www.nexusmods.com/(.*?)/.*/([0-9]+)", file.mod["url"])
    if not nexus_match:
        return

    nexus_url = f"https://api.nexusmods.com/v1/games/{nexus_match.group(1)}/mods/{nexus_match.group(2)}/files.json"
    nexus_data = get_nexus_data(nexus_url)

    best_timestamp = -2**63
    found_id = None
    found_old = True
    for i in nexus_data["files"]:
        file.version_labels[i["file_id"]] = i["version"]

        if i["name"] != file.dinfo["file_name"]:
            continue

        is_old = i["category_name"] in ["OLD_VERSION", "ARCHIVED", None]
        if is_old and not found_old:
            continue

        if i["uploaded_timestamp"] > best_timestamp:
            best_timestamp = i["uploaded_timestamp"]
            found_id = i["file_id"]
            found_old = is_old

    mod_name = file.mod["name"]
    file_name = file.dinfo["file_name"]

    if not found_id:
        print(f'WARNING: nothing found for "{mod_name} : {file_name}"')
        return
    if found_old:
        print(f'WARNING: only found an old file for "{mod_name} : {file_name}"')

    return found_id


@cache
def get_nexus_data(url):
    req = Request(url)
    req.add_header("apikey", nexus_key)
    with urlopen(req) as resp:
        return json.load(resp)


def get_nexus_key():
    # get nexus key from umo because i'm lazy
    with subprocess.Popen((get_tool_path("umo"), "info"),
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True, bufsize=1) as info:
        prefix = "config dir:\t\t"
        config_dir = Path()
        if not info.stdout:
            print("failed to get info from umo")
            return
        for line in info.stdout:
            if line.startswith(prefix):
                config_dir = Path(line.removeprefix(prefix).strip())
        with open(config_dir.joinpath("config.json")) as f:
            global nexus_key
            nexus_key = json.load(f)["NEXUS_API_KEY"]


def get_tool_path(name):
    if args.momw_dir:
        return args.momw_dir.joinpath(name)
    path = shutil.which(name)
    if path == None:
        print(f"Couldn't find \"{name}\", make sure your MOMW Tools directory is in PATH, or specify --momw-dir")
        exit(1)
    return path


if __name__ == "__main__":
    main()
