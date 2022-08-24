
import dbt.clients.git
import dbt.clients.system
import dbt.config
import dbt.exceptions

from dbt.config import Project
from dbt.config.profile import Profile
from dbt.config.renderer import DbtProjectYamlRenderer, ProfileRenderer
from dbt.context.base import generate_base_context
from dbt.context.target import generate_target_context

import collections
import os
import time
import datetime
import json

import io
import hashlib
import requests


PHONY_PROFILE = {
    "hubcap": {
        "target": "dev",
        "outputs": {
            "dev": {
                "type": "postgres",
                "host": "localhost",
                "database": "analytics",
                "schema": "hubcap",
                "user": "user",
                "password": "password",
                "port": 5432
            }
        }
    }
}


NOW = int(time.time())
NOW_ISO = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()

CWD = os.path.dirname(os.path.realpath(__file__))
ROOT_DIR = os.path.dirname(CWD)

TMP_DIR = os.path.join(CWD, "git-tmp")
dbt.clients.system.make_directory(TMP_DIR)

config = {}
with open("config.json", "r") as fh:
    config = json.loads(fh.read())

with open("hub.current.json", "r") as fh:
    tracked = json.loads(fh.read())
    config['tracked_repos'] = tracked

TRACKED_REPOS = config['tracked_repos']
ONE_BRANCH_PER_REPO = config['one_branch_per_repo']
PUSH_BRANCHES = config['push_branches']
REMOTE = config['remote']

git_root_dir = os.path.join(TMP_DIR, "ROOT")

try:
    print("Updating root repo")
    dbt.clients.system.make_directory(TMP_DIR)
    if os.path.exists(git_root_dir):
        dbt.clients.system.rmdir(git_root_dir)

    dbt.clients.system.run_cmd(TMP_DIR, ['git', 'clone', REMOTE, 'ROOT'])
    dbt.clients.system.run_cmd(git_root_dir, ['git', 'checkout', 'master'])
    dbt.clients.system.run_cmd(git_root_dir, ['git', 'pull', 'origin', 'master'])
except dbt.exceptions.CommandResultError as e:
    print(e.stderr.decode())
    raise

INDEX_DIR = os.path.join(git_root_dir, "data")
indexed_files = dbt.clients.system.find_matching(INDEX_DIR, ['packages'], '*.json')

index = collections.defaultdict(lambda : collections.defaultdict(list))
for path in indexed_files:
    abs_path = path['absolute_path']
    filename = os.path.basename(abs_path)

    if filename == 'index.json':
        continue

    pop_1 = os.path.dirname(abs_path)
    pop_2 = os.path.dirname(pop_1)
    pop_3 = os.path.dirname(pop_2)

    repo_name = os.path.basename(pop_2)
    org_name = os.path.basename(pop_3)

    version = filename[:-5]
    info = {"path": abs_path, "version": version}

    if not config.get('refresh', False):
        index[org_name][repo_name].append(info)


def download(url):
    response = requests.get(url)

    file_buf = b""
    for block in response.iter_content(1024*64):
        file_buf += block

    return file_buf

def get_sha1(url):
    print(f"    downloading: {url}")
    contents = download(url)
    hasher = hashlib.sha1()
    hasher.update(contents)
    digest = hasher.hexdigest()
    print(f"      SHA1: {digest}")
    return digest

def get_project(git_path):
    phony_profile = Profile.from_raw_profiles(
        raw_profiles=PHONY_PROFILE,
        profile_name='hubcap',
        renderer=ProfileRenderer({})
    )

    ctx = generate_target_context(phony_profile, cli_vars={})
    renderer = DbtProjectYamlRenderer(ctx)
    return Project.from_project_root(git_path, renderer)

