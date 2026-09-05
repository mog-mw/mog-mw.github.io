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
    current: int | str | None = None
    current_label: str | None = None
    latest: int | str | None = None
    latest_label: str | None = None


nexus_key: str
versions_json: dict


def main():
    with open(args.input.joinpath("versions.json")) as f:
        global versions_json
        versions_json = json.load(f)

    get_nexus_key()

    files: list[UmoFile] = []

    for filename in sorted(args.input.joinpath("mods").iterdir()):
        with open(filename) as f:
            y = yaml.load(f, Loader=yaml.Loader)
        for mod in y["mods"]:
            if "download_info" not in mod:
                continue
            for dinfo in mod["download_info"]:
                if "direct_download" in dinfo:
                    continue

                file = UmoFile(dinfo=dinfo, mod=mod)

                url = mod["url"]
                file_name = dinfo["file_name"]
                if url in versions_json and file_name in versions_json[url]:
                    file.current = versions_json[url][file_name]

                files.append(file)

    with ThreadPoolExecutor(max_workers=48) as executor:
        results = executor.map(process_file, files)

        # raise errors
        for result in results:
            result

    new_versions_json = {}

    for file in files:
        if not file.latest:
            continue

        mod_name = file.mod["name"]
        url = file.mod["url"]
        file_name = file.dinfo["file_name"]

        new_versions_json.setdefault(url, {})

        if not file.current:
            print(f'Adding versioning information for "{mod_name} : {file_name}"')
            new_versions_json[url][file_name] = file.latest
            continue

        new_versions_json[url][file_name] = file.current

        if file.current == file.latest:
            continue

        if not file.current_label:
            file.current_label = str(file.current)
        if not file.latest_label:
            file.latest_label = str(file.latest)

        print()
        print(f'Update available for "{mod_name} : {file_name}": {file.current_label} -> {file.latest_label}')
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
        if process(file):
            return


def process_nexus(file: UmoFile):
    nexus_match = re.match(r"https://www.nexusmods.com/(.*?)/.*/([0-9]+)", file.mod["url"])
    if not nexus_match:
        return False

    nexus_url = f"https://api.nexusmods.com/v1/games/{nexus_match.group(1)}/mods/{nexus_match.group(2)}/files.json"
    nexus_data = get_nexus_data(nexus_url)

    best_timestamp = -2**63
    found_old = True
    for i in nexus_data["files"]:
        if i["file_id"] == file.current:
            file.current_label = i["version"]

        if "nexus_file_id" in file.dinfo and file.dinfo["nexus_file_id"] != i["file_id"]:
            continue

        if i["name"] != file.dinfo["file_name"]:
            continue

        is_old = i["category_name"] in ["OLD_VERSION", "ARCHIVED", None]
        if is_old and not found_old:
            continue

        if i["uploaded_timestamp"] > best_timestamp:
            best_timestamp = i["uploaded_timestamp"]
            file.latest = i["file_id"]
            file.latest_label = i["version"]
            found_old = is_old

    mod_name = file.mod["name"]
    file_name = file.dinfo["file_name"]

    if not file.latest:
        print(f'WARNING: nothing found for "{mod_name} : {file_name}"')
    elif found_old:
        print(f'WARNING: only found an old file for "{mod_name} : {file_name}"')

    return True


def process_github(file: UmoFile):
    github_match = re.match(r"https://github.com/(.*)", file.mod["url"])
    if not github_match:
        return False

    api_prefix = f"https://api.github.com/repos/{github_match.group(1)}/"

    mod_name = file.mod["name"]
    file_name = file.dinfo["file_name"]

    if "branch" in file.dinfo:
        with urlopen(api_prefix + "branches/" + file.dinfo["branch"]) as resp:
            resp_json = json.load(resp)
        # TODO

    else:
        with urlopen(api_prefix + "releases") as resp:
            resp_json = json.load(resp)

        if type(resp_json) != list or len(resp_json) == 0:
            print(f'WARNING: no github releases found for "{mod_name} : {file_name}"; skipping')
            return True

        file.latest = resp_json[0]["assets"][0]["browser_download_url"]
        file.latest_label = resp_json[0]["name"]

        for release in resp_json:
            if release["assets"][0]["browser_download_url"] == file.current:
                file.current_label = release["name"]

    return True


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
