"""Prepare a release commit and an annotated tag.

    $ uv run python scripts/release.py 0.2.0

Sets the version, dates the changelog entry, commits both, and tags the commit
with the changelog section as the tag message. Nothing is pushed.
"""

import argparse
import datetime
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
INIT = ROOT / "django_celery_results_redis" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[.\-]?(?:a|b|rc|post|dev)\d+)?$")


def fail(message):
    sys.exit(f"error: {message}")


def git(*arguments, capture=True):
    result = subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=capture, text=True
    )
    if result.returncode:
        fail((result.stderr or result.stdout).strip())
    return (result.stdout or "").strip()


def set_version(version):
    source = INIT.read_text()
    updated, count = re.subn(
        r'^__version__ = ".*"$', f'__version__ = "{version}"', source, flags=re.M
    )
    if count != 1:
        fail(f"no __version__ assignment in {INIT.name}")
    INIT.write_text(updated)


def date_changelog(version, date):
    """Replace the unreleased heading of ``version`` with a dated one."""
    source = CHANGELOG.read_text()
    heading = f"## {version} ({date})"
    if f"\n{heading}\n" in source:
        return source
    updated, count = re.subn(
        rf"^## {re.escape(version)} \(unreleased\)$", heading, source, flags=re.M
    )
    if count != 1:
        fail(
            f"CHANGELOG.md has no '## {version} (unreleased)' heading; "
            "add the entries for this release first"
        )
    CHANGELOG.write_text(updated)
    return updated


def release_notes(changelog, version):
    """Return the entries under the heading of ``version``."""
    lines, notes, inside = changelog.splitlines(), [], False
    for line in lines:
        if line.startswith(f"## {version} ("):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            notes.append(line)
    body = "\n".join(notes).strip()
    if not body:
        fail(f"CHANGELOG.md has no entries under version {version}")
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="the version to release, without a 'v'")
    parser.add_argument("--date", help="release date, today by default")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="commit whatever else is in the working tree as well",
    )
    options = parser.parse_args()

    version = options.version.removeprefix("v")
    if not VERSION_PATTERN.match(version):
        fail(f"{version!r} is not a version like 1.2.3, 1.2.3rc1 or 1.2.3.dev1")
    date = options.date or datetime.date.today().isoformat()
    tag = f"v{version}"

    if git("tag", "--list", tag):
        fail(f"tag {tag} exists already")
    if git("status", "--porcelain") and not options.allow_dirty:
        fail("the working tree has changes; commit them or pass --allow-dirty")

    set_version(version)
    changelog = date_changelog(version, date)
    notes = release_notes(changelog, version)

    git(
        "add",
        "--all" if options.allow_dirty else str(INIT),
        *([] if options.allow_dirty else [str(CHANGELOG)]),
    )
    git("commit", "-m", f"Release {version}")
    git("tag", "-a", tag, "-m", f"{version}\n\n{notes}")

    print(f"Committed and tagged {tag}:\n")
    print(git("tag", "-n99", tag))
    print("\nPush when it looks right:\n")
    print(f"    git push origin {git('rev-parse', '--abbrev-ref', 'HEAD')}")
    print(f"    git push origin {tag}")
    print("\nTo undo:\n")
    print(f"    git tag -d {tag} && git reset --hard HEAD~1")


if __name__ == "__main__":
    main()