def make_spec(org, repo, version, git_path):
    tarball_url = f"https://codeload.github.com/{org}/{repo}/tar.gz/{version}"
    sha1 = get_sha1(tarball_url)

    project = get_project(git_path)
    packages = [p.to_dict() for p in project.packages.packages]
    package_name = project.project_name

    return {
        "id": f"{org}/{package_name}/{version}",
        "name": package_name,
        "version": version,
        "published_at": NOW_ISO,
        "packages": packages,
        "works_with": [],
        "_source": {
            "type": "github",
            "url": f"https://github.com/{org}/{repo}/tree/{version}/",
            "readme": f"https://raw.githubusercontent.com/{org}/{repo}/{version}/README.md",
        },
        "downloads": {"tarball": tarball_url, "format": "tgz", "sha1": sha1},
    }


def make_index(org_name, repo, existing, tags, git_path):
    description = f"dbt models for {repo}"
    assets = {
        "logo": "logos/placeholder.svg".format(repo)
    }

    if isinstance(existing, dict):
        description = existing.get('description', description)
        assets = existing.get('assets', assets)

    import dbt.semver
    version_tags = []
    for tag in tags:
        if tag.startswith('v'):
            tag = tag[1:]

        try:
            version_tag = dbt.semver.VersionSpecifier.from_version_string(tag)
            version_tags.append(version_tag)
        except dbt.exceptions.SemverException as e:
            print(f"Semver exception for {repo}. Skipping\n  {e}")

    # find latest tag which is not a prerelease
    latest = version_tags[0]
    for version_tag in version_tags:
        if version_tag > latest and not version_tag.prerelease:
            latest = version_tag

    project = get_project(git_path)
    package_name = project.project_name
    return {
        "name": package_name,
        "namespace": org_name,
        "description": description,
        "latest": latest.to_version_string().replace("=", ""), # LOL
        "assets": assets,
    }

def get_hub_versions(org, repo):
    url = f'https://hub.getdbt.com/api/v1/{org}/{repo}.json'
    resp = requests.get(url).json()
    return {r['version'] for r in resp['versions'].values()}

new_branches = {}
for org_name, repos in TRACKED_REPOS.items():
    for repo in repos:
        try:
            clone_url = f'https://github.com/{org_name}/{repo}.git'
            git_path = os.path.join(TMP_DIR, repo)

            print(f"Cloning repo {clone_url}")
            if os.path.exists(git_path):
                dbt.clients.system.rmdir(git_path)

            dbt.clients.system.run_cmd(TMP_DIR, ['git', 'clone', clone_url, repo])
            dbt.clients.system.run_cmd(git_path, ['git', 'fetch', '-t'])
            tags = dbt.clients.git.list_tags(git_path)

            project = get_project(git_path)
            package_name = project.project_name

            existing_tags = [i['version'] for i in index[org_name][package_name]]
            print(f"  Found Tags: {sorted(tags)}")
            print(f"  Existing Tags: {sorted(existing_tags)}")

            new_tags = set(tags) - set(existing_tags)

            if len(new_tags) == 0:
                print("    No tags to add. Skipping")
                continue

            # check out a new branch for the changes
            if ONE_BRANCH_PER_REPO:
                branch_name = f'bump-{org_name}-{repo}-{NOW}'
            else:
                branch_name = f'bump-{NOW}'

            index_path = os.path.join(TMP_DIR, "ROOT")
            print(f"    Checking out branch {branch_name} in meta-index")

            try:
                out, err = dbt.clients.system.run_cmd(index_path, ['git', 'checkout', branch_name])
            except dbt.exceptions.CommandResultError as e:
                dbt.clients.system.run_cmd(index_path, ['git', 'checkout', '-b', branch_name])

            new_branches[branch_name] = {"org": org_name, "repo": package_name}
            index_file_path = os.path.join(index_path, 'data', 'packages', org_name, package_name, 'index.json')

            if os.path.exists(index_file_path):
                existing_index_file_contents = dbt.clients.system.load_file_contents(index_file_path)
                try:
                    existing_index_file = json.loads(existing_index_file_contents)
                except:
                    existing_index_file = []
            else:
                existing_index_file = {}

            new_index_entry = make_index(org_name, repo, existing_index_file, set(tags) | set(existing_tags), git_path)
            repo_dir = os.path.join(index_path, 'data', 'packages', org_name, package_name, 'versions')
            dbt.clients.system.make_directory(repo_dir)
            dbt.clients.system.write_file(index_file_path, json.dumps(new_index_entry, indent=4))

            for i, tag in enumerate(sorted(new_tags)):
                print(f"    Adding tag: {tag}")

                import dbt.semver
                try:
                    raw_tag = tag
                    if raw_tag.startswith('v'):
                        raw_tag = tag[1:]
                    dbt.semver.VersionSpecifier.from_version_string(raw_tag)
                except dbt.exceptions.SemverException:
                    print(f"Not semver {raw_tag}. Skipping")
                    continue

                version_path = os.path.join(repo_dir, f"{tag}.json")

                package_spec = make_spec(org_name, repo, tag, git_path)
                dbt.clients.system.write_file(version_path, json.dumps(package_spec, indent=4))

                msg = f"hubcap: Adding tag {tag} for {org_name}/{repo}"
                print("      running `git add`")
                res = dbt.clients.system.run_cmd(repo_dir, ['git', 'add', '-A'])
                if len(res[1]):
                    print(f"ERROR{res[1].decode()}")
                print("      running `git commit`")
                res = dbt.clients.system.run_cmd(repo_dir, ['git', 'commit', '-am', f'{msg}'])
                if len(res[1]):
                    print(f"ERROR{res[1].decode()}")

            # good house keeping
            dbt.clients.system.run_cmd(index_path, ['git', 'checkout', 'master'])
            print()

        except dbt.exceptions.SemverException as e:
            print(f"Semver exception. Skipping\n  {e}")

        except Exception as e:
            print(f"Unhandled exception. Skipping\n  {e}")

def make_pr(ORG, REPO, head):
    url = 'https://api.github.com/repos/fishtown-analytics/hub.getdbt.com/pulls'
    body = {
        "title": f"HubCap: Bump {ORG}/{REPO}",
        "head": head,
        "base": "master",
        "body": f"Auto-bumping from new release at https://github.com/{ORG}/{REPO}/releases",
        "maintainer_can_modify": True,
    }

    body = json.dumps(body)

    user = config['user']['name']
    token = config['user']['token']
    req = requests.post(url, data=body, headers={'Content-Type': 'application/json'}, auth=(user, token))

def get_open_prs():
    url = 'https://api.github.com/repos/fishtown-analytics/hub.getdbt.com/pulls?state=open'

    user = config['user']['name']
    token = config['user']['token']
    req = requests.get(url, auth=(user, token))
    return req.json()

def is_open_pr(prs, ORG, REPO):
    for pr in prs:
        value = f'{ORG}/{REPO}'
        if value in pr['title']:
            return True

    return False

# push new branches, if there are any
print(f"Push branches? {PUSH_BRANCHES} - {list(new_branches.keys())}")
if PUSH_BRANCHES and len(new_branches) > 0:
    hub_dir = os.path.join(TMP_DIR, "ROOT")
    try:
        dbt.clients.system.run_cmd(hub_dir, ['git', 'remote', 'add', 'hub', REMOTE])
    except dbt.exceptions.CommandResultError as e:
        print(e.stderr.decode())

    open_prs = get_open_prs()

    for branch, info in new_branches.items():
        # don't open a PR if one is already open
        if is_open_pr(open_prs, info['org'], info['repo']):
            print(f"PR is already open for {info['org']}/{info['repo']}. Skipping.")
            continue

        try:
            dbt.clients.system.run_cmd(index_path, ['git', 'checkout', branch])
            try:
                dbt.clients.system.run_cmd(hub_dir, ['git', 'fetch', 'hub'])
            except dbt.exceptions.CommandResultError as e:
                print(e.stderr.decode())

            print(f"Pushing and PRing for {info['org']}/{info['repo']}")
            res = dbt.clients.system.run_cmd(hub_dir, ['git', 'push', 'hub', branch])
            print(res[1].decode())
            make_pr(info['org'], info['repo'], branch)
        except Exception as e:
            print(e)
